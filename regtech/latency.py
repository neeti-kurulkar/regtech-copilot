"""Stage 7 (Lab 7 B2/B4): end-to-end latency of the running service, by mode and by stage.

    python -m regtech latency

Runs requests through the real FastAPI app (in-process TestClient, so no network hop to the service itself),
one at a time, after start-up. Uncached numbers are measured with the model-call cache OFF (every query is
embedded and answered by the provider, as for a new question); cached numbers are the same questions again
through the exact response cache. The per-stage breakdown is read back from the traces by trace id.
"""
from __future__ import annotations

import json
import time

from aip import tracing
from aip.config import settings

from regtech.paths import REPORTS_DIR

QA_QUESTIONS = [
    "What must an NBFC include in the Key Facts Statement?",
    "Within how many days must property documents be returned after full repayment?",
    "How often must KYC be updated for high-risk customers?",
    "Can an NBFC levy foreclosure charges on floating rate loans to individuals?",
    "What are the permitted calling hours for recovery agents of microfinance loans?",
    "Who can be appointed as Chief Compliance Officer of an NBFC?",
    "Within how many days must an NBFC refund the surplus from a gold auction?",
    "What must an NBFC do after classifying an account as fraud?",
    "What is the maximum loan-to-value ratio for gold loans?",
    "How should penal charges be disclosed to borrowers?",
    "What is the tenure of the Chief Compliance Officer?",
    "Must the sanction letter be in a language the borrower understands?",
]
AGENT_QUESTIONS = [
    "What compliance deadlines are due in the next 30 days?",
    "Has the RBI penalised any NBFC for not returning gold auction surplus to borrowers?",
    "Check IIFL's Fair Practices Code on transfer of a loan account to another lender.",
    "Check Tata Capital's Fair Practices Code on the Key Facts Statement for loans.",
]
STAGES = ("embed.batch", "retrieve.dense", "llm.call", "llm.stream", "tool.call")


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1) if xs else float("nan")


def _stages(trace_ids: list[str]) -> dict[str, dict[str, float]]:
    rows = [r for r in tracing.read_traces() if r.get("trace_id") in set(trace_ids)]
    out = {}
    for name in STAGES:
        per_req = {}
        for r in rows:
            if r["name"] == name and r.get("duration_ms"):
                per_req[r["trace_id"]] = per_req.get(r["trace_id"], 0.0) + r["duration_ms"]
        if per_req:
            vals = list(per_req.values())
            out[name] = {"p50_ms": _pct(vals, 0.5), "p95_ms": _pct(vals, 0.95), "requests": len(vals)}
    return out


def run() -> dict:
    from fastapi.testclient import TestClient

    from regtech.service import app

    result: dict = {}
    with TestClient(app) as c:
        c.post("/ask", json={"question": "What is a Key Facts Statement?"})        # warm the connection pool
        previous, settings.cache_enabled = settings.cache_enabled, False
        try:
            qa, qa_ids = [], []
            for q in QA_QUESTIONS:
                d = c.post("/ask", json={"question": q}).json()
                qa.append(d["latency_ms"]); qa_ids.append(d["trace_id"])
            streams, ttft = [], []
            for q in QA_QUESTIONS[:6]:
                t0, first, done = time.perf_counter(), None, None
                with c.stream("POST", "/ask/stream", json={"question": q + " Answer briefly."}) as r:
                    event = None
                    for line in r.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "token" and first is None:
                            first = (time.perf_counter() - t0) * 1000
                        elif line.startswith("data:") and event == "done":
                            done = json.loads(line[5:])
                streams.append((time.perf_counter() - t0) * 1000)
                ttft.append(first or streams[-1])
            agent, agent_ids, agent_cost = [], [], []
            for q in AGENT_QUESTIONS:
                d = c.post("/ask", json={"question": q, "mode": "agent", "as_of": "2026-10-01"}).json()
                agent.append(d["latency_ms"]); agent_ids.append(d["trace_id"]); agent_cost.append(d["cost_usd"])
        finally:
            settings.cache_enabled = previous
        cached = [c.post("/ask", json={"question": q}).json()["latency_ms"] for q in QA_QUESTIONS]
        metrics = c.get("/metrics").json()
    result = {
        "qa_uncached": {"n": len(qa), "p50_ms": _pct(qa, 0.5), "p95_ms": _pct(qa, 0.95), "max_ms": round(max(qa), 1),
                        "stages": _stages(qa_ids)},
        "qa_cached_exact": {"n": len(cached), "p50_ms": _pct(cached, 0.5), "p95_ms": _pct(cached, 0.95)},
        "qa_stream": {"n": len(streams), "ttft_p50_ms": _pct(ttft, 0.5), "ttft_p95_ms": _pct(ttft, 0.95),
                      "total_p50_ms": _pct(streams, 0.5), "total_p95_ms": _pct(streams, 0.95)},
        "agent_uncached": {"n": len(agent), "per_query_ms": [round(x) for x in agent],
                           "p50_ms": _pct(agent, 0.5), "max_ms": round(max(agent), 1),
                           "cost_per_query_usd": [round(x, 4) for x in agent_cost], "stages": _stages(agent_ids)},
        "service_metrics": metrics,
    }
    (REPORTS_DIR / "stage7_latency.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
