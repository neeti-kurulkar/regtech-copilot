"""Stage 7: the regression gate (Lab 7 Part D). Exits non-zero when any metric breaches ci/thresholds.yml.

    python -m regtech gate                          # measure with the working cache (online if needed)
    python -m regtech gate --record                 # the same, then export exactly the cache entries it used
                                                    # to ci/cache/calls.sqlite3 (the slim cache CI replays)
    AIP_CACHE_DIR=ci/cache AIP_OFFLINE=1 python -m regtech gate     # what CI runs: no key, no cost

Three golden sets, each the system as it is actually served:
    qa        Stage 2's 31 questions through the service's Q&A configuration (REGTECH_QA_TIER, default SMALL)
    gap       Stage 3's 16 labelled gap cases through check_policy_gap
    redteam   Stage 6's 23 attacks and controls through the agent's default layers

Replayed offline, calls cost $0 and take 0 ms, which would make a cost or latency gate meaningless. So cost
is priced from the recorded tokens (Budget.cold_usd) and latency is the recorded model time per query
(Budget.cold_latency_ms: query embedding + generation + any repair, as measured when the cache was
recorded; retrieval itself is a few ms of local compute). Any change that alters a model request is a
cache miss offline, and the gate fails closed: unrecorded behaviour has not been evaluated.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import yaml

from aip import cache
from aip.cost import Budget

from regtech.paths import REPO_ROOT, REPORTS_DIR

CONFIG = REPO_ROOT / "ci" / "thresholds.yml"
SLIM_CACHE = REPO_ROOT / "ci" / "cache" / "calls.sqlite3"


def _mean(results, key: str) -> float:
    vals = [r.metrics[key] for r in results if key in r.metrics and r.metrics[key] == r.metrics[key]]
    return sum(vals) / len(vals) if vals else float("nan")


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else float("nan")


def measure_qa(index=None) -> dict[str, float]:
    from aip.evals import refusal_metrics, run_eval

    from regtech.qa import RegulationQA
    from regtech.qa_eval import (_as_output, _judge_phase, citation_support_summary, deterministic_metrics,
                                 load_cases, summarise)
    from regtech.service import QA_TIER

    cases = load_cases()
    qa = RegulationQA(index=index, tier=QA_TIER)
    cold: dict[str, tuple[float, float]] = {}

    def system(q: str) -> dict:
        with Budget(limit_usd=0.2, label="gate.qa.case") as b:
            out = _as_output(qa.ask(q))
        cold[q] = (b.cold_latency_ms, b.cold_usd)
        return out

    answers = run_eval("gate:qa", cases, system, deterministic_metrics, budget_usd=1.0, progress=False)
    outputs = {r.id: r.output for r in answers.results}
    res = {"answers": answers, "judge": _judge_phase("gate:qa:judge", cases, outputs),
           "refusal": refusal_metrics([o["refused"] for o in outputs.values()],
                                      [c.expected["should_refuse"] for c in cases])}
    m = summarise(res, cases)
    graded = [r for r in answers.results if "evidence_in_context" in r.metrics]
    sup = citation_support_summary([r.metrics for r in answers.results])
    return {"qa_correctness": m["correctness"], "qa_faithfulness": m["faithfulness"],
            "qa_citation_validity": m["citation_validity"], "qa_refusal_recall": m["refusal_recall"],
            "qa_refusal_precision": m["refusal_precision"],
            "qa_evidence_in_context": sum(r.metrics["evidence_in_context"] for r in graded) / max(len(graded), 1),
            "qa_cost_per_query_usd": sum(c for _, c in cold.values()) / max(len(cold), 1),
            "qa_p95_model_latency_ms": _pct([lat for lat, _ in cold.values()], 0.95),
            # citation SUPPORT, sentence level (qa_citation_validity only proves each [n] exists)
            "qa_uncited_claim_rate": sup["uncited_claim_rate"],
            "qa_numeric_claim_support": sup["numeric_claim_support"],
            "qa_support_claims": sup["claims"], "qa_support_numeric_claims": sup["numeric_claims"]}


def measure_gap() -> dict[str, float]:
    from aip.evals import run_eval

    from regtech.gap_eval import GapEvaluator, load_gap_cases

    cases = load_gap_cases()
    ev = GapEvaluator()
    with Budget(limit_usd=1.0, label="gate.gap") as b:
        rep = run_eval("gate:gap", cases, ev.system, ev.metric, budget_usd=1.0, progress=False)
    checks = len({(c.input["entity"], c.input["topic"]) for c in cases})
    return {"gap_requirement_found": _mean(rep.results, "requirement_found"),
            "gap_status_exact": _mean(rep.results, "status_exact"),
            "gap_missed_gap": _mean(rep.results, "missed_gap"),
            "gap_false_alarm": _mean(rep.results, "false_alarm"),
            "gap_cost_per_check_usd": b.cold_usd / max(checks, 1)}


def measure_redteam() -> dict[str, float]:
    from regtech.agent import DEFAULT_LAYERS
    from regtech.redteam import run_suite, summarise

    # sequential: parallel cases embed identical sanitised uploads at the same moment, and an embedding race
    # can store a different vector than the run used, which breaks exact replay
    s = summarise(run_suite([DEFAULT_LAYERS], workers=1)[DEFAULT_LAYERS.label])
    return {"redteam_block_rate": s["block_rate"], "redteam_false_positive_rate": s["false_positive_rate"],
            "redteam_privileged_calls": float(s["privileged_calls_by_attacks"]),
            "agent_cost_per_query_usd": s["mean_cold_cost_usd"]}


def measure() -> dict[str, float]:
    from regtech.paths import eval_split
    if eval_split() != "dev":
        raise SystemExit("the gate runs on the dev sets its thresholds were set on; unset REGTECH_EVAL_SPLIT")
    if os.getenv("AIP_CACHE_SALT"):
        raise SystemExit("unset AIP_CACHE_SALT: the gate must use the same cache keys CI replays")
    from regtech.index import CorpusIndex
    t0 = time.perf_counter()
    metrics = {**measure_qa(CorpusIndex("regulation")), **measure_gap(), **measure_redteam()}
    metrics["gate_runtime_s"] = round(time.perf_counter() - t0, 1)
    return metrics


def check(metrics: dict[str, float], thresholds: dict) -> list[str]:
    failures = []
    width = max(len(k) for k in thresholds)
    print(f"{'metric':<{width}}  {'value':>10}  {'gate':>12}  status")
    print("-" * (width + 38))
    for name, rule in thresholds.items():
        value = metrics.get(name)
        if value is None or value != value:
            failures.append(f"{name}: not measured")
            print(f"{name:<{width}}  {'-':>10}  {'':>12}  MISSING")
            continue
        ok, gate = True, ""
        if "min" in rule:
            gate, ok = f">= {rule['min']}", value >= rule["min"]
        if "max" in rule and ok:
            gate, ok = f"<= {rule['max']}", value <= rule["max"]
        if not ok:
            failures.append(f"{name}: {value:.4f} violates {gate}")
        print(f"{name:<{width}}  {value:>10.4f}  {gate:>12}  {'ok' if ok else 'FAIL'}")
    return failures


def main(config: Path = CONFIG, record: bool = False) -> int:
    thresholds = yaml.safe_load(Path(config).read_text(encoding="utf-8"))
    try:
        metrics = measure()
    except cache.CacheMiss as exc:
        print("GATE FAILED: a model request is not in the committed cache, so this behaviour has never been "
              "evaluated. Re-record with `python -m regtech gate --record` (online) and commit ci/cache.\n"
              f"{str(exc)[:400]}")
        return 2
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "stage7_gate.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    failures = check(metrics, thresholds)
    if record:
        n = cache.export(cache.accessed_keys(), SLIM_CACHE)
        print(f"\nrecorded {n} cache entries to {SLIM_CACHE.relative_to(REPO_ROOT)} "
              f"({SLIM_CACHE.stat().st_size / 1e6:.1f} MB)")
    if failures:
        print("\nGATE FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
