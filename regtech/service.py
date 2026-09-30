"""Stage 7: the RegTech Compliance Copilot as an HTTP service (Lab 7 pattern).

    uvicorn regtech.service:app --port 8000
    python -m regtech serve                       # the same

Endpoints
    POST /ask          {question, mode: "qa" | "agent", upload_id?, as_of?}  -> answer, citations, cost, trace id
    POST /ask/stream   Q&A mode as server-sent events: sources, tokens, then a verdict (Lab 7 B3)
    POST /upload       a policy PDF for the agent to check; returns an upload_id
    GET  /health       indices, models, caches, corpus version
    GET  /metrics      cost, latency percentiles, cache hit rate, errors by type, refusals, tool calls

Two modes, because they are two products with different budgets:
    qa     grounded Q&A over the RBI Directions (Stage 2 pipeline, SMALL tier: p95 ~2 s, ~$0.001)
    agent  the Stage 6 agent with its guardrails (any question; 10-35 s, up to ~$0.03 with a gap check)

Built on aip: tracing (one trace id per request, returned to the caller), cost.Budget (per-request ceiling
-> 429), retry.is_retryable (provider trouble -> 503 + Retry-After), response_cache (exact cache; the
semantic cache is off, see reports/stage7_semantic_threshold.md), rag.RagPipeline.stream, guards.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aip import cache, retry, tracing
from aip.config import resolve_model, settings
from aip.cost import Budget, BudgetExceeded
from aip.guards import detect_injection
from aip.response_cache import ExactCache, SemanticCache

from regtech.paths import DATA_DIR, MANIFEST_PATH, PROCESSED_DIR, REPO_ROOT

QA_TIER = os.getenv("REGTECH_QA_TIER", "SMALL")
_sem = os.getenv("REGTECH_SEMANTIC_THRESHOLD", "off")
SEMANTIC_THRESHOLD: float | None = None if _sem.lower() in ("", "off", "none") else float(_sem)
UPLOAD_DIR = Path(os.getenv("REGTECH_UPLOAD_DIR", str(REPO_ROOT / "uploads")))
QA_BUDGET_USD = float(os.getenv("REGTECH_QA_BUDGET_USD", "0.05"))
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
P95_SLO_MS = {"qa": 6000.0, "agent": 40000.0}


def corpus_version() -> str:
    """Changes whenever the manifest or a derived table changes, so cached responses never outlive their corpus."""
    h = hashlib.sha256()
    for p in (MANIFEST_PATH, PROCESSED_DIR / "compliance_calendar.json", PROCESSED_DIR / "enforcement_cases.json"):
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    mode: Literal["qa", "agent"] = "qa"
    upload_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$")
    as_of: date | None = None


class Citation(BaseModel):
    index: int
    doc_id: str
    label: str
    excerpt: str


class AskResponse(BaseModel):
    answer: str
    refused: bool
    mode: str
    citations: list[Citation]
    tool_calls: list[dict] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    latency_ms: float
    cost_usd: float
    cached: Literal["exact", "semantic"] | None = None
    trace_id: str


# ---------------------------------------------------------------------------------------------
# Request ledger for /metrics
# ---------------------------------------------------------------------------------------------
@dataclass
class RequestRecord:
    ts: float
    endpoint: str
    mode: str
    status: int
    latency_ms: float
    cost_usd: float = 0.0
    cached: str | None = None
    refused: bool = False
    tools: list[str] = field(default_factory=list)
    ttft_ms: float | None = None
    error: str | None = None
    trace_id: str | None = None


class Ledger:
    def __init__(self, keep: int = 5000):
        self.records: list[RequestRecord] = []
        self.keep, self._lock = keep, threading.Lock()

    def add(self, r: RequestRecord) -> None:
        with self._lock:
            self.records.append(r)
            del self.records[:-self.keep]

    def snapshot(self) -> list[RequestRecord]:
        with self._lock:
            return list(self.records)


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1)


def summarise_ledger(records: list[RequestRecord]) -> dict[str, Any]:
    today = time.mktime(date.today().timetuple())
    asks = [r for r in records if r.endpoint in ("/ask", "/ask/stream")]
    ok = [r for r in asks if r.status == 200]
    out: dict[str, Any] = {
        "requests": len(asks),
        "requests_today": sum(r.ts >= today for r in asks),
        "cost_usd_today": round(sum(r.cost_usd for r in asks if r.ts >= today), 5),
        "cost_usd_total": round(sum(r.cost_usd for r in asks), 5),
        "cost_per_query_usd": round(sum(r.cost_usd for r in ok) / len(ok), 5) if ok else None,
        "response_cache_hit_rate": round(sum(r.cached is not None for r in ok) / len(ok), 3) if ok else None,
        "refusal_rate": round(sum(r.refused for r in ok) / len(ok), 3) if ok else None,
        "errors_by_status": {str(s): sum(r.status == s for r in asks) for s in sorted({r.status for r in asks}) if s != 200},
        "error_rate": round(sum(r.status != 200 for r in asks) / len(asks), 3) if asks else None,
        "tool_calls": {t: sum(t in r.tools for r in asks) for t in sorted({t for r in asks for t in r.tools})},
        "by_mode": {},
    }
    for mode in ("qa", "agent"):
        rs = [r for r in ok if r.mode == mode]
        uncached = [r.latency_ms for r in rs if r.cached is None]
        cached = [r.latency_ms for r in rs if r.cached is not None]
        out["by_mode"][mode] = {
            "requests": len(rs), "p50_ms": _pct([r.latency_ms for r in rs], 0.5),
            "p95_ms": _pct([r.latency_ms for r in rs], 0.95), "p99_ms": _pct([r.latency_ms for r in rs], 0.99),
            "p95_uncached_ms": _pct(uncached, 0.95), "p95_cached_ms": _pct(cached, 0.95),
            "cost_per_query_usd": round(sum(r.cost_usd for r in rs) / len(rs), 5) if rs else None,
            "p95_slo_ms": P95_SLO_MS[mode],
        }
    ttft = [r.ttft_ms for r in asks if r.ttft_ms is not None]
    out["stream"] = {"requests": sum(r.endpoint == "/ask/stream" for r in asks), "ttft_p50_ms": _pct(ttft, 0.5),
                     "ttft_p95_ms": _pct(ttft, 0.95)}
    # Lab 7 C4 alert: the refusal rate doubling usually means the index broke (retrieval returns nothing useful)
    recent, before = ok[-20:], ok[-120:-20]
    r_now = sum(r.refused for r in recent) / len(recent) if recent else 0.0
    r_before = sum(r.refused for r in before) / len(before) if before else None
    out["alerts"] = []
    if r_before is not None and len(recent) >= 10 and r_now >= max(2 * r_before, 0.2):
        out["alerts"].append(f"refusal rate {r_now:.0%} over the last {len(recent)} answers vs {r_before:.0%} before: "
                             "check the index (GET /health) and the retrieval spans of recent traces")
    for mode, m in out["by_mode"].items():
        if m["requests"] >= 10 and m["p95_ms"] and m["p95_ms"] > m["p95_slo_ms"]:
            out["alerts"].append(f"{mode} p95 {m['p95_ms']:.0f} ms is above its {m['p95_slo_ms']:.0f} ms SLO")
    return out


# ---------------------------------------------------------------------------------------------
# The pipelines: built once, at startup
# ---------------------------------------------------------------------------------------------
class Copilot:
    def new_agent(self, as_of: date | None):
        """A per-request agent (it holds per-request state such as the calendar date) over the shared tools."""
        agent = self._agent_cls(self._layers, policy_root=UPLOAD_DIR, today=as_of)
        agent._tools = self.tools
        return agent

    def __init__(self):
        from regtech.agent import DEFAULT_LAYERS, ComplianceAgent
        from regtech.index import CorpusIndex
        from regtech.policy_gap import PolicyGapChecker
        from regtech.precedent import PrecedentFinder
        from regtech.qa import RegulationQA

        t0 = time.perf_counter()
        # aip imports LiteLLM lazily on the first uncached call; measured at ~8.5 s of the first request's
        # 9.9 s (trace 8231aaa14c45). Pay it at startup instead.
        import litellm  # noqa: F401
        self.indices = {t: CorpusIndex(t) for t in ("regulation", "internal_policy", "enforcement")}
        self.qa = RegulationQA(index=self.indices["regulation"], tier=QA_TIER)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self._agent_cls, self._layers = ComplianceAgent, DEFAULT_LAYERS
        self.tools = {   # built once; every request's agent shares them (the agent's Q&A tool stays on MAIN)
            "qa": RegulationQA(index=self.indices["regulation"]),
            "gap": PolicyGapChecker(reg_index=self.indices["regulation"], policy_index=self.indices["internal_policy"]),
            "prec": PrecedentFinder(index=self.indices["enforcement"]),
        }
        self.exact = ExactCache()
        self.semantic = SemanticCache(threshold=SEMANTIC_THRESHOLD)
        self.version = corpus_version()
        self.ledger = Ledger()
        self.startup_s = round(time.perf_counter() - t0, 1)


_COPILOT: Copilot | None = None
_STARTED = time.time()


def copilot() -> Copilot:
    global _COPILOT
    if _COPILOT is None:
        _COPILOT = Copilot()
    return _COPILOT


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("REGTECH_LAZY_START") != "1":
        await asyncio.to_thread(copilot)       # build indices before the first request, not during it
    yield


app = FastAPI(title="RegTech Compliance Copilot", version="1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------------------------
# Errors (Lab 7 A3): 422 is FastAPI's; budget -> 429; provider trouble -> 503 + Retry-After; bug -> 500
# ---------------------------------------------------------------------------------------------
def _classify(exc: Exception) -> tuple[int, str, dict[str, str]]:
    if isinstance(exc, BudgetExceeded):
        return 429, "spend limit reached for this request or for the service; try again later", {}
    if isinstance(exc, cache.CacheMiss):
        return 503, "model unavailable (offline mode and this request is not cached)", {"Retry-After": "60"}
    if retry.is_retryable(exc):
        return 503, "upstream model unavailable; retry shortly", {"Retry-After": "30"}
    return 500, "internal error; quote the trace id when reporting it", {}


def _error(status: int, detail: str, headers: dict[str, str], trace_id: str | None) -> JSONResponse:
    return JSONResponse({"detail": detail, "trace_id": trace_id}, status_code=status, headers=headers)


# ---------------------------------------------------------------------------------------------
# POST /ask
# ---------------------------------------------------------------------------------------------
def _qa_answer(c: Copilot, question: str) -> tuple[str, bool, list[Citation], list[str]]:
    from regtech.agent import USER_SIGNALS, output_filter
    verdict = detect_injection(question, USER_SIGNALS)
    if verdict.flagged:
        return ("Your message looks like an attempt to override the assistant's rules. Please rephrase the "
                "compliance question.", True, [], [f"input: {verdict.signals}"])
    ans = c.qa.ask(question)
    text, flags = output_filter(ans.text, c.qa.pipeline.system or "")
    cites = [Citation(index=s["n"], doc_id=s["doc_id"], label=s["label"], excerpt=s["text"][:1500])
             for s in ans.sources if s["n"] in ans.cited]
    return text, ans.refused, cites, flags


def _agent_answer(c: Copilot, req: AskRequest) -> tuple[str, bool, list[dict], list[str]]:
    message = req.question
    if req.upload_id:
        path = UPLOAD_DIR / f"{req.upload_id}.pdf"
        if not path.is_file():
            raise HTTPException(404, "unknown upload_id")
        message += f"\n\n(The uploaded policy file is at {path.relative_to(REPO_ROOT).as_posix()}.)" \
            if REPO_ROOT in path.parents else f"\n\n(The uploaded policy file is at {path}.)"
    run = c.new_agent(req.as_of).run(message)
    tools = [{"tool": t["tool"], "ok": t["ok"]} for t in run.tool_calls]
    return run.answer, run.refused, tools, run.flags + ([f"stopped: {run.stop_reason}"]
                                                         if run.stop_reason != "answered" else [])


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    c = copilot()
    t0 = time.perf_counter()
    scope = f"{req.mode}|{c.version}|{req.upload_id or ''}|{(req.as_of or date.today()) if req.mode == 'agent' else ''}"
    trace_id = None
    try:
        with tracing.trace("http.ask", mode=req.mode, question=req.question[:120]) as span:
            trace_id = span["trace_id"]
            hit = c.exact.get(req.question, scope)
            kind = "exact" if hit else None
            if hit is None and req.mode == "qa" and c.semantic.threshold is not None:
                sem = c.semantic.get(req.question, scope)
                if sem:
                    hit, kind = sem.value, "semantic"
                    span["semantic_similarity"] = round(sem.similarity, 4)
            if hit is not None:
                resp = AskResponse(**{**hit, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                                      "cost_usd": 0.0, "cached": kind, "trace_id": trace_id})
            else:
                with Budget(limit_usd=QA_BUDGET_USD if req.mode == "qa" else 0.25, label=f"http.{req.mode}") as b:
                    if req.mode == "qa":
                        text, refused, cites, flags = _qa_answer(c, req.question)
                        tools: list[dict] = []
                    else:
                        text, refused, tools, flags = _agent_answer(c, req)
                        cites = []
                resp = AskResponse(answer=text, refused=refused, mode=req.mode, citations=cites, tool_calls=tools,
                                   flags=flags, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                                   cost_usd=round(b.spent_usd, 6), cached=None, trace_id=trace_id)
                body = resp.model_dump(exclude={"latency_ms", "cost_usd", "cached", "trace_id"})
                if not any(f.startswith("stopped:") for f in flags):   # never cache a run that hit a limit
                    c.exact.put(req.question, body, scope)
                    if req.mode == "qa" and c.semantic.threshold is not None:
                        c.semantic.put(req.question, body, scope)
            span.update(status_code=200, cost_usd=resp.cost_usd, cached=resp.cached, refused=resp.refused,
                        n_tools=len(resp.tool_calls))
        c.ledger.add(RequestRecord(time.time(), "/ask", req.mode, 200, resp.latency_ms, resp.cost_usd, resp.cached,
                                   resp.refused, [t["tool"] for t in resp.tool_calls], trace_id=trace_id))
        return resp
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a status code, never a stack trace
        status, detail, headers = _classify(exc)
        tracing.event("http.error", status_code=status, error=f"{type(exc).__name__}: {str(exc)[:200]}",
                      trace_id_ref=trace_id)
        c.ledger.add(RequestRecord(time.time(), "/ask", req.mode, status, (time.perf_counter() - t0) * 1000,
                                   error=type(exc).__name__, trace_id=trace_id))
        return _error(status, detail, headers, trace_id)


# ---------------------------------------------------------------------------------------------
# POST /ask/stream: Q&A as server-sent events (Lab 7 B2/B3, "stream, then verdict")
# ---------------------------------------------------------------------------------------------
class StreamRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


def _stream_worker(c: Copilot, question: str, out: queue.Queue) -> None:
    """Runs the whole stream in ONE thread and context, so every span lands under one trace id."""
    from regtech.agent import USER_SIGNALS, output_filter
    from regtech.qa import source_label
    t0 = time.perf_counter()
    rec = RequestRecord(time.time(), "/ask/stream", "qa", 200, 0.0)
    try:
        with tracing.trace("http.ask_stream", question=question[:120]) as span:
            rec.trace_id = span["trace_id"]
            out.put(("meta", {"trace_id": span["trace_id"]}))
            verdict = detect_injection(question, USER_SIGNALS)
            if verdict.flagged:
                rec.refused = True
                out.put(("verdict", {"answer": "Your message looks like an attempt to override the assistant's rules.",
                                     "refused": True, "retracted": False, "citations": [], "flags": verdict.signals}))
            else:
                with Budget(limit_usd=QA_BUDGET_USD, label="http.stream") as b:
                    for ev in c.qa.pipeline.stream(question):
                        if ev["type"] == "sources":
                            out.put(("sources", [{"index": i, "doc_id": h.doc_id, "label": source_label(h)}
                                                 for i, h in enumerate(ev["hits"], start=1)]))
                        elif ev["type"] == "token":
                            if rec.ttft_ms is None:
                                rec.ttft_ms = round((time.perf_counter() - t0) * 1000, 1)
                            out.put(("token", {"text": ev["text"]}))
                        else:
                            a = ev["answer"]
                            text, flags = output_filter(a.answer, c.qa.pipeline.system or "")
                            rec.refused = a.refused
                            cites = [{"index": i, "doc_id": h.doc_id, "label": source_label(h), "excerpt": h.text[:1500]}
                                     for i, h in enumerate(a.hits, start=1) if i in a.cited_indices]
                            out.put(("verdict", {"answer": text, "refused": a.refused, "retracted": ev["retracted"],
                                                 "citations": cites, "flags": flags,
                                                 "draft_changed_by_filter": text != a.answer}))
                rec.cost_usd = round(b.spent_usd, 6)
            rec.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            span.update(status_code=200, cost_usd=rec.cost_usd, ttft_ms=rec.ttft_ms, refused=rec.refused)
            out.put(("done", {"latency_ms": rec.latency_ms, "ttft_ms": rec.ttft_ms, "cost_usd": rec.cost_usd,
                              "trace_id": rec.trace_id}))
    except Exception as exc:  # noqa: BLE001
        status, detail, _ = _classify(exc)
        rec.status, rec.error = status, type(exc).__name__
        rec.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        out.put(("error", {"status": status, "detail": detail, "trace_id": rec.trace_id}))
    finally:
        c.ledger.add(rec)
        out.put(None)


@app.post("/ask/stream")
async def ask_stream(req: StreamRequest, request: Request):
    from sse_starlette.sse import EventSourceResponse
    c = copilot()
    q: queue.Queue = queue.Queue()
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(_stream_worker, c, req.question, q), daemon=True).start()

    async def events():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                return
            name, data = item
            yield {"event": name, "data": json.dumps(data)}
            if await request.is_disconnected():
                return

    return EventSourceResponse(events())


# ---------------------------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------------------------
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file larger than 10 MB")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(415, "only PDF files are accepted")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    (UPLOAD_DIR / f"{upload_id}.pdf").write_bytes(data)
    tracing.event("http.upload", bytes=len(data), upload_id=upload_id)
    return {"upload_id": upload_id, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "note": "The check reports what this document says; it does not verify that the document is genuine."}


# ---------------------------------------------------------------------------------------------
# GET /health, GET /metrics
# ---------------------------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    c = copilot()
    return {"status": "ok", "uptime_s": round(time.time() - _STARTED, 1), "startup_s": c.startup_s,
            "corpus_version": c.version,
            "indices": {t: {"chunks": len(i.chunks), "documents": len(i.doc_ids)} for t, i in c.indices.items()},
            "models": {"qa": resolve_model(QA_TIER), "agent": resolve_model("MAIN"), "gap_assessment": resolve_model("SMALL"),
                       "embeddings": resolve_model("EMBED"), "profile": settings.profile},
            "response_cache": {"exact": {"entries": len(c.exact), **c.exact.stats.as_dict()},
                               "semantic": {"threshold": c.semantic.threshold, "entries": len(c.semantic),
                                            **c.semantic.stats.as_dict()}},
            "model_call_cache": cache.stats(), "offline": settings.offline}


@app.get("/metrics")
def metrics() -> dict:
    return summarise_ledger(copilot().ledger.snapshot())


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run("regtech.service:app", host=host, port=port)
