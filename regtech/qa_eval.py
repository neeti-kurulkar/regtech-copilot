"""Stage 2 evaluation, Lab 4 method, on aip.evals.

Phase 1  run_eval(system = RegulationQA)       -> answers + free deterministic checks,
         cost/latency of the system alone.
Phase 2  run_eval(system = stored answers)     -> LLM judges (faithfulness, correctness),
         cost of judging reported separately.
Gold     the same generator on the gold passages (Lab 4 E2 decomposition).
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import time
from dataclasses import asdict

from aip.cost import Budget
from aip.evals import (JUDGE_RUBRIC_CORRECTNESS, JUDGE_RUBRIC_FAITHFULNESS, Case, EvalReport,
                       evidence_labels, judge_agreement, llm_judge, missing_evidence, normalise_text,
                       refusal_metrics, run_eval)
from aip.retrieval import Hit

from regtech.index import load_documents
from regtech.paths import EVAL_DIR, REPORTS_DIR
from regtech.qa import Answer, RegulationQA

CALIBRATION_CSV = REPORTS_DIR / "stage2_judge_calibration.csv"
JUDGE_SCORES_JSON = REPORTS_DIR / "stage2_judge_scores.json"

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CITE = re.compile(r"\[\d+\]")


def load_cases() -> list[Case]:
    rows = [json.loads(x) for x in (EVAL_DIR / "qa_regulation.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    docs = {r.doc_id: t for r, t in load_documents("regulation")}
    problems = missing_evidence({r["id"]: r["relevant"] for r in rows}, docs)
    if problems:
        raise ValueError("qa set has problems:\n  " + "\n  ".join(problems))
    return [Case(r["id"], r["question"],
                 {"reference": r["reference"], "relevant": r["relevant"], "should_refuse": r["kind"] == "unanswerable"},
                 {"kind": r["kind"]}) for r in rows]


def _as_output(a: Answer) -> dict:
    return asdict(a)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(_CITE.sub(" ", text))}


def deterministic_metrics(out: dict, expected: dict) -> dict[str, float]:
    m = {"citation_valid": float(out["citations_valid"]), "refused": float(out["refused"]),
         "repaired": float(out["repairs"] > 0), "fell_back": float(out["fallback"]),
         "budget_doubled": float(out.get("budget_doublings", 0) > 0)}
    rel = expected["relevant"]
    if rel:
        ctx = [(s["doc_id"], s["text"]) for s in out["sources"]]
        labels, ids = evidence_labels(ctx, rel)
        m["evidence_in_context"] = float(any(lab in ids for lab in labels))
        cited = set(out["cited"])
        m["evidence_cited"] = float(any(lab in ids and (i + 1) in cited for i, lab in enumerate(labels)))
    if not out["refused"]:
        nums = _numbers(out["text"])
        if nums:
            cited_text = " ".join(f"{s['label']} {s['text']}" for s in out["sources"] if s["n"] in out["cited"])
            m["numbers_supported"] = len(nums & _numbers(cited_text)) / len(nums)
    return m


def _context_for_judge(out: dict) -> str:
    return "\n\n".join(f"[{s['n']}] ({s['label']})\n{s['text']}" for s in out["sources"])


def judge_metrics(out: dict, expected: dict, question: str, kind: str) -> dict[str, float]:
    m: dict[str, float] = {}
    f = llm_judge(JUDGE_RUBRIC_FAITHFULNESS.format(context=_context_for_judge(out), answer=out["text"]))
    if f.get("parse_error"):
        m["judge_parse_error"] = 1.0
    else:
        m["faithfulness"] = float(f.get("score", 0))
    if kind != "unanswerable":
        c = llm_judge(JUDGE_RUBRIC_CORRECTNESS.format(question=question, reference=expected["reference"],
                                                      candidate=out["text"]))
        if c.get("parse_error"):
            m["judge_parse_error"] = 1.0
        else:
            m["correctness"] = float(c.get("score", 0)) / 2.0
    return m


def gold_hits(qa: RegulationQA, case: Case) -> list[Hit]:
    """What perfect retrieval would supply: the chunk holding each gold evidence phrase plus its
    neighbours in the same document (a clause or table often runs across a window boundary).

    A chunk containing the *whole* phrase is preferred; the 60% partial match is only a fallback,
    because a partial match can land on similar wording elsewhere in the same Direction.
    """
    chunks = qa.index.chunks
    picked: list[int] = []
    for rel in case.expected["relevant"]:
        phrase = normalise_text(rel["evidence"])
        exact = [i for i, c in enumerate(chunks) if c.doc_id == rel["doc_id"] and phrase in normalise_text(c.text)]
        partial = [i for i, c in enumerate(chunks)
                   if (lambda labs: labs[0][0] in labs[1])(evidence_labels([(c.doc_id, c.text)], [rel]))]
        centre = (exact or partial or [None])[0]
        if centre is None:
            continue
        for j in (centre - 1, centre, centre + 1):
            if 0 <= j < len(chunks) and chunks[j].doc_id == rel["doc_id"] and j not in picked:
                picked.append(j)
    return [Hit(chunks[j], 1.0, "gold", r) for r, j in enumerate(sorted(picked))]


def _judge_phase(name: str, cases: list[Case], outputs: dict[str, dict]) -> EvalReport:
    by_q = {c.input: c for c in cases}
    return run_eval(name, cases, system=lambda q: outputs[by_q[q].id],
                    metric=lambda out, exp: judge_metrics(out, exp, out["question"],
                                                          next(c.meta["kind"] for c in cases if c.expected is exp)),
                    budget_usd=1.0, progress=False)


def run_variant(variant: str, cases: list[Case], judge: bool = True, tier: str = "MAIN") -> dict:
    qa = RegulationQA(variant, tier=tier)
    answers = run_eval(f"stage2:{variant}", cases, system=lambda q: _as_output(qa.ask(q)),
                       metric=deterministic_metrics, budget_usd=0.5, progress=False)
    outputs = {r.id: r.output for r in answers.results}
    result = {"variant": variant, "answers": answers, "outputs": outputs,
              "refusal": refusal_metrics([o["refused"] for o in outputs.values()],
                                         [c.expected["should_refuse"] for c in cases])}
    if judge:
        result["judge"] = _judge_phase(f"stage2:{variant}:judge", cases, outputs)
    return result


def run_gold(cases: list[Case]) -> dict:
    qa = RegulationQA("balanced")
    answerable = [c for c in cases if c.expected["relevant"]]
    gold = {c.input: gold_hits(qa, c) for c in answerable}
    answers = run_eval("stage2:gold-context", answerable, system=lambda q: _as_output(qa.ask_with_context(q, gold[q])),
                       metric=deterministic_metrics, budget_usd=0.5, progress=False)
    outputs = {r.id: r.output for r in answers.results}
    return {"answers": answers, "outputs": outputs, "judge": _judge_phase("stage2:gold:judge", answerable, outputs)}


def merged(res: dict) -> dict[str, dict[str, float]]:
    """Per-case metrics from both phases."""
    out = {r.id: dict(r.metrics) for r in res["answers"].results}
    for r in res.get("judge", EvalReport("", [], {})).results:
        out.setdefault(r.id, {}).update(r.metrics)
    return out


def mean(values) -> float:
    vals = [v for v in values if v is not None]
    return statistics.fmean(vals) if vals else float("nan")


def failure_mode(m: dict[str, float], kind: str, refused: bool) -> str | None:
    """Lab 4 E3 / Lab 5: which of the seven RAG stages failed, from free signals."""
    if kind == "unanswerable":
        return None if refused else "answered an unanswerable question (generation / refusal)"
    if m.get("correctness", 1.0) >= 1.0:
        return None
    if refused:
        return "over-refusal (generation)" if m.get("evidence_in_context") else "over-refusal after retrieval miss"
    if not m.get("evidence_in_context"):
        return "retrieval: gold passage not in context (stage 3/4)"
    if not m.get("evidence_cited"):
        return "generation: gold passage in context but not used (stage 6)"
    return "generation: right passage cited, answer incomplete (stage 6/7)"


def write_calibration_sheet(cases: list[Case], res: dict, n: int = 20) -> None:
    """Lab 4 D2: hand-label BEFORE looking at the judge. The judge's scores go to a separate file."""
    pick = [c for c in cases if c.meta["kind"] != "unanswerable"][:n]
    per_case = merged(res)
    REPORTS_DIR.mkdir(exist_ok=True)
    if not CALIBRATION_CSV.exists():
        with CALIBRATION_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "question", "reference", "answer", "cited_sources",
                        "human_faithfulness_0_or_1", "human_correctness_0_1_2"])
            for c in pick:
                o = res["outputs"][c.id]
                cited = "\n---\n".join(f"[{s['n']}] {s['label']}\n{s['text'][:700]}" for s in o["sources"] if s["n"] in o["cited"])
                w.writerow([c.id, c.input, c.expected["reference"], o["text"], cited, "", ""])
    JUDGE_SCORES_JSON.write_text(json.dumps(
        {c.id: {"faithfulness": per_case[c.id].get("faithfulness"),
                "correctness_0_1_2": None if per_case[c.id].get("correctness") is None
                else int(round(per_case[c.id]["correctness"] * 2))} for c in pick}, indent=2), encoding="utf-8")


