"""Minimal JSONL tracing.

Observability in one file. Every model call appends a record to
`.aip_traces/<run>.jsonl`. In Lab 7 you will read these back to build a
latency and cost dashboard; in Lab 5 you will read them to work out which
stage of your RAG pipeline produced a bad answer.

This is a teaching-scale stand-in for Langfuse / LangSmith / Phoenix. The
concept is identical: structured events, a run id, and a parent span id.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from aip.config import settings

RUN_ID = os.getenv("AIP_RUN_ID") or time.strftime("%Y%m%d-%H%M%S")
_TRACE_FILE: Path = settings.trace_dir / f"{RUN_ID}.jsonl"
_WRITE_LOCK = threading.Lock()

# [regtech] The open-span stack lives in a ContextVar (it was a threading.local). Work handed to a thread
# pool through aip.cost.map_in_context, or run by a web framework's worker threads with the request's
# context, now keeps its parent span, and every record carries the `trace_id` of its root span, so one
# request's whole tree (including parallel tool calls) can be read back with a single id.
# Stored as an immutable tuple so a copied context can never mutate its parent's stack.
_STACK: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar("aip_span_stack", default=())


def _parent() -> dict[str, Any] | None:
    stack = _STACK.get()
    return stack[-1] if stack else None


def current_trace_id() -> str | None:
    """[regtech] The trace id (root span id) of the span currently open in this context, if any."""
    p = _parent()
    return p["trace_id"] if p else None


@contextlib.contextmanager
def trace(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Open a span.

        with trace("retrieve", k=8) as span:
            docs = retriever.search(q, k=8)
            span["n_results"] = len(docs)
    """
    parent = _parent()
    span_id = uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "run_id": RUN_ID,
        "span_id": span_id,
        "parent_id": parent["span_id"] if parent else None,
        "trace_id": parent["trace_id"] if parent else span_id,
        "name": name,
        "ts": time.time(),
        **attrs,
    }
    previous = _STACK.get()
    _STACK.set((*previous, record))
    t0 = time.perf_counter()
    try:
        yield record
        record["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - we re-raise
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        _STACK.set(previous)   # set, not reset(token): safe even if a framework resumed us in a copied context
        _write(record)


def event(name: str, **attrs: Any) -> None:
    """Record a point-in-time event with no duration."""
    parent = _parent()
    span_id = uuid.uuid4().hex[:12]
    _write(
        {
            "run_id": RUN_ID,
            "span_id": span_id,
            "parent_id": parent["span_id"] if parent else None,
            "trace_id": parent["trace_id"] if parent else span_id,
            "name": name,
            "ts": time.time(),
            "duration_ms": 0.0,
            "status": "ok",
            **attrs,
        }
    )


def span_record(name: str, started: float, duration_ms: float, *, parent_id: str | None = None,
                trace_id: str | None = None, **attrs: Any) -> None:
    """[regtech] Write a finished span whose timing was measured by hand.

    For work that spans generator `yield`s (streaming): a `with trace(...)` held open across a yield would
    be resumed in whatever context the consumer happens to use, so the stream measures its own timing and
    writes the span when it finishes, under the parent/trace ids it captured at the start."""
    span_id = uuid.uuid4().hex[:12]
    _write({"run_id": RUN_ID, "span_id": span_id, "parent_id": parent_id, "trace_id": trace_id or span_id,
            "name": name, "ts": started, "duration_ms": round(duration_ms, 2), "status": attrs.pop("status", "ok"),
            **attrs})


def _write(record: dict[str, Any]) -> None:
    line = json.dumps(record, default=str)
    with _WRITE_LOCK, _TRACE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_traces(run_id: str | None = None) -> list[dict[str, Any]]:
    """Load one run's traces (default: the current run)."""
    path = settings.trace_dir / f"{run_id or RUN_ID}.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def trace_file() -> Path:
    return _TRACE_FILE
