"""Stage 4, part 2: find_enforcement_precedent - has this failure been penalised before?

  risk (text, or a check_policy_gap finding)
    --search--> penalty passages (Stage 1 enforcement index) -> candidate cases (case table)
    --structured()--> a verdict for EVERY candidate: same_failure | related | unrelated,
                      quoting the matching charge verbatim
    -> precedents (same_failure), related cases, or an explicit "no comparable precedent".

The nearest case is not a precedent by default: with 13 penalties, most risks have none, and a
tool that always returns its top hit would manufacture false evidence.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aip.cost import Budget
from aip.guards import UNTRUSTED_SYSTEM_CLAUSE, delimit_untrusted, quote_in_source
from aip.llm import StructuredOutputError, structured

from regtech.enforcement import CaseRecord, load_cases
from regtech.index import CorpusIndex

Match = Literal["same_failure", "related", "unrelated"]
GAP_STATUSES = {"missing", "weaker", "inconsistent"}


def _lakh(inr: int) -> str:
    return f"₹{inr / 100_000:.2f} lakh" if inr < 10_000_000 else f"₹{inr / 10_000_000:.2f} crore"


class PrecedentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk: str | None = Field(None, min_length=10, max_length=800, description="the compliance risk, in plain words")
    gap: dict | None = Field(None, description="a check_policy_gap finding (requirement, status, explanation)")
    max_cases: int = Field(3, ge=1, le=5)

    @model_validator(mode="after")
    def _one_input(self) -> PrecedentRequest:
        if not self.risk and not self.gap:
            raise ValueError("give either `risk` (a description) or `gap` (a check_policy_gap finding)")
        if self.gap and not self.gap.get("requirement"):
            raise ValueError("`gap` must carry the finding's `requirement`")
        return self

    def risk_text(self) -> str:
        if self.risk:
            return self.risk
        g = self.gap or {}
        return f"Failure to comply with: {g['requirement']} ({g.get('status', 'gap')}: {g.get('explanation', '')})"


class Precedent(BaseModel):
    case_id: str
    entity: str
    press_release_date: date
    order_date: date | None
    penalty_inr: int
    penalty_text: str
    directions: list[str]
    matched_charge: str
    match: Match
    reason: str


class PrecedentReport(BaseModel):
    risk: str
    precedents: list[Precedent]
    related: list[Precedent]
    no_precedent: bool
    message: str
    candidates_considered: list[str]
    cost_usd: float = 0.0

    def render(self) -> str:
        lines = [f"Risk: {self.risk}", "", self.message]
        for title, items in (("Precedents (same failure penalised)", self.precedents),
                             ("Related penalties (same area, different obligation)", self.related)):
            if items:
                lines += ["", f"{title}:"]
                for p in items:
                    lines += [f"  - {p.entity}: fined {_lakh(p.penalty_inr)}, press release {p.press_release_date}"
                              + (f", order {p.order_date}" if p.order_date else ""),
                              f"    Charge upheld: \"{p.matched_charge}\"",
                              f"    Why it matches: {p.reason}"]
        return "\n".join(lines)


JUDGE_SYSTEM = f"""\
You decide whether real RBI penalty cases are precedents for a described compliance risk.
For EVERY numbered candidate case, give one verdict:
- same_failure: one of its sustained charges is the same kind of failure as the risk (the same
  obligation breached), even if the company, product or wording differ.
- related: a charge is in the same regulatory area but concerns a different obligation.
- unrelated: nothing in its charges concerns this risk.
Example (fictitious): risk "deposit receipts not issued to depositors". Charge "failed to issue
deposit receipts" is same_failure; "failed to renew deposits on maturity" is related;
"failed to classify NPAs" is unrelated.

For same_failure or related, copy the matching charge VERBATIM from that case. Judge only from the
charges as written; do not assume facts they do not state. Being in the same broad topic (for
example both about KYC) is not enough for same_failure.

