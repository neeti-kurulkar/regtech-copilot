"""Stage 5: extract_deadlines / check_upcoming - the compliance calendar.

Lab 1's reliable extractor, scaled from one document to a corpus: every heading section with
temporal language is sent to aip.llm.structured with a schema that grounds itself - the quote must
be verbatim in the section, and any number of days, years or date the model claims must appear in
its quote (digits or words). Items become a stored calendar; check_upcoming projects it onto dates.

Three kinds, because most rules are not dates:
  fixed_date      "by March 31, 2027", "with effect from ..."            -> on the calendar
  recurring       "monthly", "weekly ... every Friday", "within 30 days from the end of the half-year"
                                                                          -> projected from as-of
  event_triggered "within 14 days from the date of classification as fraud" -> standing obligations
"""
from __future__ import annotations

import calendar as _cal
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aip.chunking import Chunk
from aip.cost import Budget, map_in_context
from aip.guards import (UNTRUSTED_SYSTEM_CLAUSE, date_in_text, delimit_untrusted, normalise_text, number_forms,
                        number_in_text, quote_in_source)
from aip.llm import StructuredOutputError, structured

from regtech.index import build_chunks
from regtech.paths import DATA_DIR
from regtech.qa import short_title

CALENDAR_PATH = DATA_DIR / "processed" / "compliance_calendar.json"

Kind = Literal["fixed_date", "recurring", "event_triggered"]
Frequency = Literal["daily", "weekly", "fortnightly", "monthly", "quarterly", "half_yearly", "annual", "every_n_years", "other"]
Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Section pre-filter: any stated period ("30 days", "seven working days", "one year") or cadence or date.
# A first version only matched "within <digits> days" and silently skipped sections written as "within a
# maximum period of seven working days" - including the gold-auction surplus refund (see stage5_findings.md).
TEMPORAL = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty-five|"
    r"sixty|ninety)\s+(?:\(\d+\)\s+)?(?:working\s+|calendar\s+|business\s+)?(?:days?|weeks?|months?|years?)\b|"
    r"not later than|monthly|quarter|"
    r"half[- ]yearly|six months|annual|every year|once a year|once in every|weekly|every (?:friday|week)|fortnight|"
    r"with effect from|w\.e\.f\.|come into (?:force|effect)|effective from|"
    r"(?:by|from|on or after|before|up to) (?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december) \d{1,2}, 20\d\d", re.I)

# Grounding helpers live in aip.guards (general, not NBFC-specific); aliases kept short for the schema below.
number_words = number_forms
_figure_in = number_in_text
_date_in = date_in_text


# ---------------------------------------------------------------------------------------------
# Schema handed to aip.llm.structured (grounding inside the validators)
# ---------------------------------------------------------------------------------------------
def deadline_schema(source: str) -> type[BaseModel]:
    class Deadline(BaseModel):
        obligation: str = Field(min_length=10, max_length=400, description="what must be done, in plain language")
        kind: Kind
        quote: str = Field(min_length=10, max_length=900, description="exact consecutive words from the section")
        applies_to: str | None = Field(None, description="which NBFCs / loans it applies to, if the text limits it")
        due_date: date | None = Field(None, description="fixed_date only: YYYY-MM-DD")
        frequency: Frequency | None = Field(None, description="recurring only")
        n_years: int | None = Field(None, ge=1, le=20, description="recurring every_n_years only")
        offset_days: int | None = Field(None, ge=0, le=366, description="recurring: due this many days after the period ends")
        offset_months: int | None = Field(None, ge=1, le=24, description="recurring: due this many MONTHS after the period ends")
        weekday: Weekday | None = Field(None, description="recurring weekly: the day it is due")
        trigger: str | None = Field(None, description="event_triggered only: the event that starts the clock")
        within_days: int | None = Field(None, ge=0, le=3650, description="event_triggered: days allowed after the trigger")
        within_months: int | None = Field(None, ge=1, le=120, description="event_triggered: MONTHS allowed, when stated in months")
        working_days: bool = Field(False, description="true if the text says working days")

        @model_validator(mode="after")
        def _consistent_and_grounded(self) -> Deadline:
            if not quote_in_source(self.quote, source):
                raise ValueError("quote is not verbatim in the section; copy the exact words")
            if self.kind == "fixed_date" and self.due_date is None:
                raise ValueError("a fixed_date item needs due_date")
            if self.kind == "recurring" and self.frequency is None:
                raise ValueError("a recurring item needs frequency")
            if self.kind == "recurring" and self.frequency == "every_n_years" and not self.n_years:
                raise ValueError("frequency every_n_years needs n_years")
            if self.kind == "event_triggered" and not self.trigger:
                raise ValueError("an event_triggered item needs the trigger event")
            for name in ("within_days", "offset_days", "n_years", "within_months", "offset_months"):
                n = getattr(self, name)
                if n is not None and not _figure_in(n, self.quote) and not (name == "within_days" and n in (365, 366)
                                                                            and _figure_in(1, self.quote)):
                    raise ValueError(f"{name}={n} does not appear in the quote; quote the words that state it")
            if self.due_date is not None and not _date_in(self.due_date, self.quote):
                raise ValueError(f"due_date {self.due_date} does not appear in the quote; quote the words that state it")
            return self

    class Deadlines(BaseModel):
        items: list[Deadline] = Field(default_factory=list)

    return Deadlines


