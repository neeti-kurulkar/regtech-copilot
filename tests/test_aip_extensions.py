"""Tests for the RegTech additions to aip (see aip/CHANGELOG_REGTECH.md)."""
import numpy as np
import pytest

from aip import retry
from aip.chunking import STRATEGIES, Chunk, annotate_provenance, markdown_chunks, strip_heading_prefix, whole_chunks
from aip.evals import Case, CaseResult, EvalReport, evidence_labels, evidence_retrieval_metrics, missing_evidence
from aip.retrieval import Bm25Retriever, _top_k

MD = "# Title\n\n## Chapter I\n\n### A. Scope\n\n1. First rule applies here.\n\n2. Second rule, with more words in it.\n"


# --- chunking ---------------------------------------------------------------
def test_markdown_prefix_flag_keeps_heading_in_meta():
    with_prefix = markdown_chunks(MD, "d", size=400)
    without = markdown_chunks(MD, "d", size=400, prefix=False)
    assert with_prefix[0].text.startswith("[") and not without[0].text.startswith("[")
    assert [c.meta["heading"] for c in with_prefix] == [c.meta["heading"] for c in without]
    assert strip_heading_prefix(with_prefix[0].text) == without[0].text


def test_new_strategies_registered():
    assert {"whole", "markdown_noprefix"} <= set(STRATEGIES)
    assert len(whole_chunks(MD, "d")) == 1 and whole_chunks("  ", "d") == []


def test_annotate_provenance_on_a_fixed_chunk():
    chunks = STRATEGIES["sliding"](MD, "d", size=60, overlap=10)
    annotate_provenance(MD, chunks)
    last = chunks[-1]
    assert last.meta["heading"] == "Title > Chapter I > A. Scope"
    expected = "2" if last.meta["start"] >= MD.index("2. Second") else "1"
    assert last.meta["number"] == expected and last.meta["start"] > MD.index("1. First")


def test_annotate_provenance_can_skip_numbers():
    chunks = annotate_provenance(MD, STRATEGIES["sliding"](MD, "d", size=60, overlap=10), number_pattern=None)
    assert all(c.meta["number"] == "" for c in chunks)


# --- retrieval ----------------------------------------------------------------
def test_top_k_respects_filter_and_shrinks_k():
    chunks = [Chunk("a", "d1", "1"), Chunk("b", "d2", "2"), Chunk("c", "d1", "3")]
    top = _top_k(np.array([0.9, 0.8, 0.1]), chunks, k=5, chunk_filter=lambda c: c.doc_id == "d1")
    assert list(top) == [0, 2]


def test_bm25_chunk_filter():
    chunks = [Chunk("gold auction notice newspapers", "a", "a1"), Chunk("gold auction rules", "b", "b1")]
    hits = Bm25Retriever(chunks).search("gold auction", k=5, chunk_filter=lambda c: c.doc_id == "b")
    assert [h.doc_id for h in hits] == ["b"]


# --- evals --------------------------------------------------------------------
def test_evidence_labels_credit_once_and_keep_ranks():
    rel = [{"doc_id": "reg", "evidence": "within 21 days from the date"}]
    ranked = [("kyc", "unrelated"), ("reg", "conveyed WITHIN 21 days from the date of receipt"),
              ("reg", "within 21 days from the date again")]
    labels, ids = evidence_labels(ranked, rel)
    assert labels[1] == ids[0] and labels[0].startswith("_x") and labels[2].startswith("_x")
    assert evidence_retrieval_metrics(ranked, rel)["mrr"] == pytest.approx(0.5)


def test_evidence_partial_boundary_match_counts():
    ev = "periodic updation at least once in every two years for high-risk customers"
    labels, ids = evidence_labels([("kyc", "shall carry out periodic updation at least once in every two")],
                                  [{"doc_id": "kyc", "evidence": ev}])
    assert labels == ids


def test_doc_level_labels_dedupe_documents():
    ranked = [("a", "x"), ("a", "y"), ("c", "z"), ("b", "w")]
    labels, ids = evidence_labels(ranked, [{"doc_id": "a"}, {"doc_id": "b"}])
    assert labels == [ids[0], "_x:c", ids[1]]


def test_missing_evidence_reports_typos_and_unknown_docs():
    docs = {"reg": "The KFS shall have a validity period of at least three working days."}
    problems = missing_evidence({"Q1": [{"doc_id": "reg", "evidence": "validity period of three days"}],
                                 "Q2": [{"doc_id": "nope"}],
                                 "Q3": [{"doc_id": "reg", "evidence": "VALIDITY  period of at least"}]}, docs)
    assert len(problems) == 2 and problems[0].startswith("Q1") and problems[1].startswith("Q2")


def test_report_breakdown_and_latency():
    cases = [Case("1", None, meta={"kind": "a"}), Case("2", None, meta={"kind": "a"}), Case("3", None, meta={"kind": "b"})]
    rep = EvalReport("x", [CaseResult("1", metrics={"mrr": 1.0}, latency_ms=10),
                           CaseResult("2", metrics={"mrr": 0.5}, latency_ms=30),
                           CaseResult("3", metrics={"mrr": 0.0}, latency_ms=20)], {})
    assert rep.breakdown(cases, "kind", "mrr") == {"a": 0.75, "b": 0.0}
    assert rep.latency_percentile(50) == 20


# --- cost: context-local budgets ------------------------------------------------
def test_parallel_budgets_do_not_count_each_others_calls():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from aip import cost

    barrier = threading.Barrier(2)

    def task(n_calls):
        with cost.Budget(limit_usd=1.0, label=f"task{n_calls}") as b:
            barrier.wait()  # both budgets are open at the same time
            for _ in range(n_calls):
                cost.record(cost.Usage("local/x", 10, 0, 0.0, 1.0))
            barrier.wait()
        return b.calls

    with cost.Budget(limit_usd=1.0, label="outer") as outer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert cost.map_in_context(pool, task, [2, 5]) == [2, 5]
    assert outer.calls == 7  # the caller's budget still sees all work done in the pool


# --- retry --------------------------------------------------------------------
def test_retry_recovers_from_transient_error(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("read operation timed out")
        return "ok"

    assert retry.call_with_retries(flaky, event="test.retry", max_retries=4) == "ok" and calls["n"] == 3


def test_retry_does_not_retry_permanent_errors():
    with pytest.raises(ValueError):
        retry.call_with_retries(lambda: (_ for _ in ()).throw(ValueError("bad request")), event="t")


# --- guards: figure and date grounding (Stage 5) -------------------------------------
def test_number_and_date_grounding():
    from datetime import date

    from aip.guards import date_in_text, number_forms, number_in_text
    assert {"21", "twenty-one", "twenty one"} <= number_forms(21) and "one hundred and eighty" in number_forms(180)
    assert number_in_text(7, "a maximum period of seven working days") and not number_in_text(4, "within 14 days")
    assert date_in_text(date(2026, 3, 31), "By March 31, 2026") and date_in_text(date(2026, 3, 31), "31.03.2026")
    assert not date_in_text(date(2026, 3, 31), "By March 31, 2025")
