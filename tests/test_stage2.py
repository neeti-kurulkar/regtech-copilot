"""Stage 2: aip.rag validate/repair/fail-closed, refusal metrics, and the regtech Q&A helpers (all offline)."""
import pytest

import aip.rag as rag
from aip.chunking import Chunk, annotate_provenance
from aip.evals import refusal_metrics
from aip.rag import REFUSAL, RagPipeline, is_refusal, validate_answer
from aip.retrieval import Hit, format_context

from regtech.qa import paragraph_range, short_title, source_label
from regtech.qa_eval import deterministic_metrics, failure_mode


# --- aip.rag -------------------------------------------------------------------
def test_validate_answer_rules():
    assert validate_answer("Within 21 days [1].", 3) == []
    assert validate_answer(REFUSAL, 3) == []
    assert "do not exist" in validate_answer("Within 21 days [4].", 3)[0]
    assert "no citations" in validate_answer("Within 21 days.", 3)[0]
    assert "cut off" in validate_answer("Within 21 [1]", 3, finish_reason="length")[0]
    assert validate_answer("   ", 3) == ["the answer was empty"]


def test_partial_answer_is_not_a_refusal():
    assert is_refusal(REFUSAL)
    assert not is_refusal("The tenure is three years [1]. The sources do not state a salary.")


class _FakeRetriever:
    def __init__(self, n=2):
        self.hits = [Hit(Chunk(f"passage {i}", f"d{i}", f"d{i}::0"), 1.0, "fake", i) for i in range(n)]

    def search(self, q, k=8):
        return self.hits[:k]


def _script(monkeypatch, replies):
    """replies: str, or (text, finish_reason, expected_max_tokens) to also check the budget."""
    calls = []

    def fake_chat(messages, **kw):
        calls.append(messages)
        reply = replies[len(calls) - 1]
        if isinstance(reply, tuple):
            text, finish, max_tokens = reply
            assert kw["max_tokens"] == max_tokens
            return {"text": text, "finish_reason": finish}
        return {"text": reply, "finish_reason": "stop"}

    monkeypatch.setattr(rag, "chat", fake_chat)
    return calls


def test_truncation_doubles_budget_instead_of_repairing(monkeypatch):
    calls = _script(monkeypatch, [("Answer [1", "length", 600), ("Answer [1].", "stop", 1200)])
    ans = RagPipeline(_FakeRetriever(), max_repairs=1, fail_closed=True).answer("q")
    assert ans.answer == "Answer [1]." and ans.budget_doublings == 1 and ans.repairs == 0
    assert len(calls) == 2 and len(calls[1]) == 1  # same request, not a corrective turn


def test_repair_fixes_an_invented_citation(monkeypatch):
    calls = _script(monkeypatch, ["Answer [7].", "Answer [1]."])
    ans = RagPipeline(_FakeRetriever(), max_repairs=1, fail_closed=True).answer("q")
    assert ans.answer == "Answer [1]." and ans.repairs == 1 and not ans.fallback and ans.citations_valid
    corrective = calls[1][-1]["content"]
    assert "do not exist" in corrective and calls[1][-2]["content"] == "Answer [7]."


def test_fail_closed_after_failed_repair(monkeypatch):
    _script(monkeypatch, ["Answer [7].", "Still [9]."])
    ans = RagPipeline(_FakeRetriever(), max_repairs=1, fail_closed=True).answer("q")
    assert ans.answer == REFUSAL and ans.fallback and ans.refused and ans.problems


def test_defaults_keep_original_behaviour(monkeypatch):
    _script(monkeypatch, ["Answer [7]."])
    ans = RagPipeline(_FakeRetriever()).answer("q")
    assert ans.answer == "Answer [7]." and not ans.citations_valid and ans.repairs == 0 and not ans.fallback


def test_answer_from_hits_skips_retrieval(monkeypatch):
    _script(monkeypatch, ["Gold answer [1]."])
    gold = [Hit(Chunk("gold passage", "g", "g::0"), 1.0, "gold", 0)]
    ans = RagPipeline(_FakeRetriever()).answer_from_hits("q", gold)
    assert ans.hits == gold and ans.cited_indices == [1]


def test_format_context_label():
    h = Hit(Chunk("text", "reg-kyc", "reg-kyc::s1"), 1.0)
    assert "(source: KYC | para 5)" in format_context([h], label=lambda _: "KYC | para 5")
    assert "(source: reg-kyc)" in format_context([h])


