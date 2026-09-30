"""The model client. One function for text, one for typed output.

    from aip import chat, structured

    text = chat("Summarise this in one line:\n" + doc, tier="SMALL")

    class Ticket(BaseModel):
        category: Literal["billing", "technical", "account"]
        urgency: int = Field(ge=1, le=5)

    t = structured(prompt, schema=Ticket, tier="SMALL")   # -> Ticket instance

Everything else — retries, caching, cost, tracing, offline replay — happens
underneath. Read the code; it is the reference implementation for the
patterns T1 and T2 describe.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from aip import cache, cost, retry, tracing
from aip.config import resolve_model, settings

T = TypeVar("T", bound=BaseModel)

_INFLIGHT: dict[str, Future] = {}   # [regtech] single-flight, see raw_call
_INFLIGHT_LOCK = threading.Lock()

Messages = Sequence[dict[str, Any]]


class StructuredOutputError(RuntimeError):
    """The model could not be coerced into a valid instance of the schema."""

    def __init__(self, message: str, raw: str = "", attempts: int = 0):
        super().__init__(message)
        self.raw = raw
        self.attempts = attempts


# --------------------------------------------------------------------------
# core call
# --------------------------------------------------------------------------
def _normalise(prompt_or_messages: str | Messages, system: str | None) -> list[dict[str, Any]]:
    if isinstance(prompt_or_messages, str):
        msgs: list[dict[str, Any]] = [{"role": "user", "content": prompt_or_messages}]
    else:
        msgs = [dict(m) for m in prompt_or_messages]
    if system:
        msgs = [{"role": "system", "content": system}, *msgs]
    return msgs


def _is_retryable(exc: Exception) -> bool:
    # [regtech] predicate moved to aip.retry so aip.embed can share it.
    return retry.is_retryable(exc)


def raw_call(
    messages: list[dict[str, Any]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    response_format: dict | None = None,
    tools: list[dict] | None = None,
    tool_choice: Any | None = None,
    extra: dict | None = None,
) -> dict[str, Any]:
    """Single provider call with cache, retry, cost accounting and tracing.

    Returns a plain dict: {"text", "tool_calls", "usage", "raw"}.
    """
    request = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "tools": tools,
        "tool_choice": tool_choice,
        **(extra or {}),
    }
    key = cache.make_key("chat", request)

    hit = cache.get(key)
    if hit is not None:
        usage = cost.Usage(
            model=model,
            prompt_tokens=hit["usage"]["prompt_tokens"],
            completion_tokens=hit["usage"]["completion_tokens"],
            cost_usd=0.0,          # a cache hit costs nothing
            latency_ms=0.0,
            cached=True,
            calls=1,
            priced=True,           # free for certain -- it never left the machine
            cold_latency_ms=float(hit["usage"].get("latency_ms", 0.0) or 0.0),   # [regtech]
        )
        cost.record(usage)
        tracing.event("llm.call", model=model, cached=True, cost_usd=0.0)
        hit["usage"]["cached"] = True
        return hit

    if settings.offline:
        raise cache.CacheMiss(
            "AIP_OFFLINE=1 and this request is not in the cache.\n"
            f"model={model}\nfirst 200 chars of last message: "
            f"{str(messages[-1].get('content'))[:200]!r}\n"
            "Either run once online to populate the cache, or unset AIP_OFFLINE."
        )

    # [regtech] single-flight: identical requests in flight at the same time share one provider call.
    # Without it, parallel workers sending the same prompt each get their own (non-deterministic) answer,
    # and the cache keeps only the last one written, so a recorded run can no longer be replayed: a later
    # prompt built from the answer that was NOT kept misses the cache (found building the Stage 7 CI gate).
    with _INFLIGHT_LOCK:
        fut = _INFLIGHT.get(key)
        leader = fut is None
        if leader:
            fut = _INFLIGHT[key] = Future()
    if not leader:
        shared = fut.result()
        cost.record(cost.Usage(model, shared["usage"]["prompt_tokens"], shared["usage"]["completion_tokens"], 0.0,
                               0.0, cached=True, calls=1, priced=True,
                               cold_latency_ms=float(shared["usage"].get("latency_ms", 0.0) or 0.0)))
        tracing.event("llm.call", model=model, cached=True, coalesced=True, cost_usd=0.0)
        return {**shared, "usage": {**shared["usage"], "cached": True}}
    try:
        # re-check: a previous leader may have finished (and cached) between our cache miss and our claim
        late = cache.get(key)
        if late is not None:
            cost.record(cost.Usage(model, late["usage"]["prompt_tokens"], late["usage"]["completion_tokens"], 0.0,
                                   0.0, cached=True, calls=1, priced=True,
                                   cold_latency_ms=float(late["usage"].get("latency_ms", 0.0) or 0.0)))
            late["usage"]["cached"] = True
            fut.set_result(late)
            return late
        result = _call_provider(request, key, model=model, messages=messages, temperature=temperature,
                                max_tokens=max_tokens, response_format=response_format, tools=tools,
                                tool_choice=tool_choice, extra=extra)
        fut.set_result(result)
        return result
    except BaseException as exc:
        fut.set_exception(exc)
        raise
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)


def _call_provider(request: dict[str, Any], key: str, *, model: str, messages: list[dict[str, Any]],
                   temperature: float, max_tokens: int, response_format: dict | None, tools: list[dict] | None,
                   tool_choice: Any | None, extra: dict | None) -> dict[str, Any]:
    # LiteLLM is imported lazily: importing it costs ~1s, and offline/cached
    # runs should not pay for it.
    from litellm import completion

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": settings.timeout_s,
    }
    if response_format:
        kwargs["response_format"] = response_format
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    kwargs.update(extra or {})

    last_exc: Exception | None = None
    with tracing.trace("llm.call", model=model, cached=False) as span:
        for attempt in range(settings.max_retries):
            t0 = time.perf_counter()
            try:
                resp = completion(**kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not _is_retryable(exc) or attempt == settings.max_retries - 1:
                    span["error_kind"] = type(exc).__name__
                    raise
                # Exponential backoff with full jitter — the standard fix for
                # a fleet of 27 students hitting one rate limit at once.
                sleep_s = min(30.0, (2 ** attempt)) * random.random()
                tracing.event("llm.retry", attempt=attempt + 1, sleep_s=round(sleep_s, 2),
                              error=type(exc).__name__)
                time.sleep(sleep_s)
        else:  # pragma: no cover
            raise last_exc  # type: ignore[misc]

        latency_ms = (time.perf_counter() - t0) * 1000
        choice = resp.choices[0]
        text = choice.message.content or ""
        tool_calls = [
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in (getattr(choice.message, "tool_calls", None) or [])
        ]
        pt = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(resp.usage, "completion_tokens", 0) or 0)
        usd = cost.price_of(model, pt, ct)

        span.update(
            prompt_tokens=pt, completion_tokens=ct,
            cost_usd=round(usd, 6), latency_ms=round(latency_ms, 1),
            finish_reason=getattr(choice, "finish_reason", None),
        )
        cost.record(cost.Usage(model, pt, ct, usd, latency_ms, cached=False, calls=1,
                               priced=cost.is_priced(model)))

    result = {
        "text": text,
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cost_usd": usd,
            "latency_ms": latency_ms,
            "cached": False,
        },
        "finish_reason": getattr(choice, "finish_reason", None),
    }
    cache.put(key, "chat", request, result)
    return result


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def chat(
    prompt_or_messages: str | Messages,
    *,
    system: str | None = None,
    tier: str = "MAIN",
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 1024,
    tools: list[dict] | None = None,
    tool_choice: Any | None = None,
    return_full: bool = False,
    **extra: Any,
):
    """Free-text completion. Returns a string, or the full dict if return_full."""
    m = resolve_model(model or tier)
    result = raw_call(
        _normalise(prompt_or_messages, system),
        model=m,
        temperature=settings.temperature if temperature is None else temperature,
        max_tokens=max_tokens,
        tools=tools,
        tool_choice=tool_choice,
        extra=extra or None,
    )
    return result if return_full else result["text"]


class ChatStream:
    """[regtech] A streamed completion: iterate for text deltas; afterwards `.result` holds the same dict
    raw_call returns, and `.cached` / `.ttft_ms` say how it was served.

    It shares raw_call's cache key, so a streamed answer is reused by `chat()` and vice versa, and a
    cached answer is replayed in small pieces (offline replay and CI work unchanged). Cost, retries and
    tracing follow raw_call. Retries only cover opening the stream: once a token has been sent, a failure
    is raised, not silently restarted. The span is written when the stream ends (see tracing.span_record),
    because a stream is consumed across yields."""

    def __init__(self, messages: list[dict[str, Any]], *, model: str, temperature: float, max_tokens: int,
                 extra: dict | None = None, piece_chars: int = 24):
        self.request = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                        "response_format": None, "tools": None, "tool_choice": None, **(extra or {})}
        self.model, self.piece_chars = model, piece_chars
        self.result: dict[str, Any] | None = None
        self.cached = False
        self.ttft_ms: float | None = None
        self._parent_id = tracing._parent()["span_id"] if tracing._parent() else None
        self._trace_id = tracing.current_trace_id()

    def __iter__(self):
        key = cache.make_key("chat", self.request)
        t_start, started = time.perf_counter(), time.time()
        hit = cache.get(key)
        if hit is not None:
            cost.record(cost.Usage(self.model, hit["usage"]["prompt_tokens"], hit["usage"]["completion_tokens"],
                                   0.0, 0.0, cached=True, calls=1, priced=True,
                                   cold_latency_ms=float(hit["usage"].get("latency_ms", 0.0) or 0.0)))
            self.cached, self.result, self.ttft_ms = True, hit, 0.0
            text = hit["text"]
            for i in range(0, len(text), self.piece_chars):
                yield text[i:i + self.piece_chars]
            tracing.span_record("llm.stream", started, (time.perf_counter() - t_start) * 1000,
                                parent_id=self._parent_id, trace_id=self._trace_id, model=self.model, cached=True)
            return
        if settings.offline:
            raise cache.CacheMiss("AIP_OFFLINE=1 and this streamed request is not in the cache "
                                  f"(model={self.model}).")
        from litellm import completion

        kwargs = {k: v for k, v in self.request.items() if k not in ("response_format", "tools", "tool_choice")}
        resp = retry.call_with_retries(
            lambda: completion(**kwargs, timeout=settings.timeout_s, stream=True,
                               stream_options={"include_usage": True}), event="llm.retry")
        parts: list[str] = []
        finish, pt, ct = None, 0, 0
        status = "ok"
        try:
            for chunk in resp:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = getattr(choices[0].delta, "content", None) or ""
                    finish = getattr(choices[0], "finish_reason", None) or finish
                    if delta:
                        if self.ttft_ms is None:
                            self.ttft_ms = (time.perf_counter() - t_start) * 1000
                        parts.append(delta)
                        yield delta
                usage = getattr(chunk, "usage", None)
                if usage:
                    pt = int(getattr(usage, "prompt_tokens", 0) or 0)
                    ct = int(getattr(usage, "completion_tokens", 0) or 0)
        except Exception:
            status = "error"
            raise
        finally:
            latency_ms = (time.perf_counter() - t_start) * 1000
            usd = cost.price_of(self.model, pt, ct)
            tracing.span_record("llm.stream", started, latency_ms, parent_id=self._parent_id,
                                trace_id=self._trace_id, model=self.model, cached=False, status=status,
                                ttft_ms=round(self.ttft_ms or 0.0, 1), prompt_tokens=pt, completion_tokens=ct,
                                cost_usd=round(usd, 6), finish_reason=finish)
        cost.record(cost.Usage(self.model, pt, ct, usd, latency_ms, cached=False, calls=1,
                               priced=cost.is_priced(self.model)))
        self.result = {"text": "".join(parts), "tool_calls": [],
                       "usage": {"prompt_tokens": pt, "completion_tokens": ct, "cost_usd": usd,
                                 "latency_ms": latency_ms, "cached": False},
                       "finish_reason": finish}
        cache.put(key, "chat", self.request, self.result)


def stream_chat(prompt_or_messages: str | Messages, *, system: str | None = None, tier: str = "MAIN",
                model: str | None = None, temperature: float | None = None, max_tokens: int = 1024,
                **extra: Any) -> ChatStream:
    """[regtech] Streaming counterpart of chat(): `for delta in stream_chat(...)`, then `.result`."""
    return ChatStream(_normalise(prompt_or_messages, system), model=resolve_model(model or tier),
                      temperature=settings.temperature if temperature is None else temperature,
                      max_tokens=max_tokens, extra=extra or None)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Best-effort JSON recovery from a model response.

    Models wrap JSON in prose and fences more often than their docs admit.
    Order matters: try the whole string, then fenced blocks, then the widest
    balanced brace/bracket span.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for block in _JSON_BLOCK.findall(text):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"No JSON object found in response: {text[:300]!r}")


def structured(
    prompt_or_messages: str | Messages,
    *,
    schema: type[T],
    system: str | None = None,
    tier: str = "SMALL",
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 2048,
    max_repairs: int = 2,
    **extra: Any,
) -> T:
    """Return a validated instance of `schema`, or raise StructuredOutputError.

    The strategy, in order:
      1. Ask for JSON, giving the model the JSON Schema in the system prompt.
         (Provider-native JSON mode is requested where supported.)
      2. Parse it, tolerating fences and prose.
      3. Validate with Pydantic.
      4. On failure, send the *validation errors back to the model* and ask for
         a repair. This "repair loop" is the single highest-leverage
         reliability trick in applied GenAI, and it is why we do not simply
         trust `response_format`.

    Every attempt is cached and counted, so a repair is not free — which is
    exactly the trade-off you are asked to measure in Lab 1.
    """
    m = resolve_model(model or tier)
    json_schema = schema.model_json_schema()
    instruction = (
        "You must reply with a single JSON object and nothing else. "
        "No prose, no markdown fences, no explanation.\n"
        "The object must validate against this JSON Schema:\n"
        f"{json.dumps(json_schema, indent=2)}"
    )
    sys_prompt = f"{system}\n\n{instruction}" if system else instruction
    messages = _normalise(prompt_or_messages, sys_prompt)

    # Provider-native JSON mode where available; harmless elsewhere because we
    # fall back to text parsing anyway.
    response_format: dict | None = {"type": "json_object"}
    if m.startswith(("ollama/", "local/")):
        response_format = None

    raw_text = ""
    errors = ""
    budget = max_tokens
    for attempt in range(max_repairs + 1):
        try:
            result = raw_call(
                messages,
                model=m,
                temperature=settings.temperature if temperature is None else temperature,
                max_tokens=budget,
                response_format=response_format,
                extra=extra or None,
            )
        except Exception:
            if response_format is not None and attempt == 0:
                response_format = None  # provider rejected json mode; retry plain
                continue
            raise

        raw_text = result["text"]

        # Truncation is not a schema problem and must not be "repaired" as one.
        # Some models reason in their VISIBLE output -- NVIDIA's nemotron family
        # opens with "We need to output JSON with fields..." -- and exhaust the
        # token budget before emitting a single brace. Sending that back with
        # validation errors is useless; the fix is more room. Double and retry.
        if result.get("finish_reason") == "length" and attempt < max_repairs:
            tracing.event("structured.truncated", attempt=attempt + 1,
                          budget=budget, next_budget=budget * 2)
            budget *= 2
            continue

        try:
            return schema.model_validate(extract_json(raw_text))
        except (ValidationError, ValueError) as exc:
            errors = str(exc)[:2000]
            tracing.event("structured.repair", attempt=attempt + 1, error=errors[:300])
            if attempt == max_repairs:
                break
            messages = [
                *messages,
                {"role": "assistant", "content": raw_text},
                {
                    "role": "user",
                    "content": (
                        "That output failed validation with these errors:\n"
                        f"{errors}\n\n"
                        "Return the corrected JSON object only. Fix exactly the "
                        "fields named above; leave the others unchanged."
                    ),
                },
            ]

    raise StructuredOutputError(
        f"Could not obtain a valid {schema.__name__} after {max_repairs + 1} attempts. "
        f"Last validation error: {errors}",
        raw=raw_text,
        attempts=max_repairs + 1,
    )
