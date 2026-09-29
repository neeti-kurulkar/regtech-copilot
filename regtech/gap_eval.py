"""Stage 3 evaluation of check_policy_gap on aip.evals, with Lab 5 stage-wise diagnosis.

A case names ONE rule (by a verbatim evidence phrase), the company, the topic a user would type,
and the acceptable statuses. The metric finds the report's finding for that rule and asks, in
pipeline order, where it went wrong:

  1. regulation retrieval   - was the rule's passage among the retrieved regulation chunks?
  2. requirement extraction - did the rule become a requirement in the report?
  3. policy retrieval       - (for 'met' cases) did the assessor see the policy wording?
  4. assessment             - was the status right?
"""
from __future__ import annotations

import json
import threading

from aip.cost import Budget
from aip.evals import Case, missing_evidence, run_eval
from aip.guards import normalise_text, quote_in_source

from regtech.entities import resolve
from regtech.index import load_documents
from regtech.paths import EVAL_DIR, REPORTS_DIR
from regtech.policy_gap import PolicyGapChecker

GAP = {"weaker", "missing", "inconsistent"}
TARGETS = {"requirement_found": 0.90, "status_exact": 0.80, "missed_gap": 0.0, "false_alarm": 0.20}


def load_gap_cases() -> list[Case]:
    rows = [json.loads(x) for x in (EVAL_DIR / "gap_cases.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    docs = {r.doc_id: t for r, t in load_documents("regulation")}
    docs |= {r.doc_id: t for r, t in load_documents("internal_policy")}
    labels = {r["id"]: [{"doc_id": r["rule"]["doc_id"], "evidence": e} for e in r["rule"]["evidence"]] for r in rows}
    for r in rows:
        r["policy_doc_id"] = resolve(r["entity"]).doc_id
        if r["policy_evidence"]:
            labels[r["id"] + ":policy"] = [{"doc_id": r["policy_doc_id"], "evidence": r["policy_evidence"]}]
    problems = missing_evidence(labels, docs)
    if problems:
        raise ValueError("gap cases have problems:\n  " + "\n  ".join(problems))
    return [Case(r["id"], {"entity": r["entity"], "topic": r["topic"]}, r, {"expected": "/".join(r["expected"])})
            for r in rows]


def same_rule(evidence: str, quote: str) -> bool:
    return quote_in_source(evidence, quote, 0.6) or quote_in_source(quote, evidence, 0.6)


def target_finding(report: dict, evidence: str | list[str]) -> dict | None:
    """The report's finding for the target rule. A rule may be stated by several passages (e.g. once as
    'capitalisation' and again as 'capitalization'); a finding quoting any of them counts."""
    phrases = [evidence] if isinstance(evidence, str) else evidence
    return next((f for f in report["findings"] if any(same_rule(e, f["regulation"]["quote"]) for e in phrases)), None)


class GapEvaluator:
    def __init__(self, checker: PolicyGapChecker | None = None):
        self.checker = checker or PolicyGapChecker()
        self.policy_text = {c.chunk_id: c.text for c in self.checker.policy_index.chunks}
        self._memo: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def system(self, inp: dict) -> dict:
        key = (inp["entity"], inp["topic"])
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        report = self.checker.check(inp).model_dump()
        with self._lock:
            self._memo[key] = report
        return report

    def rule_retrieved(self, topic: str, evidence: list[str]) -> bool:
        hits = self.checker.reg_index.search(topic, k=self.checker.reg_k)
        return any(normalise_text(e)[:40] in normalise_text(h.text) for e in evidence for h in hits)

    def metric(self, report: dict, exp: dict) -> dict[str, float]:
        f = target_finding(report, exp["rule"]["evidence"])
        m: dict[str, float] = {"requirement_found": float(f is not None),
                               "needs_review_share": sum(x["status"] == "needs_review" for x in report["findings"])
                               / max(len(report["findings"]), 1)}
        expected_gap = exp["expected"][0] in GAP
        # A case whose acceptable labels span 'met' and a gap is ambiguous on the gap/no-gap question.
        ambiguous = "met" in exp["expected"] and any(s in GAP for s in exp["expected"])
        if f is None:
            m["stage_failed_rule_retrieval"] = float(not self.rule_retrieved(exp["topic"], exp["rule"]["evidence"]))
            m["stage_failed_extraction"] = 1.0 - m["stage_failed_rule_retrieval"]
            return m
        status = f["status"]
        m["status_exact"] = float(status in exp["expected"])
        m["status_exact_original_labels"] = float(status in exp.get("original_expected", exp["expected"]))
        if not ambiguous:
            m["gap_call_correct"] = float((status in GAP) == expected_gap and status != "needs_review")
            m["missed_gap"] = float(expected_gap and status == "met")
            m["false_alarm"] = float(not expected_gap and status in GAP)
        if exp["policy_evidence"]:
            seen = any(quote_in_source(exp["policy_evidence"], self.policy_text.get(c, ""), 0.6) for c in f["considered"])
            m["policy_evidence_seen"] = float(seen)
            m["policy_cited_right"] = float(bool(f["policy"]) and
                                            quote_in_source(exp["policy_evidence"], self.policy_text.get(f["policy"]["chunk_id"], ""), 0.6))
            if not m["status_exact"]:
                m["stage_failed_policy_retrieval"] = float(not seen)
                m["stage_failed_assessment"] = float(seen)
        elif not m["status_exact"]:
            m["stage_failed_assessment"] = 1.0
        return m


def diagnose(m: dict[str, float], status: str | None) -> str | None:
    if m.get("stage_failed_rule_retrieval"):
        return "1 regulation retrieval: rule passage not retrieved for this topic"
    if m.get("stage_failed_extraction"):
        return "2 requirement extraction: passage retrieved but rule not extracted"
    if m.get("stage_failed_policy_retrieval"):
        return "3 policy retrieval: policy wording not shown to the assessor"
    if m.get("stage_failed_assessment"):
        return f"4 assessment: called '{status}'"
    return None


def run(label: str = "current", tier: str = "MAIN", assess_tier: str | None = "SMALL") -> dict:
    cases = load_gap_cases()
    ev = GapEvaluator(PolicyGapChecker(tier=tier, assess_tier=assess_tier))
    with Budget(limit_usd=1.0, label=f"stage3-{label}") as b:
        report = run_eval(f"stage3:{label}", cases, ev.system, ev.metric, budget_usd=1.0, progress=False)
    from regtech.gap_report import write_report
    return write_report(label, cases, report, b.as_dict(), ev)
