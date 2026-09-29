"""Stage 4, part 1: the enforcement case table (Lab 1: validated structured extraction).

Each RBI penalty press release becomes one CaseRecord via aip.llm.structured. Grounding is part
of the schema: every charge, direction name and the amount text must be verbatim in the release,
and the press-release date must match the manifest, so a wrong field is repaired by aip's repair
loop, not trusted. The rupee amount is parsed by code from the verbatim text, never by the model.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from aip.cost import Budget
from aip.guards import UNTRUSTED_SYSTEM_CLAUSE, delimit_untrusted, quote_in_source
from aip.llm import structured

from regtech.index import load_documents
from regtech.paths import DATA_DIR

CASES_PATH = DATA_DIR / "processed" / "enforcement_cases.json"

Category = Literal[
    "kyc_aml", "fair_practices_code", "interest_and_charges", "grievance_redressal", "asset_classification",
    "credit_reporting", "governance", "microfinance", "securitisation", "gold_loans", "other",
]

_AMOUNT = re.compile(r"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)\s*(lakh|crore)?", re.I)


def parse_inr(text: str) -> int:
    """'₹6.20 lakh' -> 620000; '₹3,10,000' -> 310000; '₹1.5 crore' -> 15000000."""
    m = _AMOUNT.search(text)
    if not m:
        raise ValueError(f"no rupee amount in {text!r}")
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return round(value * {"lakh": 100_000, "crore": 10_000_000}.get(unit, 1))


class CaseRecord(BaseModel):
    doc_id: str
    entity: str
    press_release_date: date
    order_date: date | None
    penalty_text: str
    penalty_inr: int
    statute: str
    directions: list[str]
    charges: list[str]
    categories: list[Category]
    inspection_as_of: date | None = None
    press_release_no: str | None = None
    title: str = ""


def _extraction_schema(source: str, published: date) -> type[BaseModel]:
    class Extraction(BaseModel):
        entity: str = Field(description="penalised company's full name, as written")
        order_date: date | None = Field(description="date of the RBI order (YYYY-MM-DD), if stated")
        press_release_date: date = Field(description="date at the top of the release (YYYY-MM-DD)")
        penalty_text: str = Field(description="the amount exactly as written, e.g. '₹6.20 lakh'")
        statute: str = Field(description="the legal provision exactly as written, e.g. 'Section 58G(1)(b) read with ...'")
        directions: list[str] = Field(description="names of the directions breached, each copied exactly as written")
        charges: list[str] = Field(min_length=1, description="each charge that was sustained, copied verbatim, one per item")
        categories: list[Category] = Field(min_length=1, description="violation categories that apply")
        inspection_as_of: date | None = Field(None, description="'financial position as on' date, if stated")
        press_release_no: str | None = Field(None, description="e.g. '2026-2027/501', if stated")

        @field_validator("penalty_text")
        @classmethod
        def _amount(cls, v: str) -> str:
            if not quote_in_source(v, source) or not _AMOUNT.search(v):
                raise ValueError("penalty_text must be the rupee amount copied exactly from the release, e.g. '₹6.20 lakh'")
            return v

        @model_validator(mode="after")
        def _grounded(self) -> Extraction:
            if self.press_release_date != published:
                raise ValueError(f"press_release_date must be the date at the top of the release ({published})")
            for field in ("charges", "directions"):
                for item in getattr(self, field):
                    if not quote_in_source(item, source):
                        raise ValueError(f"{field}: {item[:60]!r} is not verbatim in the release; copy it exactly")
            if not quote_in_source(self.statute, source):
                raise ValueError("statute must be copied exactly from the release")
            return self

    return Extraction


EXTRACT_SYSTEM = f"""\
You extract a structured record from one RBI press release announcing a monetary penalty.
Copy text fields EXACTLY as written in the release (names, amount, legal provision, directions,
charges); dates as YYYY-MM-DD. List each sustained charge as a separate item, verbatim, without
its list numbering. Choose every violation category that applies to the charges.

{UNTRUSTED_SYSTEM_CLAUSE}
"""


def extract_case(doc_id: str, title: str, published: date, text: str, tier: str = "MAIN") -> CaseRecord:
    schema = _extraction_schema(text, published)
    out = structured(delimit_untrusted(text), schema=schema, system=EXTRACT_SYSTEM, tier=tier, max_tokens=4096)
    return CaseRecord(doc_id=doc_id, title=title, penalty_inr=parse_inr(out.penalty_text), **out.model_dump())


def build_cases() -> list[CaseRecord]:
    with Budget(limit_usd=0.5, label="build-enforcement-cases") as b:
        cases = [extract_case(row.doc_id, row.title, row.publish_date, text) for row, text in load_documents("enforcement")]
    CASES_PATH.write_text(json.dumps([c.model_dump(mode="json") for c in cases], indent=2, ensure_ascii=False),
                          encoding="utf-8")
    print(b.report())
    return cases


def load_cases() -> dict[str, CaseRecord]:
    if not CASES_PATH.exists():
        raise FileNotFoundError(f"{CASES_PATH} not found; run `python -m regtech build-cases` first")
    return {c["doc_id"]: CaseRecord.model_validate(c) for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}