# --- provenance numbers ----------------------------------------------------------
def test_annotate_provenance_lists_every_paragraph_touched():
    md = "# T\n\n## A\n\n17. First.\n\n18. Second.\n\n19. Third.\n"
    c = Chunk("17. First.\n\n18. Second.\n\n19. Third.", "d", "d::0")
    annotate_provenance(md, [c])
    assert c.meta["numbers"] == ["17", "18", "19"]


# --- aip.evals.refusal_metrics ------------------------------------------------------
def test_refusal_metrics_counts():
    m = refusal_metrics([True, True, False, True], [True, False, False, True])
    assert m["refusal_recall"] == 1.0 and m["refusal_precision"] == pytest.approx(2 / 3)
    assert m["over_refusals"] == 1 and m["should_refuse_total"] == 2


# --- regtech.qa helpers ---------------------------------------------------------------
def test_short_title_and_paragraph_range():
    t = "Reserve Bank of India (Non-Banking Financial Companies – Know Your Customer) Directions, 2025"
    assert short_title(t) == "Know Your Customer Directions, 2025"
    assert paragraph_range([]) == "" and paragraph_range(["19"]) == "para 19" and paragraph_range(["17", "20"]) == "paras 17-20"


def test_source_label_uses_title_path_and_paragraphs():
    c = Chunk("x", "reg-rbc", "reg-rbc::s18", {
        "title": "Reserve Bank of India (Non-Banking Financial Companies – Responsible Business Conduct) Directions, 2025",
        "heading": "Reserve Bank of India (NBFC – RBC) Directions, 2025 > Chapter III – Lending > A. Fair Practices Code > A.4 General",
        "numbers": ["18", "19", "20"]})
    assert source_label(Hit(c, 1.0)) == ("Responsible Business Conduct Directions, 2025 | "
                                          "A. Fair Practices Code > A.4 General | paras 18-20")


def _out(text, cited, sources, refused=False):
    return {"text": text, "cited": cited, "sources": sources, "refused": refused,
            "citations_valid": True, "repairs": 0, "fallback": False}


def test_deterministic_metrics_evidence_and_numbers():
    sources = [{"n": 1, "doc_id": "reg-rbc", "label": "RBC | para 19", "text": "shall be conveyed within 21 days from the date of receipt of request"},
               {"n": 2, "doc_id": "reg-kyc", "label": "KYC", "text": "unrelated"}]
    exp = {"relevant": [{"doc_id": "reg-rbc", "evidence": "conveyed within 21 days from the date of receipt"}]}
    m = deterministic_metrics(_out("Within 21 days, per para 19 [1].", [1], sources), exp)
    assert m["evidence_in_context"] == 1.0 and m["evidence_cited"] == 1.0 and m["numbers_supported"] == 1.0
    m = deterministic_metrics(_out("Within 30 days [2].", [2], sources), exp)
    assert m["evidence_cited"] == 0.0 and m["numbers_supported"] == 0.0


def test_failure_mode_classification():
    assert failure_mode({"correctness": 1.0}, "answerable", False) is None
    assert failure_mode({"correctness": 0.5, "evidence_in_context": 0.0}, "answerable", False).startswith("retrieval")
    assert failure_mode({"correctness": 0.0, "evidence_in_context": 1.0}, "answerable", True).startswith("over-refusal")
    assert failure_mode({}, "unanswerable", False).startswith("answered an unanswerable")
    assert failure_mode({}, "unanswerable", True) is None


def test_refusal_is_exact_not_a_prefix():
    # F2: the refusal sentence followed by claims used to count as a refusal and skip the citation check
    assert is_refusal(f'"{REFUSAL}"') and is_refusal(REFUSAL.upper().rstrip("."))
    smuggled = REFUSAL + " However, NBFCs must report fraud within 3 days."
    assert not is_refusal(smuggled)
    assert validate_answer(smuggled, 6), "refusal + uncited claims must be sent back for repair"


def test_citation_support_is_sentence_level():
    from aip.guards import citation_support
    c = citation_support("The CCO tenure is 3 years [1]. The CCO must be paid Rs 1 crore. "
                         "The sources do not specify a minimum salary.", {1: "a minimum tenure of three years"})
    assert c == {"claims": 2, "uncited": 1, "with_numbers": 1, "numbers_unsupported": 0}
    bad = citation_support("Gold loans need 50 per cent LTV [2].", {1: "50 per cent", 2: "75 per cent"})
    assert bad["numbers_unsupported"] == 1, "a number must be in the source the sentence itself cites"