PROMPTS = {
    "A": f"""\
Extract every compliance deadline from this section of an RBI Direction for NBFCs: any obligation on
the NBFC that has a time element. Classify each:
- fixed_date: must happen by, or applies from, a specific calendar date.
- recurring: repeats on a cycle (monthly, quarterly, weekly on a named day, once every N years ...).
- event_triggered: must happen within a period after some event (a request, a repayment, a classification ...).
Copy the quote verbatim from the section. One item per obligation. If there are none, return an empty list.

{UNTRUSTED_SYSTEM_CLAUSE}
""",
    "B": f"""\
Extract every compliance deadline from this section of an RBI Direction for NBFCs: an obligation on
the NBFC (or its Board) that must be met by a date, on a cycle, or within a period after an event.

Kinds:
- fixed_date: by, or with effect from, a specific calendar date. Put the date in due_date.
- recurring: repeats on a cycle. Set frequency; for "once in every N years" use every_n_years with n_years;
  if due some time after the period ends, set offset_days (or offset_months if the text states months);
  for weekly, the weekday if named.
- event_triggered: within a period after an event. Set trigger and within_days (convert "one year" to 365),
  or within_months if the text states months; working_days=true if the text says working days.

NOT deadlines - do not extract:
- worked examples or illustrations with sample dates;
- classification thresholds or definitions ("overdue for more than 90 days", "SMA-1: 31-60 days");
- durations that are not due dates (a minimum tenure, a validity period of a document, a loan term);
- historical dates that only say from when a rule already applied, or when a scheme started.

Rules: one item per obligation (split a sentence that sets several limits); copy the quote verbatim;
every number or date you put in a field must appear in your quote. If there are none, return an empty list.

{UNTRUSTED_SYSTEM_CLAUSE}
""",
}


# ---------------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------------
class DeadlineRecord(BaseModel):
    id: str
    doc_id: str
    source: str
    chunk_id: str
    obligation: str
    kind: Kind
    quote: str
    applies_to: str | None = None
    due_date: date | None = None
    frequency: Frequency | None = None
    n_years: int | None = None
    offset_days: int | None = None
    offset_months: int | None = None
    weekday: Weekday | None = None
    trigger: str | None = None
    within_days: int | None = None
    within_months: int | None = None
    working_days: bool = False
    party: Literal["nbfc", "other"] = "nbfc"
    party_reason: str = ""

    def window(self) -> str:
        """'within 7 working days' / 'within 2 months' / 'within ? days' for event-triggered items."""
        if self.within_months:
            return f"within {self.within_months} month{'s' if self.within_months > 1 else ''}"
        return f"within {self.within_days if self.within_days is not None else '?'} {'working ' if self.working_days else ''}days"


