from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from regtech.paths import CORPUS_DIRS, MANIFEST_PATH

COLUMNS = ["doc_id", "doc_type", "entity_name", "publish_date", "source_url", "file_name"]

DOC_ID_PREFIX = {"regulation": "reg", "internal_policy": "fpc", "enforcement": "enf"}
_DOC_ID_RE = re.compile(r"^(reg|fpc|enf)-[a-z0-9]+(?:-[a-z0-9]+)*$")


class ManifestRow(BaseModel):
    doc_id: str
    doc_type: Literal["regulation", "internal_policy", "enforcement"]
    entity_name: str
    publish_date: date | None = None
    source_url: str | None = None
    file_name: str

    @field_validator("publish_date", "source_url", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("doc_id")
    @classmethod
    def _doc_id_format(cls, v: str) -> str:
        if not _DOC_ID_RE.match(v):
            raise ValueError(f"doc_id {v!r} must look like 'reg-kyc' / 'fpc-iifl' / 'enf-iifl-2026-02-13'")
        return v

    @field_validator("entity_name", "file_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v.strip()

    @field_validator("source_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("source_url must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def _prefix_matches_type(self) -> ManifestRow:
        expected = DOC_ID_PREFIX[self.doc_type]
        if not self.doc_id.startswith(expected + "-"):
            raise ValueError(f"doc_id {self.doc_id!r} should start with '{expected}-' for doc_type {self.doc_type!r}")
        return self

    @property
    def path(self) -> Path:
        return CORPUS_DIRS[self.doc_type] / self.file_name


class ManifestError(ValueError):
    pass


def init_manifest(path: Path = MANIFEST_PATH) -> bool:
    """Create an empty manifest with the header row. Returns False if one already exists."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(COLUMNS)
    return True


def load_manifest(path: Path = MANIFEST_PATH, *, check_files: bool = False) -> list[ManifestRow]:
    """Read and validate every row; collects all problems before raising."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ManifestError(f"header is {reader.fieldnames}, expected {COLUMNS}")
        raw_rows = list(reader)

    rows: list[ManifestRow] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(raw_rows, start=2):
        try:
            row = ManifestRow(**raw)
        except ValidationError as e:
            errors.extend(f"line {line_no}: {err['loc'][0] if err['loc'] else ''} {err['msg']}" for err in e.errors())
            continue
        if row.doc_id in seen:
            errors.append(f"line {line_no}: duplicate doc_id {row.doc_id!r}")
        seen.add(row.doc_id)
        if check_files and not row.path.exists():
            errors.append(f"line {line_no}: file not found: {row.path}")
        rows.append(row)

    if errors:
        raise ManifestError("manifest has problems:\n  " + "\n  ".join(errors))
    return rows


def write_manifest(rows: list[ManifestRow], path: Path = MANIFEST_PATH) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            d = r.model_dump()
            d["publish_date"] = d["publish_date"].isoformat() if d["publish_date"] else ""
            d["source_url"] = d["source_url"] or ""
            w.writerow(d)