def judge_kappa() -> dict | None:
    """Cohen's kappa between the judge and the human labels in the calibration sheet (aip.evals.judge_agreement)."""
    if not CALIBRATION_CSV.exists() or not JUDGE_SCORES_JSON.exists():
        return None
    judge = json.loads(JUDGE_SCORES_JSON.read_text(encoding="utf-8"))
    with CALIBRATION_CSV.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)]
    out = {}
    for crit, col, key in (("faithfulness", "human_faithfulness_0_or_1", "faithfulness"),
                           ("correctness", "human_correctness_0_1_2", "correctness_0_1_2")):
        pairs = [(judge[r["id"]][key], float(r[col])) for r in rows
                 if r[col].strip() and judge.get(r["id"], {}).get(key) is not None]
        if len(pairs) >= 10:
            out[crit] = judge_agreement([float(a) for a, _ in pairs], [b for _, b in pairs])
    return out or None


def latency_probe(cases: list[Case], n: int = 8, tier: str = "MAIN") -> dict | None:
    """End-to-end latency of a warm system, one request at a time, with the aip cache OFF.

    The answer phase runs 4 workers in parallel (queueing inflates latency) and, on any re-run,
    is served from the cache (latency ~0). Neither is what a user waits for. Skipped offline.
    """
    from aip.config import settings

    if settings.offline:
        return None
    qa = RegulationQA("balanced", tier=tier)
    questions = [c.input for c in cases if c.meta["kind"] == "answerable"][:n]
    times = []
    previous, settings.cache_enabled = settings.cache_enabled, False
    try:
        # Warm-up must reach the network (cache is already off here): a cached warm-up leaves
        # the first timed request paying the cold start (measured once at 22 s vs ~5 s warm).
        qa.ask("What is a Key Facts Statement?")
        with Budget(limit_usd=0.2, label="stage2-latency-probe") as b:
            for q in questions:
                t0 = time.perf_counter()
                qa.ask(q)
                times.append((time.perf_counter() - t0) * 1000)
    finally:
        settings.cache_enabled = previous
    times.sort()
    return {"n": len(times), "p50_ms": times[len(times) // 2], "p95_ms": times[min(len(times) - 1, round(0.95 * (len(times) - 1)))],
            "max_ms": times[-1], "cost_per_query_usd": b.spent_usd / len(times)}


def main(judge: bool = True) -> dict:
    cases = load_cases()
    with Budget(limit_usd=1.5, label="stage2-eval") as total:
        balanced = run_variant("balanced", cases, judge=judge)
        strict = run_variant("strict", cases, judge=False)
        gold = run_gold(cases) if judge else None
        balanced["latency_probe"] = latency_probe(cases)
    if judge:
        write_calibration_sheet(cases, balanced)
    from regtech.qa_report import write_report
    write_report(cases, balanced, strict, gold, judge_kappa(), total.as_dict())
    return {"balanced": balanced, "strict": strict, "gold": gold}


def summarise(res: dict, cases: list[Case]) -> dict[str, float]:
    """The headline Stage 2 metrics of one run, as a flat dict (used by the tier check and the Stage 7 gate)."""
    per = merged(res)
    answerable = [c.id for c in cases if c.meta["kind"] == "answerable"]
    out = {"citation_validity": mean(per[i]["citation_valid"] for i in per),
           "faithfulness": mean(per[i].get("faithfulness") for i in per),
           "correctness": mean(per[i].get("correctness") for i in answerable if i in per),
           "refusal_recall": res["refusal"]["refusal_recall"], "refusal_precision": res["refusal"]["refusal_precision"],
           "repair_rate": mean(per[i]["repaired"] for i in per)}
    b = res["answers"].budget
    out["cold_cost_per_query_usd"] = b.get("cold_cost_usd", 0.0) / max(len(per), 1)
    return out


def tier_check(tier: str, label: str) -> dict:
    """Stage 7: would a cheaper/faster tier hold Stage 2's quality? Answers + judge + a serial latency probe.
    Writes reports/stage7_qa_tier_<label>.json; does not touch the Stage 2 report."""
    import json

    from regtech.paths import REPORTS_DIR
    cases = load_cases()
    res = run_variant("balanced", cases, judge=True, tier=tier)
    out = {"tier": tier, "label": label, **summarise(res, cases), "latency_probe": latency_probe(cases, tier=tier)}
    (REPORTS_DIR / f"stage7_qa_tier_{label}.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out
