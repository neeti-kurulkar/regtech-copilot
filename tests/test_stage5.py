"""Stage 5 (offline): grounding of figures and dates, calendar maths, check_upcoming, dev-set matching."""
from datetime import date

import pytest
from pydantic import ValidationError

from regtech.deadline_eval import load_dev, match
from regtech.deadlines import (DeadlineRecord, _date_in, _figure_in, _period_ends, check_upcoming, deadline_schema,
                               number_words, occurrences)


def test_numbers_in_words_and_digits():
    assert {"7", "seven"} <= number_words(7) and "thirty" in number_words(30) and "twenty-one" in number_words(21)
    assert _figure_in(7, "not exceeding a maximum period of seven working days")
    assert _figure_in(14, "not later than 14 days from the date of classification")
    assert not _figure_in(4, "not later than 14 days")          # 4 is not hiding inside 14
    assert not _figure_in(30, "within 21 days of the request")


def test_dates_in_quotes():
    assert _date_in(date(2026, 3, 31), "|> 90 days|By March 31, 2026|")
    assert _date_in(date(2026, 3, 31), "the half-year (September 30th and March 31st of 2026)") is False
    assert _date_in(date(2026, 6, 30), "or up to June 30, 2026, whichever is later")
    assert not _date_in(date(2026, 6, 30), "or up to June 30, 2025")


SECTION = ("43. The NBFC shall furnish FMR immediately but not later than 14 days from the date of classification of "
           "an incident as fraud. 21. A NBFC shall submit a weekly report by close of business on every Friday.")


def test_schema_grounds_quotes_and_figures():
    schema = deadline_schema(SECTION)
    ok = {"obligation": "Report fraud to RBI via FMR", "kind": "event_triggered", "trigger": "classification as fraud",
          "within_days": 14, "quote": "not later than 14 days from the date of classification of an incident as fraud"}
    assert schema.model_validate({"items": [ok]})
    with pytest.raises(ValidationError, match="does not appear in the quote"):
        schema.model_validate({"items": [{**ok, "within_days": 15}]})
    with pytest.raises(ValidationError, match="not verbatim"):
        schema.model_validate({"items": [{**ok, "quote": "fraud must be reported within two weeks"}]})
    with pytest.raises(ValidationError, match="needs the trigger"):
        schema.model_validate({"items": [{**ok, "trigger": None}]})
    with pytest.raises(ValidationError, match="needs frequency"):
        schema.model_validate({"items": [{**ok, "kind": "recurring", "within_days": None}]})
    assert schema.model_validate({"items": []}).items == []


def test_period_ends_follow_the_indian_financial_year():
    assert _period_ends("quarterly", date(2026, 9, 1), date(2027, 4, 30)) == [
        date(2026, 9, 30), date(2026, 12, 31), date(2027, 3, 31)]
    assert _period_ends("half_yearly", date(2026, 1, 1), date(2027, 12, 31)) == [
        date(2026, 3, 31), date(2026, 9, 30), date(2027, 3, 31), date(2027, 9, 30)]
    assert _period_ends("annual", date(2026, 1, 1), date(2027, 12, 31)) == [date(2026, 3, 31), date(2027, 3, 31)]


def _rec(**kw):
    base = dict(id="x", doc_id="reg-x", source="X Directions", chunk_id="reg-x::m1", obligation="do it", kind="recurring",
                quote="q")
    base.update(kw)
    return DeadlineRecord(**base)


def test_occurrences():
    start, end = date(2026, 9, 29), date(2026, 10, 31)          # 29 Sep 2026 is a Tuesday
    fridays = occurrences(_rec(frequency="weekly", weekday="Friday"), start, end)
    assert [d for d, _ in fridays] == [date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 23), date(2026, 10, 30)]
    half = occurrences(_rec(frequency="half_yearly", offset_days=30), start, end)
    assert half == [(date(2026, 10, 30), None)]                  # 30 days after 30 Sep
    monthly = occurrences(_rec(frequency="monthly"), start, end)
    assert monthly[0] == (date(2026, 9, 30), "no day stated: shown at month end")
    two_months = occurrences(_rec(frequency="half_yearly", offset_months=2), start, date(2027, 6, 30))
    assert [d for d, _ in two_months] == [date(2026, 11, 30), date(2027, 5, 31)]   # 2 months after 30 Sep / 31 Mar
    assert occurrences(_rec(kind="fixed_date", due_date=date(2026, 10, 1)), start, end) == [(date(2026, 10, 1), None)]
    assert occurrences(_rec(kind="fixed_date", due_date=date(2026, 3, 31)), start, end) == []
    assert occurrences(_rec(frequency="every_n_years", n_years=2), start, end) == []


