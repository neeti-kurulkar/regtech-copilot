"""Stage 3: check_policy_gap - reconcile one company's Fair Practices Code with the current RBI Directions.

Two corpora, retrieved independently, then reconciled requirement by requirement:

  topic --search--> regulation passages --structured()--> requirements (verbatim-quoted)
  each requirement --search, scoped to the company--> policy passages --structured()--> status

Built on aip: CorpusIndex/aip.retrieval (chunk_filter scoping), aip.llm.structured (Lab 1:
schema + repair loop; the grounding check lives *inside* the schema so a fabricated quote is
repaired like any other validation error), aip.guards (untrusted delimiting, injection flags,
quote_in_source), aip.cost.Budget.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aip.chunking import STRATEGIES, Chunk, annotate_provenance
from aip.cost import Budget, map_in_context
from aip.guards import UNTRUSTED_SYSTEM_CLAUSE, delimit_untrusted, detect_injection, normalise_text, quote_in_source
from aip.llm import StructuredOutputError, structured
from aip.retrieval import DenseRetriever, Hit, format_context

from regtech.entities import resolve
from regtech.index import DEFAULT_CONFIG, CorpusIndex
from regtech.qa import source_label

GapStatus = Literal["met", "weaker", "inconsistent", "missing", "needs_review"]


# ---------------------------------------------------------------------------------------------
# The tool contract (validated at the boundary; Stage 6 hands exactly this to the agent)
# ---------------------------------------------------------------------------------------------
class PolicyGapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str | None = Field(None, max_length=120, description="NBFC name, e.g. 'IIFL Finance'")
    policy_path: str | None = Field(None, description="path to an uploaded policy (.pdf/.md/.txt) instead of an indexed one")
    topic: str = Field(min_length=3, max_length=200, description="regulatory topic, e.g. 'gold loan auctions'")
    max_requirements: int = Field(8, ge=1, le=12)

    @model_validator(mode="after")
    def _one_policy(self) -> PolicyGapRequest:
        if not self.entity and not self.policy_path:
            raise ValueError("give either `entity` (an indexed NBFC) or `policy_path` (an uploaded policy)")
        return self


class Citation(BaseModel):
    doc_id: str
    chunk_id: str
    label: str
    quote: str


class GapFinding(BaseModel):
    requirement: str
    regulation: Citation
    status: GapStatus
    policy: Citation | None = None
    explanation: str
    considered: list[str] = Field(default_factory=list, description="policy chunks the assessment read (audit trail)")

    @model_validator(mode="after")
    def _evidence_matches_status(self) -> GapFinding:
        if self.status in ("met", "weaker", "inconsistent") and self.policy is None:
            raise ValueError(f"status {self.status!r} needs a cited policy clause")
        if self.status == "missing" and self.policy is not None:
            raise ValueError("status 'missing' cannot cite a policy clause")
        return self


class GapReport(BaseModel):
    entity: str
    policy_doc_id: str
    policy_title: str
    policy_date: str | None
    policy_date_basis: str | None = None   # manifest date_basis; None for an uploaded document
    topic: str
    regulation_as_of: str | None
    policy_predates_regulation: bool | None
    findings: list[GapFinding]
    counts: dict[str, int]
    notes: list[str] = Field(default_factory=list)
    injection_flags: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0

    def _date_text(self) -> str:
        if not self.policy_date:
            return "undated"
        return {"pdf_metadata": f"{self.policy_date}, from the PDF's metadata",
                "not_before": f"issued on or after {self.policy_date}"}.get(self.policy_date_basis or "", self.policy_date)

    def render(self) -> str:
        head = [f"Policy gap check: {self.entity} - {self.topic}",
                f"Policy: {self.policy_title} ({self._date_text()})"
                + (f"  [last revised before the rules' latest update, {self.regulation_as_of}]"
                   if self.policy_predates_regulation else "")
                + (f"  [date not known exactly: cannot tell whether it reflects the rules' latest update, "
                   f"{self.regulation_as_of}]"
                   if self.policy_predates_regulation is None and self.policy_date_basis and self.regulation_as_of
                   else ""),
                "Result: " + ", ".join(f"{n} {s}" for s, n in self.counts.items() if n), ""]
        body = []
        for i, f in enumerate(self.findings, 1):
            body += [f"{i}. [{f.status.upper()}] {f.requirement}",
                     f"   Rule:   {f.regulation.label}",
                     f"           \"{f.regulation.quote}\"",
                     f"   Policy: " + (f"{f.policy.label}\n           \"{f.policy.quote}\"" if f.policy else "not found"),
                     f"   Why:    {f.explanation}", ""]
        tail = [f"Note: {n}" for n in self.notes] + [f"WARNING: {w}" for w in self.injection_flags]
        return "\n".join(head + body + tail).rstrip()


# ---------------------------------------------------------------------------------------------
# Prompts. Examples are generic on purpose: none is drawn from the evaluation cases.
# ---------------------------------------------------------------------------------------------
EXTRACT_SYSTEM = f"""\
You are a compliance analyst. From the numbered extracts of RBI Directions for NBFCs, list the
specific obligations on the given topic that an NBFC's Fair Practices Code should reflect.