def source_label(chunk: Chunk) -> str:
    path = [p for p in chunk.meta.get("heading", "").split(" > ")[1:] if p]
    nums = chunk.meta.get("numbers", [])
    para = (f"para {nums[0]}" if len(nums) == 1 else f"paras {nums[0]}-{nums[-1]}") if nums else ""
    return " | ".join(x for x in (short_title(chunk.meta.get("title", chunk.doc_id)), " > ".join(path[-2:]), para) if x)


def extract_section(chunk: Chunk, prompt: str = "B", tier: str = "SMALL") -> list[DeadlineRecord]:
    """extract_deadlines for one section. Grounding failures after aip's repairs yield no items (fail closed)."""
    try:
        out = structured(delimit_untrusted(chunk.text), schema=deadline_schema(chunk.text), system=PROMPTS[prompt],
                         tier=tier, max_tokens=4096)
    except StructuredOutputError:
        return []
    records = []
    for it in out.items:
        key = f"{chunk.chunk_id}|{normalise_text(it.quote)}|{it.kind}"
        records.append(DeadlineRecord(id=hashlib.sha1(key.encode()).hexdigest()[:12], doc_id=chunk.doc_id,
                                      source=source_label(chunk), chunk_id=chunk.chunk_id, **it.model_dump()))
    return records


def deadline_sections(doc_type: str = "regulation", doc_ids: set[str] | None = None) -> list[Chunk]:
    return [c for c in build_chunks(doc_type, "markdown", 1500)
            if TEMPORAL.search(c.text) and (doc_ids is None or c.doc_id in doc_ids)]


def extract_deadlines(doc_type: str = "regulation", doc_ids: set[str] | None = None, prompt: str = "B",
                      tier: str = "SMALL", workers: int = 4) -> list[DeadlineRecord]:
    """The tool: every deadline in one document, a set of documents, or a whole corpus."""
    sections = deadline_sections(doc_type, doc_ids)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = map_in_context(pool, lambda c: extract_section(c, prompt, tier), sections)
    seen, out = set(), []
    for rec in (r for batch in batches for r in batch):
        if rec.id not in seen:
            seen.add(rec.id)
            out.append(rec)
    return out


PARTY_SYSTEM = f"""\
RBI Directions for NBFCs also place duties on other bodies. For each numbered deadline extracted from the
section below, say who must meet it:
- nbfc: the NBFC itself, including when the text calls it a "credit institution" (CI), a "regulated
  entity" or a lender, and including its Board, committees, officers or senior management.
- other: a different body - a credit information company (CIC), a working group or sub-group, the
  Reserve Bank, a government authority, an exchange, a borrower, and so on.
If the text lists several bodies and the NBFC is among them (for example "banks, ... REs, ..." - an NBFC is
a regulated entity), or the duty is shared between the NBFC and another body, answer nbfc: hiding a duty
the NBFC does have is worse than showing one it shares. Answer from the section's wording only. Give a
verdict for every item.

{UNTRUSTED_SYSTEM_CLAUSE}
"""


def _party_schema(n: int) -> type[BaseModel]:
    class PartyVerdict(BaseModel):
        item: int = Field(ge=1, le=n)
        party: Literal["nbfc", "other"]
        reason: str = Field(min_length=3, max_length=300)

    class PartyVerdicts(BaseModel):
        verdicts: list[PartyVerdict]

        @model_validator(mode="after")
        def _each_once(self) -> PartyVerdicts:
            if sorted(v.item for v in self.verdicts) != list(range(1, n + 1)):
                raise ValueError(f"give exactly one verdict for each item 1..{n}")
            return self

    return PartyVerdicts