def test_check_upcoming_buckets():
    recs = [_rec(id="a", frequency="weekly", weekday="Friday"),
            _rec(id="b", frequency="every_n_years", n_years=2),
            _rec(id="c", kind="event_triggered", trigger="a request", within_days=21),
            _rec(id="d", kind="fixed_date", due_date=date(2026, 10, 15))]
    rep = check_upcoming({"days": 16, "as_of": date(2026, 9, 29)}, records=recs)
    assert [o.due for o in rep.due] == [date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 15)]
    assert [r.id for r in rep.recurring_unscheduled] == ["b"] and [r.id for r in rep.event_triggered] == ["c"]
    text = rep.render()
    assert "financial year" in text and "within 21 days of a request" in text
    long = check_upcoming({"days": 60, "as_of": date(2026, 9, 29)}, records=recs).render()
    assert long.count("do it") <= 4 and "(9 times)" in long      # nine Fridays collapse into one line
    with pytest.raises(ValidationError):
        check_upcoming({"days": 0}, records=recs)


def test_duties_on_other_bodies_are_hidden_by_default():
    from regtech.deadlines import _party_schema
    recs = [_rec(id="n", kind="fixed_date", due_date=date(2026, 10, 1)),
            _rec(id="o", kind="fixed_date", due_date=date(2026, 10, 2), party="other", party_reason="a CIC's duty")]
    default = check_upcoming({"days": 10, "as_of": date(2026, 9, 29)}, records=recs)
    assert [o.record_id for o in default.due] == ["n"]
    both = check_upcoming({"days": 10, "as_of": date(2026, 9, 29), "include_other_parties": True}, records=recs)
    assert [o.record_id for o in both.due] == ["n", "o"]
    schema = _party_schema(2)
    with pytest.raises(ValidationError, match="each item"):
        schema.model_validate({"verdicts": [{"item": 1, "party": "nbfc", "reason": "the NBFC must"}]})


def test_months_are_grounded_and_rendered():
    schema = deadline_schema("place a report before top management within two months from the end of the half-year")
    item = {"obligation": "Report DQI issues to top management", "kind": "event_triggered", "trigger": "end of half-year",
            "within_months": 2, "quote": "within two months from the end of the half-year"}
    assert schema.model_validate({"items": [item]})
    with pytest.raises(ValidationError, match="within_months=3"):
        schema.model_validate({"items": [{**item, "within_months": 3}]})
    assert _rec(kind="event_triggered", trigger="x", within_months=2).window() == "within 2 months"


def test_dev_set_and_one_to_one_matching():
    cases = load_dev()
    assert len(cases) == 11 and sum(len(c.expected) for c in cases) == 17
    gold = next(c for c in cases if c.id == "D04").expected
    merged = [{"quote": "at least once in every two years for high-risk customers, once in every eight years for "
                        "medium risk customers and once in every 10 years for low-risk customers",
               "kind": "recurring", "n_years": 2}]
    tp, fields_ok, _ = match(merged, gold)
    assert tp == 1 and fields_ok == 1   # one merged item earns credit for one obligation only


def test_section_filter_does_not_drop_deadline_sections():
    """Regression: v1's filter skipped 'within a maximum period of seven working days' and similar phrasing."""
    from regtech.deadlines import TEMPORAL, deadline_sections
    ids = {c.chunk_id for c in deadline_sections("regulation")}
    assert all(c.input in ids for c in load_dev())
    for phrase in ("not exceeding a maximum period of seven working days", "within a period of 30 days after full repayment",
                   "at least once in every two years", "by close of business on every Friday"):
        assert TEMPORAL.search(phrase), phrase