Rules:
- Use ONLY the numbered sources. Each requirement is ONE concrete obligation (a duty, a time limit,
  a prohibition or a disclosure), in plain language, at most 40 words.
- Never merge obligations. If one sentence or paragraph contains two obligations (for example
  "X shall not be done, and Y shall be done"), list them as two requirements, each with the quote
  that states that obligation. A requirement must claim nothing its own quote does not state.
- When a rule lists specific practices, limits, amounts, times or deadlines, keep them: write the
  requirement with those specifics and quote the words that contain them, not only the
  introductory sentence of the list.
- Keep numbers, time limits, conditions and scope exactly as the source states them.
- If an obligation applies only to certain loans or kinds of NBFC, say so in the requirement.
- quote: copy 10 to 60 consecutive words VERBATIM from that source which state the obligation.
  Do not paraphrase, abbreviate, or join text from different places.
- Skip definitions, applicability lists, repeal clauses, and anything not about the topic.
- Return at most the requested number, most important first. If no source addresses the topic,
  return an empty list.

{UNTRUSTED_SYSTEM_CLAUSE}
"""

ASSESS_SYSTEM = f"""\
You check whether a company's Fair Practices Code satisfies ONE regulatory requirement.
You receive the REQUIREMENT with its exact regulation text, and numbered EXCERPTS from the
company's policy: the passages most relevant to this requirement.

Choose one status:
- met: the policy commits to the same obligation, or a stricter one.
- weaker: the policy covers the same subject but is vaguer, narrower in scope, or less strict
  (for example a longer deadline, a general promise where the rule sets a specific standard,
  or a required element left out).
- inconsistent: the policy states something that contradicts the requirement.
- missing: no excerpt addresses this obligation. Similar words about a different obligation
  do not count.

For met, weaker or inconsistent, give the excerpt number and copy the exact supporting words
(10 to 60 consecutive words) VERBATIM from that excerpt. For missing, give no source or quote.
explanation: one or two plain-language sentences a compliance officer can act on, saying what is
covered and what is missing or weaker. Judge only from the excerpts; never assume the company does
something the excerpts do not say. The explanation may only attribute to the policy what its
quoted words actually say (no figures, times or commitments that are not in the excerpts).