{UNTRUSTED_SYSTEM_CLAUSE}
"""


def _verdict_schema(cases: list[CaseRecord]) -> type[BaseModel]:
    n = len(cases)

    class Verdict(BaseModel):
        candidate: int = Field(ge=1, le=n)
        match: Match
        charge: str | None = Field(None, description="the matching charge, verbatim; null if unrelated")
        reason: str = Field(min_length=5, max_length=500)

        @model_validator(mode="after")
        def _grounded(self) -> Verdict:
            if self.match == "unrelated":
                self.charge = None
                return self
            case = cases[self.candidate - 1]
            if not self.charge or not any(quote_in_source(self.charge, ch) or quote_in_source(ch, self.charge)
                                          for ch in case.charges):
                raise ValueError(f"candidate {self.candidate}: 'charge' must be one of that case's charges, copied verbatim")
            return self

    class Verdicts(BaseModel):
        verdicts: list[Verdict]

        @model_validator(mode="after")
        def _every_candidate_once(self) -> Verdicts:
            seen = [v.candidate for v in self.verdicts]
            if sorted(seen) != list(range(1, n + 1)):
                raise ValueError(f"give exactly one verdict for each candidate 1..{n}; got {sorted(seen)}")
            return self

    return Verdicts


def _case_block(i: int, c: CaseRecord) -> str:
    charges = "\n".join(f"   - {ch}" for ch in c.charges)
    return (f"[{i}] {c.entity} | press release {c.press_release_date} | penalty {c.penalty_text}\n"
            f"   Directions: {'; '.join(c.directions) or 'not named'}\n   Charges sustained:\n{charges}")


class PrecedentFinder:
    def __init__(self, index: CorpusIndex | None = None, cases: dict[str, CaseRecord] | None = None,
                 n_candidates: int = 6, tier: str = "MAIN"):
        self.index = index or CorpusIndex("enforcement")
        self.cases = cases or load_cases()
        self.n_candidates, self.tier = n_candidates, tier

    def candidates(self, risk: str) -> list[CaseRecord]:
        order: list[str] = []
        for h in self.index.search(risk, k=self.n_candidates * 4):
            if h.doc_id not in order:
                order.append(h.doc_id)
        return [self.cases[d] for d in order[: self.n_candidates]]

    def find(self, request: PrecedentRequest | dict) -> PrecedentReport:
        req = request if isinstance(request, PrecedentRequest) else PrecedentRequest.model_validate(request)
        risk = req.risk_text()
        with Budget(limit_usd=0.1, label="find_enforcement_precedent") as b:
            cands = self.candidates(risk)
            prompt = (f"RISK: {risk}\n\nCANDIDATE PENALTY CASES:\n"
                      + delimit_untrusted("\n\n".join(_case_block(i, c) for i, c in enumerate(cands, 1))))
            try:
                out = structured(prompt, schema=_verdict_schema(cands), system=JUDGE_SYSTEM, tier=self.tier,
                                 max_tokens=4096)
                verdicts = sorted(out.verdicts, key=lambda v: v.candidate)
            except StructuredOutputError:
                verdicts = []  # fail closed: no grounded verdict means no claimed precedent
        found = {"same_failure": [], "related": []}
        for v in verdicts:
            if v.match != "unrelated":
                c = cands[v.candidate - 1]
                found[v.match].append(Precedent(
                    case_id=c.doc_id, entity=c.entity, press_release_date=c.press_release_date, order_date=c.order_date,
                    penalty_inr=c.penalty_inr, penalty_text=c.penalty_text, directions=c.directions,
                    matched_charge=v.charge, match=v.match, reason=v.reason))
        precedents, related = found["same_failure"][: req.max_cases], found["related"][: req.max_cases]
        n_total = len(self.cases)
        if precedents:
            message = f"{len(precedents)} precedent(s): the RBI has penalised this kind of failure."
        elif not verdicts and cands:
            message = "Could not produce a grounded judgement; no precedent is claimed."
        else:
            message = (f"No comparable precedent among the {n_total} RBI penalty cases in the corpus"
                       + (" (related cases in the same area are listed below)." if related else "."))
        return PrecedentReport(risk=risk, precedents=precedents, related=related, no_precedent=not precedents,
                               message=message, candidates_considered=[c.doc_id for c in cands],
                               cost_usd=round(b.spent_usd, 6))


def find_enforcement_precedent(**kwargs) -> PrecedentReport:
    """Tool entry point: validates arguments against PrecedentRequest, returns a PrecedentReport."""
    return PrecedentFinder().find(PrecedentRequest(**kwargs))


def precedents_for_gaps(gap_report: dict, finder: PrecedentFinder | None = None) -> list[tuple[dict, PrecedentReport]]:
    """Chain Stage 3 -> Stage 4: a precedent search for every gap (missing / weaker / inconsistent) in a GapReport."""
    finder = finder or PrecedentFinder()
    return [(f, finder.find({"gap": f})) for f in gap_report["findings"] if f["status"] in GAP_STATUSES]
