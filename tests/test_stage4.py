"""Stage 4 (offline): case table, extraction grounding, verdict contract, evaluation metric."""
from datetime import date

import pytest
from pydantic import ValidationError

from regtech.enforcement import CaseRecord, _extraction_schema, load_cases, parse_inr
from regtech.precedent import PrecedentRequest, _lakh, _verdict_schema
from regtech.precedent_eval import load_precedent_cases, metric

RELEASE = ("June 19, 2026 RBI imposes monetary penalty on X Finance Limited. The RBI has, by an order dated June 18, "
           "2026, imposed a monetary penalty of ₹6.20 lakh on X Finance Limited for non-compliance with certain "
           "provisions of 'KYC Directions'. Powers under Section 58G(1)(b) of the Reserve Bank of India Act, 1934. "
           "Charges sustained: i. The company failed to put in place a system of periodic review of risk categorisation.")


def _extraction(**over):
    base = dict(entity="X Finance Limited", order_date="2026-06-18", press_release_date="2026-06-19",
                penalty_text="₹6.20 lakh", statute="Section 58G(1)(b) of the Reserve Bank of India Act, 1934",
                directions=["KYC Directions"], categories=["kyc_aml"],
                charges=["The company failed to put in place a system of periodic review of risk categorisation."])
    base.update(over)
    return base


def test_parse_inr():
    assert parse_inr("₹6.20 lakh") == 620_000 and parse_inr("₹10 lakh") == 1_000_000
    assert parse_inr("₹3,10,000") == 310_000 and parse_inr("Rs. 1.5 crore") == 15_000_000
    assert _lakh(620_000) == "₹6.20 lakh"
    with pytest.raises(ValueError):
        parse_inr("six lakh")


def test_extraction_schema_grounding():
    schema = _extraction_schema(RELEASE, date(2026, 6, 19))
    assert schema.model_validate(_extraction())
    with pytest.raises(ValidationError, match="not verbatim"):
        schema.model_validate(_extraction(charges=["The company ignored KYC rules."]))
    with pytest.raises(ValidationError, match="press_release_date"):
        schema.model_validate(_extraction(press_release_date="2026-06-18"))
    with pytest.raises(ValidationError):
        schema.model_validate(_extraction(penalty_text="₹7 lakh"))


def test_case_table_is_complete_and_consistent():
    cases = load_cases()
    assert len(cases) == 13
    for c in cases.values():
        assert c.penalty_inr == parse_inr(c.penalty_text) and c.charges and c.categories
    assert sum(c.penalty_inr for c in cases.values()) == 7_200_000


def _case(charges):
    return CaseRecord(doc_id="enf-x", entity="X", press_release_date=date(2026, 1, 1), order_date=None,
                      penalty_text="₹1 lakh", penalty_inr=100_000, statute="s", directions=[], charges=charges,
                      categories=["other"])


def test_verdicts_must_cover_every_candidate_once_with_verbatim_charges():
    cands = [_case(["The company failed to pay the surplus amount realised from the auction of pledged gold."]),
             _case(["The company failed to report credit information to CRILC."])]
    schema = _verdict_schema(cands)
    good = {"verdicts": [
        {"candidate": 1, "match": "same_failure", "reason": "same obligation",
         "charge": "The company failed to pay the surplus amount realised from the auction of pledged gold."},
        {"candidate": 2, "match": "unrelated", "reason": "different area", "charge": "anything"}]}
    parsed = schema.model_validate(good)
    assert parsed.verdicts[1].charge is None
    with pytest.raises(ValidationError, match="exactly one verdict"):
        schema.model_validate({"verdicts": good["verdicts"][:1]})
    bad = {"verdicts": [{**good["verdicts"][0], "charge": "The company kept the surplus."}, good["verdicts"][1]]}
    with pytest.raises(ValidationError, match="verbatim"):
        schema.model_validate(bad)


def test_request_contract():
    assert PrecedentRequest(risk="customers never risk-categorised").max_cases == 3
    r = PrecedentRequest(gap={"requirement": "Refund auction surplus within seven working days", "status": "missing"})
    assert "Refund auction surplus" in r.risk_text()
    with pytest.raises(ValidationError):
        PrecedentRequest()
    with pytest.raises(ValidationError):
        PrecedentRequest(gap={"status": "missing"})
    with pytest.raises(ValidationError):
        PrecedentRequest(risk="kyc", unknown=True)


def test_metric_scores_precedents_and_honest_absence():
    out = {"precedents": [{"case_id": "a"}, {"case_id": "c"}], "no_precedent": False, "candidates_considered": ["b", "a"]}
    m = metric(out, ["a", "b"])
    assert m["recall"] == 0.5 and m["precision"] == 0.5 and m["hit_at_1"] == 1.0 and m["baseline_hit_at_1"] == 1.0
    none = {"precedents": [], "no_precedent": True, "candidates_considered": ["b"]}
    m = metric(none, [])
    assert m["correct_no_precedent"] == 1.0 and m["false_precedent"] == 0.0 and m["baseline_false_precedent"] == 1.0


def test_precedent_cases_validate():
    cases = load_precedent_cases()
    kinds = [c.meta["kind"] for c in cases]
    assert kinds.count("no_precedent") == 8 and kinds.count("gap") == 4 and kinds.count("boundary") == 4 and len(cases) == 32


def test_a_failed_judgement_is_not_a_negative_finding(monkeypatch):
    # F12: a structured-output failure used to come back as no_precedent=True
    import regtech.precedent as prec
    from aip.llm import StructuredOutputError

    cases = load_cases()
    first = next(iter(cases.values()))

    class OneCandidate(prec.PrecedentFinder):
        def __init__(self):
            self.cases, self.n_candidates, self.tier = cases, 1, "MAIN"

        def candidates(self, risk):
            return [first]

    def fail(*a, **k):
        raise StructuredOutputError("no valid verdicts")

    monkeypatch.setattr(prec, "structured", fail)
    r = OneCandidate().find({"risk": "a risk the judge cannot decide on"})
    assert r.outcome == "could_not_judge" and r.no_precedent is False and not r.precedents
    m = metric(r.model_dump(mode="json"), [])
    assert m["correct_no_precedent"] == 0.0 and m["could_not_judge"] == 1.0 and m["false_precedent"] == 0.0
