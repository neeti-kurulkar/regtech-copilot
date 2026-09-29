"""Stage 3 (offline): contracts, entity resolution, grounding validators, evaluation plumbing."""
import pytest
from pydantic import ValidationError

from aip.guards import quote_in_source
from regtech.entities import EntityNotFound, resolve
from regtech.gap_eval import diagnose, load_gap_cases, same_rule, target_finding
from regtech.policy_gap import (Citation, GapFinding, GapReport, PolicyGapRequest, _assessment_schema,
                                _requirement_schema)


# --- contract ---------------------------------------------------------------------------
def test_request_needs_a_policy_and_a_sane_topic():
    assert PolicyGapRequest(entity="IIFL", topic="gold loan auctions").max_requirements == 8
    with pytest.raises(ValidationError):
        PolicyGapRequest(topic="gold loan auctions")
    with pytest.raises(ValidationError):
        PolicyGapRequest(entity="IIFL", topic="x")
    with pytest.raises(ValidationError):
        PolicyGapRequest(entity="IIFL", topic="gold", max_requirements=50)
    with pytest.raises(ValidationError):
        PolicyGapRequest(entity="IIFL", topic="gold", delete_everything=True)


def _cite(q="quote"):
    return Citation(doc_id="d", chunk_id="d::0", label="L", quote=q)


def test_finding_status_must_match_evidence():
    GapFinding(requirement="r" * 12, regulation=_cite(), status="missing", explanation="silent on it")
    GapFinding(requirement="r" * 12, regulation=_cite(), status="met", policy=_cite(), explanation="covers it")
    with pytest.raises(ValidationError):
        GapFinding(requirement="r" * 12, regulation=_cite(), status="met", explanation="no policy cited")
    with pytest.raises(ValidationError):
        GapFinding(requirement="r" * 12, regulation=_cite(), status="missing", policy=_cite(), explanation="x" * 12)


# --- grounding lives in the schema, so aip.llm.structured repairs it ----------------------------
SOURCES = ["The surplus, if any, shall be refunded to the borrower within a maximum period of seven working days.",
           "An NBFC shall give adequate notice to the borrower before initiating the auction procedure."]


def test_requirement_schema_rejects_paraphrased_quote():
    schema = _requirement_schema(SOURCES, 6)
    ok = {"requirement": "Refund auction surplus within seven working days", "source": 1,
          "quote": "shall be refunded to the borrower within a maximum period of seven working days"}
    assert schema.model_validate({"requirements": [ok]})
    bad = {**ok, "quote": "surplus must be returned to borrowers within a week"}
    with pytest.raises(ValidationError, match="not verbatim"):
        schema.model_validate({"requirements": [bad]})
    with pytest.raises(ValidationError):
        schema.model_validate({"requirements": [{**ok, "source": 3}]})


def test_assessment_schema_requires_quote_unless_missing():
    schema = _assessment_schema(SOURCES)
    assert schema.model_validate({"status": "missing", "source": 2, "quote": "x", "explanation": "not addressed at all"}).source is None
    with pytest.raises(ValidationError, match="needs the policy excerpt"):
        schema.model_validate({"status": "met", "explanation": "covered by the policy"})
    with pytest.raises(ValidationError, match="not verbatim"):
        schema.model_validate({"status": "met", "source": 2, "quote": "we always tell borrowers before auctions",
                               "explanation": "covered by the policy"})
    assert schema.model_validate({"status": "weaker", "source": 2, "quote": "shall give adequate notice to the borrower",
                                  "explanation": "notice given but no channel specified"})


def test_vague_promise_cannot_meet_a_specific_limit():
    src = ["The Company shall not resort to undue harassment viz. persistently bothering the borrowers at odd hours.",
           "Calls to the borrower shall not be made before 9:00 a.m. or after 6:00 p.m."]
    schema = _assessment_schema(src, "Do not call borrowers before 8:00 a.m. or after 7:00 p.m.")
    with pytest.raises(ValidationError, match="specific figure"):
        schema.model_validate({"status": "met", "source": 1, "quote": "persistently bothering the borrowers at odd hours",
                               "explanation": "the policy bans calls at odd hours"})
    assert schema.model_validate({"status": "weaker", "source": 1, "quote": "persistently bothering the borrowers at odd hours",
                                  "explanation": "odd hours is vaguer than the 8-7 window"})
    assert schema.model_validate({"status": "met", "source": 2, "quote": "shall not be made before 9:00 a.m. or after 6:00 p.m.",
                                  "explanation": "a stricter window than the rule"})