def tag_parties(records: list[DeadlineRecord], tier: str = "SMALL", workers: int = 4) -> list[DeadlineRecord]:
    """Second pass: which party does each deadline bind? One call per source section, all its items at once."""
    chunks = {c.chunk_id: c for c in build_chunks("regulation", "markdown", 1500)}
    by_section: dict[str, list[DeadlineRecord]] = {}
    for r in records:
        by_section.setdefault(r.chunk_id, []).append(r)

    def tag(items: list[DeadlineRecord]) -> None:
        listing = "\n".join(f"[{i}] {r.obligation}  (quote: \"{r.quote[:300]}\")" for i, r in enumerate(items, 1))
        prompt = f"SECTION:\n{delimit_untrusted(chunks[items[0].chunk_id].text)}\n\nDEADLINES:\n{listing}"
        try:
            out = structured(prompt, schema=_party_schema(len(items)), system=PARTY_SYSTEM, tier=tier, max_tokens=2048)
        except StructuredOutputError:
            return  # keep the default ('nbfc'): an untagged item stays visible rather than silently disappearing
        for v in out.verdicts:
            items[v.item - 1].party, items[v.item - 1].party_reason = v.party, v.reason

    with ThreadPoolExecutor(max_workers=workers) as pool:
        map_in_context(pool, tag, list(by_section.values()))
    return records


def build_calendar(prompt: str = "B", tier: str = "SMALL") -> list[DeadlineRecord]:
    with Budget(limit_usd=1.0, label="build-compliance-calendar") as b:
        records = tag_parties(extract_deadlines("regulation", prompt=prompt, tier=tier))
    CALENDAR_PATH.write_text(json.dumps({"prompt": prompt, "tier": tier, "items": [r.model_dump(mode="json") for r in records]},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
    print(b.report())
    return records


def load_calendar(path: Path = CALENDAR_PATH) -> list[DeadlineRecord]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `python -m regtech build-calendar` first")
    return [DeadlineRecord.model_validate(r) for r in json.loads(path.read_text(encoding="utf-8"))["items"]]


# ---------------------------------------------------------------------------------------------
# check_upcoming
# ---------------------------------------------------------------------------------------------
class UpcomingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(30, ge=1, le=366)
    as_of: date | None = None
    doc_ids: list[str] | None = None
    include_event_triggered: bool = True
    include_other_parties: bool = False


class Occurrence(BaseModel):
    due: date
    obligation: str
    kind: Kind
    source: str
    quote: str
    assumption: str | None = None
    record_id: str


class UpcomingReport(BaseModel):
    as_of: date
    until: date
    due: list[Occurrence]
    recurring_unscheduled: list[DeadlineRecord]
    event_triggered: list[DeadlineRecord]
    assumptions: list[str]

    def render(self, max_standing: int = 8) -> str:
        lines = [f"Compliance calendar: {self.as_of} to {self.until}", ""]
        if self.due:
            groups: dict[str, list[Occurrence]] = {}
            for o in self.due:
                groups.setdefault(o.record_id, []).append(o)
            lines.append(f"Due in this window ({len(self.due)} dates, {len(groups)} obligations):")
            for occ in sorted(groups.values(), key=lambda g: (g[0].due, g[0].source)):
                o = occ[0]
                if len(occ) <= 2:
                    when = ", ".join(str(x.due) for x in occ)
                else:
                    when = f"{o.due} ... {occ[-1].due}  ({len(occ)} times)"
                lines.append(f"  {when}  {o.obligation}")
                lines.append(f"              {o.source}" + (f"  [{o.assumption}]" if o.assumption else ""))
        else:
            lines.append("Nothing with a calendar date falls in this window.")
        if self.recurring_unscheduled:
            lines += ["", f"Recurring, but the Directions set no calendar day ({len(self.recurring_unscheduled)}), e.g.:"]
            lines += [f"  - [{r.frequency}{f' x{r.n_years}' if r.n_years else ''}] {r.obligation}  ({r.source})"
                      for r in self.recurring_unscheduled[:max_standing]]
        if self.event_triggered:
            lines += ["", f"Standing obligations triggered by events ({len(self.event_triggered)}), e.g.:"]
            lines += [f"  - {r.window()} of {r.trigger}: {r.obligation}  ({r.source})"
                      for r in self.event_triggered[:max_standing]]
        lines += ["", "Assumptions: " + "; ".join(self.assumptions)]
        return "\n".join(lines)


_PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "half_yearly": 6, "annual": 12}
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _period_ends(freq: str, start: date, end: date) -> list[date]:
    """Period ends on the Indian financial-year grid: quarters end Jun/Sep/Dec/Mar, half-years Sep/Mar, year Mar 31."""
    step = _PERIOD_MONTHS[freq]
    out, y, m = [], start.year - 1, 3  # walk month-ends from March of the previous year
    while True:
        last = date(y, m, _cal.monthrange(y, m)[1])
        if last > end:
            return out
        if last >= start and ((m - 3) % step == 0):
            out.append(last)
        m += 1
        if m > 12:
            y, m = y + 1, 1


_PERIOD_NAME = {"monthly": "month", "quarterly": "quarter", "half_yearly": "half-year", "annual": "financial year"}


def _month_end_plus(d: date, months: int) -> date:
    """A period end (a month-end) moved on by whole months, landing on that month's end."""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, _cal.monthrange(y, m)[1])