{UNTRUSTED_SYSTEM_CLAUSE}
"""


_LIST_ITEM = re.compile(r"\((\d{1,2}|[a-h]|i{1,3}|iv|v|vi{0,3})\)")
_ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii"]


def _next_marker(m: str) -> str | None:
    if m.isdigit():
        return str(int(m) + 1)
    if m in _ROMAN[:-1]:
        return _ROMAN[_ROMAN.index(m) + 1]
    return chr(ord(m) + 1) if len(m) == 1 else None


_LIST_FOLLOWS = re.compile(r"^[^()]{0,220}?:\s*\((?:1|a|i)\)")
_PARA_NO = re.compile(r"(?:^|\s)\d{1,3}\.\s")


def _cuts_list_short(quote: str, source: str) -> str | None:
    """Does the quote state a rule but stop before the enumerated list that makes it specific?

    Found in Stage 6, twice on the same rule. The RBI's rule on harsh recovery is a lead-in plus a list:
    "...following practices shall be deemed as harsh: (1) Use of threatening or abusive language (2) ... calling
    the borrower before 9:00 a.m. and after 6:00 p.m. ...". The extractor first quoted the lead-in with item (1)
    only, then (told not to) quoted just the general sentence before the list. Both times the assessor never saw
    the hours and marked Tata Capital's 08:00-19:00 calling window as met: a missed gap.
    One list item quoted on its own (no lead-in) is fine."""
    q, s = normalise_text(quote), normalise_text(source)
    at = s.find(q)
    if at < 0:
        return None
    after = s[at + len(q):]
    markers = _LIST_ITEM.findall(q)
    if markers:
        first = _LIST_ITEM.search(q)
        if len(q[:first.start()].strip()) < 20:
            return None      # starts at an item: one item as its own requirement
        nxt = _next_marker(markers[-1])
        if nxt and f"({nxt})" in after[:60]:
            return f"the quote stops after list item ({markers[-1]}), but item ({nxt}) follows directly in the source"
        return None
    lead = _LIST_FOLLOWS.match(after)
    if lead and not _PARA_NO.search(lead.group()):
        return "the text right after the quote introduces a numbered list of the specific practices or limits"
    return None


def _complete_list(quote: str, source: str) -> str | None:
    """If the quote stops before its list ends, return it extended with the rest of the list, copied from the
    source. Deterministic: asking the model to fix the quote failed twice (it cut the list a different way)."""
    if not _cuts_list_short(quote, source):
        return None
    key = quote.strip()[-25:]
    at = source.find(key)
    if at < 0:
        return None
    rest = source[at + len(key):]
    q = normalise_text(quote)
    first = _LIST_ITEM.search(q)
    if first and len(q[:first.start()].strip()) >= 20:
        nxt = _next_marker(_LIST_ITEM.findall(q)[-1])
    else:
        start = re.search(r"\((?:1|a|i)\)", rest)
        nxt = start.group()[1:-1] if start else None
    pos = 0
    while nxt:
        k = rest.find(f"({nxt})", pos)
        if k < 0 or k - pos > 400 or _PARA_NO.search(rest[pos:k]):
            break
        pos, nxt = k + 1, _next_marker(nxt)
    if pos == 0:
        return None
    end = rest.find("\n\n", pos)
    return " ".join((quote.rstrip() + rest[:len(rest) if end < 0 else end]).split())


def _requirement_schema(sources: list[str], n_max: int) -> type[BaseModel]:
    n = len(sources)

    class Requirement(BaseModel):
        requirement: str = Field(min_length=10, max_length=400, description="one obligation, plain language")
        source: int = Field(ge=1, le=max(n, 1), description="number of the source it comes from")
        quote: str = Field(min_length=15, max_length=800, description="exact consecutive words copied from that source")

        @model_validator(mode="after")
        def _grounded(self) -> Requirement:
            if not quote_in_source(self.quote, sources[self.source - 1]):
                raise ValueError(f"quote is not verbatim in source [{self.source}]; copy the exact words from it")
            if self.quote.rstrip().endswith(":"):
                raise ValueError("the quote only introduces a list (it ends with ':'); quote the listed practices, "
                                 "limits or figures themselves, as separate requirements if there are several")
            cut = _cuts_list_short(self.quote, sources[self.source - 1])
            if cut:
                completed = _complete_list(self.quote, sources[self.source - 1])
                if not completed:
                    raise ValueError(f"{cut}; quote the list items (the specific practices, limits or figures), either "
                                     "the whole list in one quote or each item as its own requirement")
                self.quote = completed
            return self

    class RequirementList(BaseModel):
        requirements: list[Requirement] = Field(default_factory=list, max_length=n_max)

    return RequirementList


_FIGURE = re.compile(r"\d")


def _assessment_schema(sources: list[str], requirement: str = "") -> type[BaseModel]:
    n = len(sources)
    requirement_has_figure = bool(_FIGURE.search(requirement))

    class Assessment(BaseModel):
        status: Literal["met", "weaker", "inconsistent", "missing"]
        source: int | None = Field(None, ge=1, le=max(n, 1), description="policy excerpt number; null if missing")
        quote: str | None = Field(None, description="exact words from that excerpt; null if missing")
        explanation: str = Field(min_length=10, max_length=700)

        @model_validator(mode="after")
        def _grounded(self) -> Assessment:
            if self.status == "missing":
                self.source, self.quote = None, None
                return self
            if not self.source or not self.quote:
                raise ValueError(f"status {self.status!r} needs the policy excerpt number and an exact quote")
            if not quote_in_source(self.quote, sources[self.source - 1]):
                raise ValueError(f"quote is not verbatim in excerpt [{self.source}]; copy the exact words from it")
            # A vague promise cannot meet a specific limit: if the rule sets a figure (days, hours,
            # amounts), 'met' needs policy words that also state one. Stricter figures still pass.
            if self.status == "met" and requirement_has_figure and not _FIGURE.search(self.quote):
                raise ValueError("the requirement sets a specific figure (a time, deadline or amount) but the quoted "
                                 "policy words state none; quote the policy words that give the figure, or if the "
                                 "policy gives no specific figure the status is 'weaker', not 'met'")
            return self

    return Assessment


# ---------------------------------------------------------------------------------------------
# Policy sources: an indexed company, or an uploaded file
# ---------------------------------------------------------------------------------------------
@dataclass
class PolicySource:
    doc_id: str
    entity: str
    title: str
    date: str | None
    search: Callable[[str, int], list[Hit]]
    chunks: list[Chunk]
    date_basis: str | None = None


def policy_label(hit: Hit) -> str:
    path = [p for p in hit.chunk.meta.get("heading", "").split(" > ")[1:] if p]
    return " | ".join(x for x in (hit.chunk.meta.get("title", hit.doc_id), " > ".join(path[-2:])) if x)


def _uploaded_source(path: Path, entity: str | None, sanitise: Callable[[str], str] | None = None) -> PolicySource:
    """Index an uploaded policy on the fly. `sanitise` (the agent's layer 2) cleans the text before chunking."""
    from regtech.pdf_to_md import convert

    title = f"{entity or path.stem} - uploaded policy"
    if path.suffix.lower() == ".pdf":
        text = convert(path, title=title, profile="generic").markdown
    else:
        text = path.read_text(encoding="utf-8")
        text = text if text.lstrip().startswith("#") else f"# {title}\n\n{text}"
    if sanitise:
        text = sanitise(text)
    cfg = DEFAULT_CONFIG["internal_policy"]
    doc_id = "upload-" + "".join(ch if ch.isalnum() else "-" for ch in path.stem.lower()).strip("-")
    chunks = STRATEGIES[cfg.strategy](text, doc_id, size=cfg.size)
    annotate_provenance(text, chunks, number_pattern=None)
    for c in chunks:
        c.meta.update({"title": title, "entity": entity or path.stem, "doc_type": "internal_policy"})
    retriever = DenseRetriever(chunks, show_progress=False)
    return PolicySource(doc_id, entity or path.stem, title, None, lambda q, k: retriever.search(q, k=k), chunks)


# ---------------------------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------------------------
class PolicyGapChecker:
    def __init__(self, reg_index: CorpusIndex | None = None, policy_index: CorpusIndex | None = None,
                 tier: str = "MAIN", reg_k: int = 8, policy_k: int = 4, workers: int = 4,
                 assess_tier: str | None = "SMALL"):
        self.reg_index = reg_index or CorpusIndex("regulation")
        self.policy_index = policy_index or CorpusIndex("internal_policy")
        self.tier, self.reg_k, self.policy_k, self.workers = tier, reg_k, policy_k, workers
        # Step 2 runs once per requirement, so it is the cost lever. SMALL matched MAIN on the Stage 3 eval in
        # two cold runs at ~40% of the cost (reports/stage6_findings.md, "Cheaper gap checks").
        self.assess_tier = assess_tier or tier
        # The rulebook for a topic does not depend on the company: extract it once and share it, so
        # every company is compared against the same requirements (and concurrent checks on one
        # topic cannot each draw a slightly different list from a non-deterministic model).
        self._requirements: dict[tuple, tuple[list[dict], list[str]]] = {}
        self._req_locks: dict[tuple, threading.Lock] = {}
        self._req_guard = threading.Lock()

    # -- policy side ------------------------------------------------------------------------
    def _policy_source(self, req: PolicyGapRequest, sanitise: Callable[[str], str] | None = None) -> PolicySource:
        if req.policy_path:
            return _uploaded_source(Path(req.policy_path), req.entity, sanitise)
        ent = resolve(req.entity or "")
        row = ent.row
        chunks = [c for c in self.policy_index.chunks if c.doc_id == ent.doc_id]
        return PolicySource(ent.doc_id, ent.name, row.title, row.publish_date.isoformat() if row.publish_date else None,
                            lambda q, k: self.policy_index.search(q, k=k, scope=ent.doc_id), chunks, row.date_basis)

    # -- step 1: requirements from the rulebook (Lab 1 pattern) --------------------------------
    def requirements_for(self, topic: str, hits: list[Hit], n_max: int) -> tuple[list[dict], list[str]]:
        key = (" ".join(topic.lower().split()), tuple(h.chunk.chunk_id for h in hits), n_max)
        with self._req_guard:
            lock = self._req_locks.setdefault(key, threading.Lock())
        with lock:
            if key not in self._requirements:
                self._requirements[key] = self.extract_requirements(topic, hits, n_max)
            items, notes = self._requirements[key]
        return [dict(r) for r in items], list(notes)

    def extract_requirements(self, topic: str, hits: list[Hit], n_max: int) -> tuple[list[dict], list[str]]:
        sources = [h.text for h in hits]
        prompt = (f"{delimit_untrusted(format_context(hits, label=source_label))}\n\n"
                  f"Topic: {topic}\nList at most {n_max} requirements.")
        notes: list[str] = []
        try:
            out = structured(prompt, schema=_requirement_schema(sources, n_max), system=EXTRACT_SYSTEM,
                             tier=self.tier, max_tokens=4096)
            items = [r.model_dump() for r in out.requirements]
        except StructuredOutputError:
            # Grounding kept failing for some item: take what the model gave, keep only verified quotes.
            loose = structured(prompt, schema=_loose_requirements(len(sources), n_max),
                               system=EXTRACT_SYSTEM, tier=self.tier, max_tokens=4096)
            items = [r.model_dump() for r in loose.requirements if quote_in_source(r.quote, sources[r.source - 1])]
            for r in items:   # the fallback must not reintroduce a list cut short
                r["quote"] = _complete_list(r["quote"], sources[r["source"] - 1]) or r["quote"]
            notes.append(f"{len(loose.requirements) - len(items)} extracted requirement(s) dropped: quote not found in the cited rule")
        return items, notes

    # -- step 2: per requirement, search the policy and assess ---------------------------------
    def assess(self, requirement: dict, reg_hit: Hit, policy: PolicySource) -> tuple[GapFinding, list[str]]:
        query = f"{requirement['requirement']} {requirement['quote']}"
        hits = policy.search(query, self.policy_k)
        flags = sorted({s for h in hits for s in detect_injection(h.text).signals})
        reg_cite = Citation(doc_id=reg_hit.doc_id, chunk_id=reg_hit.chunk.chunk_id, label=source_label(reg_hit),
                            quote=requirement["quote"])
        prompt = (f"REQUIREMENT: {requirement['requirement']}\n"
                  f"REGULATION TEXT ({source_label(reg_hit)}): \"{requirement['quote']}\"\n\n"
                  f"EXCERPTS FROM {policy.entity.upper()}'S POLICY:\n"
                  f"{delimit_untrusted(format_context(hits, label=policy_label))}")
        considered = [h.chunk.chunk_id for h in hits]
        try:
            a = structured(prompt, schema=_assessment_schema([h.text for h in hits], requirement["requirement"]),
                           system=ASSESS_SYSTEM,
                           tier=self.assess_tier, max_tokens=4096)
        except StructuredOutputError as exc:
            # Fail closed: an assessment we cannot ground is not reported as met or missing.
            return GapFinding(requirement=requirement["requirement"], regulation=reg_cite, status="needs_review",
                              explanation=f"Could not produce a grounded assessment ({str(exc)[:160]}).",
                              considered=considered), flags
        pol = None
        if a.status != "missing":
            h = hits[a.source - 1]
            pol = Citation(doc_id=h.doc_id, chunk_id=h.chunk.chunk_id, label=policy_label(h), quote=a.quote)
        return GapFinding(requirement=requirement["requirement"], regulation=reg_cite, status=a.status,
                          policy=pol, explanation=a.explanation, considered=considered), flags

    # -- the whole tool --------------------------------------------------------------------------
    def check(self, request: PolicyGapRequest | dict, sanitise: Callable[[str], str] | None = None) -> GapReport:
        req = request if isinstance(request, PolicyGapRequest) else PolicyGapRequest.model_validate(request)
        with Budget(limit_usd=0.25, label="check_policy_gap") as b:
            policy = self._policy_source(req, sanitise)
            reg_hits = self.reg_index.search(req.topic, k=self.reg_k)
            requirements, notes = self.requirements_for(req.topic, reg_hits, req.max_requirements)
            findings: list[GapFinding] = []
            flags: set[str] = set()
            if not requirements:
                notes.append("No requirement on this topic was found in the regulation corpus; nothing to compare.")
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = map_in_context(pool, lambda r: self.assess(r, reg_hits[r["source"] - 1], policy), requirements)
            for finding, f in results:
                findings.append(finding)
                flags.update(f)
        reg_dates = sorted({h.chunk.meta.get("publish_date", "") for h in reg_hits
                            if any(f.regulation.chunk_id == h.chunk.chunk_id for f in findings)} - {""})
        as_of = reg_dates[-1] if reg_dates else None
        predates = policy_predates(policy.date, policy.date_basis, as_of)
        counts = {s: sum(f.status == s for f in findings) for s in ("met", "weaker", "inconsistent", "missing", "needs_review")}
        injection = [f"policy text matched prompt-injection signatures ({', '.join(sorted(flags))}); "
                     "treated as data, but review the document"] if flags else []
        return GapReport(entity=policy.entity, policy_doc_id=policy.doc_id, policy_title=policy.title,
                         policy_date=policy.date, policy_date_basis=policy.date_basis, topic=req.topic, regulation_as_of=as_of,
                         policy_predates_regulation=predates, findings=findings, counts=counts, notes=notes,
                         injection_flags=injection, cost_usd=round(b.spent_usd, 6))


def policy_predates(policy_date: str | None, basis: str | None, rules_as_of: str | None) -> bool | None:
    """True / False when the dates settle it; None when they cannot. An exact date (stated or PDF metadata) is
    compared directly. A 'not_before' date settles only one direction: issued on or after a date later than the
    rules means current, anything else is unknown. Undated or uploaded documents: unknown."""
    if not policy_date or not rules_as_of:
        return None
    if basis in ("stated", "pdf_metadata", None):
        return policy_date < rules_as_of if basis else None
    if basis == "not_before":
        return False if policy_date >= rules_as_of else None
    return None


def _loose_requirements(n_sources: int, n_max: int) -> type[BaseModel]:
    class Requirement(BaseModel):
        requirement: str
        source: int = Field(ge=1, le=max(n_sources, 1))
        quote: str

    class RequirementList(BaseModel):
        requirements: list[Requirement] = Field(default_factory=list, max_length=n_max)

    return RequirementList


def check_policy_gap(**kwargs) -> GapReport:
    """Tool entry point: validates arguments against PolicyGapRequest, returns a GapReport."""
    return PolicyGapChecker().check(PolicyGapRequest(**kwargs))


__all__ = ["PolicyGapRequest", "GapFinding", "GapReport", "Citation", "PolicyGapChecker", "check_policy_gap"]