def test_quote_in_source_tolerates_typography_not_paraphrase():
    src = "the consent or otherwise i.e. objection of the NBFC, if any, shall be conveyed within 21 days"
    assert quote_in_source("Objection of the NBFC, if any, shall be conveyed within 21 days.", src)
    assert not quote_in_source("objection must be sent within three weeks", src)


# --- entity resolution: lookalikes must fail loudly ---------------------------------------------
@pytest.mark.parametrize("name,doc", [("IIFL", "fpc-iifl"), ("Muthoot Finance Ltd", "fpc-muthoot"),
                                      ("Mahindra Finance", "fpc-mahindra-finance"), ("L&T Finance", "fpc-lnt-microloans"),
                                      ("Shri Ram Finance Corporation", "fpc-shri-ram-finance-corp")])
def test_resolve_known_names(name, doc):
    assert resolve(name).doc_id == doc


@pytest.mark.parametrize("name", ["Shriram Finance", "Muthoot MCred", "Bajaj Housing Finance", "finance", ""])
def test_resolve_refuses_lookalikes_and_ambiguity(name):
    with pytest.raises(EntityNotFound):
        resolve(name)


def test_lookalike_suggests_the_real_name():
    with pytest.raises(EntityNotFound) as e:
        resolve("Shriram Finance")
    assert "Shri Ram Finance Corporation Private Limited" in e.value.suggestions


# --- evaluation plumbing -------------------------------------------------------------------------
def test_gap_cases_validate():
    cases = load_gap_cases()
    assert len(cases) == 16 and all(c.expected["expected"] for c in cases)


def test_target_finding_matches_rule_by_quote_overlap():
    report = {"findings": [{"regulation": {"quote": "An NBFC shall declare a reserve price for the gold"}},
                           {"regulation": {"quote": "The surplus, if any, from the auction shall be refunded to the "
                                                    "borrower(s) / legal heir(s) within a maximum period of seven working days"}}]}
    f = target_finding(report, "shall be refunded to the borrower(s) / legal heir(s) within a maximum period of seven working days")
    assert f is report["findings"][1]
    assert not same_rule("calling the borrower before 8:00 a.m. and after 7:00 p.m.", "An NBFC shall declare a reserve price")


def test_diagnosis_names_the_first_failing_stage():
    assert diagnose({"stage_failed_rule_retrieval": 1.0}, None).startswith("1")
    assert diagnose({"stage_failed_extraction": 1.0}, None).startswith("2")
    assert diagnose({"stage_failed_policy_retrieval": 1.0}, "missing").startswith("3")
    assert "called 'met'" in diagnose({"stage_failed_assessment": 1.0}, "met")
    assert diagnose({"status_exact": 1.0}, "met") is None


def test_report_render_is_readable():
    r = GapReport(entity="X Ltd", policy_doc_id="fpc-x", policy_title="X - FPC", policy_date="2025-01-01", topic="t",
                  regulation_as_of="2026-07-01", policy_predates_regulation=True,
                  findings=[GapFinding(requirement="Refund surplus in 7 working days", regulation=_cite("refund it"),
                                       status="missing", explanation="The policy is silent on refunds.")],
                  counts={"missing": 1})
    text = r.render()
    assert "[MISSING]" in text and "not found" in text and "latest update, 2026-07-01" in text


def test_requirement_quote_that_only_introduces_a_list_is_rejected():
    src = ["An NBFC shall not engage in harsh methods. Following practices shall be deemed as harsh: "
           "(1) calling the borrower before 9:00 a.m. and after 6:00 p.m."]
    schema = _requirement_schema(src, 8)
    with pytest.raises(ValidationError, match="introduces a list"):
        schema.model_validate({"requirements": [{"requirement": "No harsh recovery methods", "source": 1,
                                                 "quote": "Following practices shall be deemed as harsh:"}]})
    assert schema.model_validate({"requirements": [{"requirement": "No calls before 9am or after 6pm", "source": 1,
                                                    "quote": "calling the borrower before 9:00 a.m. and after 6:00 p.m."}]})


def test_target_finding_accepts_any_passage_stating_the_rule():
    report = {"findings": [{"regulation": {"quote": "Further, it shall be ensured that there is no capitalization of the "
                                                    "penal charges i.e., no further interest computed on such charges."}}]}
    assert target_finding(report, ["There shall be no capitalisation of penal charges",
                                   "there is no capitalization of the penal charges"])
    assert target_finding(report, "There shall be no capitalisation of penal charges") is None