def occurrences(r: DeadlineRecord, start: date, end: date) -> list[tuple[date, str | None]]:
    if r.kind == "fixed_date" and r.due_date and start <= r.due_date <= end:
        return [(r.due_date, None)]
    if r.kind != "recurring" or not _schedulable(r):
        return []
    if r.frequency == "weekly":
        first = start + timedelta(days=(_WEEKDAYS.index(r.weekday) - start.weekday()) % 7)
        return [(first + timedelta(weeks=i), None) for i in range(((end - first).days // 7) + 1) if first + timedelta(weeks=i) <= end]
    stated = r.offset_days is not None or r.offset_months is not None
    note = None if stated else f"no day stated: shown at {_PERIOD_NAME[r.frequency]} end"

    def shift(e: date) -> date:
        return _month_end_plus(e, r.offset_months) if r.offset_months else e + timedelta(days=r.offset_days or 0)

    lookback = timedelta(days=(r.offset_days or 0) + 31 * (r.offset_months or 0))
    return [(shift(e), note) for e in _period_ends(r.frequency, start - lookback, end) if start <= shift(e) <= end]


def check_upcoming(request: UpcomingRequest | dict | None = None, records: list[DeadlineRecord] | None = None) -> UpcomingReport:
    req = UpcomingRequest.model_validate(request or {}) if not isinstance(request, UpcomingRequest) else request
    as_of = req.as_of or date.today()
    until = as_of + timedelta(days=req.days)
    recs = records if records is not None else load_calendar()
    if req.doc_ids:
        recs = [r for r in recs if r.doc_id in set(req.doc_ids)]
    if not req.include_other_parties:
        recs = [r for r in recs if r.party == "nbfc"]
    due, unscheduled = [], []
    for r in recs:
        occ = occurrences(r, as_of, until)
        due += [Occurrence(due=d, obligation=r.obligation, kind=r.kind, source=r.source, quote=r.quote,
                           assumption=a, record_id=r.id) for d, a in occ]
        if r.kind == "recurring" and not occ and not _schedulable(r):
            unscheduled.append(r)
    events = [r for r in recs if r.kind == "event_triggered"] if req.include_event_triggered else []
    return UpcomingReport(
        as_of=as_of, until=until, due=sorted(due, key=lambda o: (o.due, o.source)), recurring_unscheduled=unscheduled,
        event_triggered=events,
        assumptions=["financial year April-March (quarters end 30 Jun, 30 Sep, 31 Dec, 31 Mar)",
                     "a report due 'monthly/quarterly' with no stated day is shown at period end",
                     "weekly items fall on the stated weekday; holidays are not adjusted",
                     "only duties on the NBFC are shown; duties on CICs, working groups or the RBI are hidden"
                     + ("" if not req.include_other_parties else " (included on request)"),
                     "event-triggered deadlines depend on events the system cannot see, so they are listed, not dated"])


def _schedulable(r: DeadlineRecord) -> bool:
    return (r.frequency == "weekly" and r.weekday is not None) or r.frequency in ("monthly", "quarterly", "half_yearly") \
        or (r.frequency == "annual" and (r.offset_days is not None or r.offset_months is not None))
