"""PDF -> Markdown that keeps the heading structure needed for clause-level citation.

Regulations get pattern-based levels (Chapter -> ##, "A." section -> ###,
"A.1" subsection -> ####). Policies and press releases use font cues
(bold / larger than body text). Numbered paragraphs stay inline so a chunk
still carries its paragraph number.
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf


@dataclass
class Line:
    text: str
    size: float
    bold: bool
    page: int
    x0: float
    y0: float
    y1: float
    page_h: float
    table_md: str | None = None


@dataclass(frozen=True)
class DocRules:
    """Per-document cleanup for layouts the generic rules cannot handle."""

    drop_exact: frozenset[str] = frozenset()
    drop_sizes: tuple[float, ...] = ()
    start_at: str | None = None
    end_before: str | None = None


DOC_RULES: dict[str, DocRules] = {
    "fpc-mahindra-finance": DocRules(
        drop_exact=frozenset({"Home > Customer Service > Fair Practice code", "Quick Pay", "Apply Now",
                              "Grievance & Support", "Fraud Advisory", "English"}),
        start_at=r"^Mahindra & Mahindra Financial Services Limited",
        end_before=r"^Loans$",
    ),
    "fpc-manappuram": DocRules(
        drop_sizes=(15.0,),
        start_at=r"^MANAPPURAM FINANCE LIMITED",
        end_before=r"^SI No$",
    ),
}

_NON_LATIN = re.compile(r"[ऀ-ॿᰀ-᳿]")
_DOT_LEADER = re.compile(r"\.{6,}|…{3,}|(?:\. ){5,}")
_PAGE_NO = re.compile(r"^(?:page\s*)?\d{1,3}(?:\s*(?:of|/)\s*\d{1,3})?$", re.I)
_TOC_MARKER = re.compile(r"^(?:table of contents|contents|index)$", re.I)
_LONE_ENUM = re.compile(r"^(?:(?:[A-Z]|[IVXL]{1,5})[.)]|[A-Z]\.\d{1,2}(?:\.\d{1,2})*)$")
_ENUM = re.compile(
    r"^(?:\(?\d{1,3}(?:\.\d{1,3})*[.)]|\([a-zA-Z0-9]{1,4}\)|[a-zA-Z][.)]|[ivxlIVXL]{1,5}[.)]|[•▪●◦▪\-–]\s)"
)
_LOWER_ENUM = re.compile(r"^(?:\(?\d{1,3}(?:\.\d{1,3})*[.)]|\([a-z0-9]{1,4}\)|[a-z][.)]|[ivxl]{1,5}[.)]|[•▪●◦\-–]\s)")

_REG_CHAPTER = re.compile(r"^(?:CHAPTER|Chapter)\s*[-–]?\s*[IVXLC]+\b")
_REG_ANNEX = re.compile(r"^(?:Annex|ANNEX|Appendix|APPENDIX|Schedule|SCHEDULE|Part|PART)\b")
_REG_SECTION = re.compile(r"^[A-Z]\.\s*[A-Za-z(]")
_REG_SUB = re.compile(r"^[A-Z]\.(\d+)(\.\d+)*\s")

_DIGIT_ENUM = re.compile(r"^\d{1,2}(?:\.\d{1,2})*\.?\s*[A-Za-z]")
_UPPER_ENUM_TITLE =re.compile(r"^(?:[A-Z]|[IVXL]{1,5})[.)]\s*[A-Z][^.;:]{2,80}$")
_PRESS_LETTERHEAD = re.compile(r"^RESERVE BANK OF INDIA$|www\.rbi\.org\.in|^Department of Communication", re.I)

_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.I)


def _table_markdown(t) -> str | None:
    """Render a detected table, or None if it looks like a layout box rather than a table.

    Checked on the rendered markdown because merged cells are repeated there,
    which is exactly what duplicates text in the output.
    """
    md = t.to_markdown(clean=False).strip()
    if _DOT_LEADER.search(md):
        return None
    rows =[r for r in md.splitlines() if r.startswith("|") and not set(r) <= set("|-: ")]
    body = rows[1:]
    cells = [c.replace("<br>", " ").strip() for r in body for c in r.strip("|").split("|")]
    filled = [c for c in cells if c]
    if len(filled) < 4 or len(filled) < 0.5 * len(cells):
        return None
    multi = [[c for c in (x.replace("<br>", " ").strip() for x in r.strip("|").split("|")) if c] for r in body]
    multi = [r for r in multi if len(r) >= 2]
    dup = [r for r in multi if len(set(r)) < len(r)]
    if multi and len(dup) / len(multi) > 0.2:
        return None
    return md


def _extract_lines(pdf: Path, *, tables_enabled: bool = True) -> list[Line]:
    doc = pymupdf.open(pdf)
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES
    out: list[Line] = []
    for pno, page in enumerate(doc):
        tables: list[tuple[pymupdf.Rect, str]] = []
        if tables_enabled:
            try:
                for t in page.find_tables().tables:
                    md = _table_markdown(t) if t.col_count >= 2 and t.row_count >= 2 else None
                    if md:
                        tables.append((pymupdf.Rect(t.bbox), md))
            except Exception:
                tables = []
        boxes = [r for r, _ in tables]
        page_lines: list[Line] = []
        for block in page.get_text("dict", flags=flags, sort=True)["blocks"]:
            for ln in block.get("lines", []):
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                bbox = pymupdf.Rect(ln["bbox"])
                if any(b.contains(bbox) or (b & bbox).get_area() > 0.6 * bbox.get_area() for b in boxes):
                    continue
                text = " ".join("".join(s["text"] for s in spans).replace("\xa0", " ").split())
                page_lines.append(Line(
                    text=text,
                    size=round(max(s["size"] for s in spans), 1),
                    bold=all((s["flags"] & 16) or _BOLD_FONT.search(s["font"]) for s in spans),
                    page=pno, x0=bbox.x0, y0=bbox.y0, y1=bbox.y1, page_h=page.rect.height,
                ))
        for r, md in tables:
            page_lines.append(Line(text="", size=0, bold=False, page=pno, x0=r.x0, y0=r.y0,
                                   y1=r.y1, page_h=page.rect.height, table_md=md))
        page_lines.sort(key=lambda l: (round(l.y0, 0), l.x0))
        out.extend(page_lines)
    return out


def _norm_furniture(text: str) -> str:
    return re.sub(r"\d+", "", text.lower()).replace(" ", "")


def _drop_page_furniture(lines: list[Line]) -> list[Line]:
    n_pages = len({l.page for l in lines}) or 1
    margin = 0.08
    edge = [l for l in lines if l.table_md is None and (l.y0 < l.page_h * margin or l.y1 > l.page_h * (1 - margin))]
    pages_by_text: dict[str, set[int]] = collections.defaultdict(set)
    for l in edge:
        pages_by_text[_norm_furniture(l.text)].add(l.page)
    threshold = max(2, int(0.3 * n_pages))
    repeated = {k for k, pages in pages_by_text.items() if len(pages) >= threshold}
    kept = []
    for l in lines:
        if l.table_md is None:
            is_edge = l.y0 < l.page_h * margin or l.y1 > l.page_h * (1 - margin)
            key = _norm_furniture(l.text)
            if (is_edge and (key in repeated or not key)) or _PAGE_NO.match(l.text):
                continue
        kept.append(l)
    return kept


def _drop_toc(lines: list[Line]) -> list[Line]:
    by_page: dict[int, list[int]] = collections.defaultdict(list)
    for i, l in enumerate(lines):
        by_page[l.page].append(i)
    drop: set[int] = set()
    for page, idxs in by_page.items():
        leaders = [i for i in idxs if lines[i].table_md is None and _DOT_LEADER.search(lines[i].text)]
        if len(leaders) < 3:
            drop.update(i for i in leaders)
            continue
        markers = [i for i in idxs if _TOC_MARKER.match(lines[i].text or "")]
        start = markers[0] if markers else idxs[0]
        drop.update(i for i in idxs if start <= i <= leaders[-1])
    for i, l in enumerate(lines):
        if l.table_md is None and _TOC_MARKER.match(l.text):
            drop.add(i)
    return [l for i, l in enumerate(lines) if i not in drop]


def _merge_lone_enumerators(lines: list[Line]) -> list[Line]:
    out: list[Line] = []
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.table_md is None and _LONE_ENUM.match(l.text) and i + 1 < len(lines) and lines[i + 1].table_md is None:
            nxt = lines[i + 1]
            out.append(Line(f"{l.text} {nxt.text}", max(l.size, nxt.size), l.bold and nxt.bold,
                            nxt.page, l.x0, l.y0, nxt.y1, nxt.page_h))
            i += 2
            continue
        out.append(l)
        i += 1
    return out


def _apply_rules(lines: list[Line], rules: DocRules) -> list[Line]:
    lines = [l for l in lines if l.table_md is not None or
             (l.text not in rules.drop_exact and l.size not in rules.drop_sizes)]
    if rules.start_at:
        for i, l in enumerate(lines):
            if l.table_md is None and re.search(rules.start_at, l.text):
                lines = lines[i:]
                break
    if rules.end_before:
        for i, l in enumerate(lines):
            hay = l.text if l.table_md is None else l.table_md.replace("|", "\n")
            if re.search(rules.end_before, hay, re.M):
                lines = lines[:i]
                break
    return lines


def _body_size(lines: list[Line]) -> float:
    mass: collections.Counter[float] = collections.Counter()
    for l in lines:
        if l.table_md is None and not l.bold and len(l.text) >= 40:
            mass[l.size] += len(l.text)
    if not mass:
        for l in lines:
            if l.table_md is None:
                mass[l.size] += len(l.text)
    return mass.most_common(1)[0][0] if mass else 11.0


def _heading_level(l: Line, profile: str, body: float, top_heading_size: float | None) -> int | None:
    t = l.text
    if l.table_md is not None or len(t) > 120 or sum(c.isalpha() for c in t) < 3 or t.endswith((",", ";")):
        return None
    if profile == "enforcement" or (l.page == 0 and l.size >= body + 5):
        return None
    if profile == "regulation":
        if not l.bold:
            return None
        if _REG_CHAPTER.match(t) or _REG_ANNEX.match(t):
            return 2
        m = _REG_SUB.match(t)
        if m:
            return 4 if m.group(2) is None else 5
        if _REG_SECTION.match(t):
            return 3
        if _LOWER_ENUM.match(t) or t.endswith("."):
            return None
        return 4
    larger = l.size >= body + 0.9
    if l.bold:
        if _DIGIT_ENUM.match(t):
            return 3 if len(t) <= 100 and not t.endswith(".") else None
        if _LOWER_ENUM.match(t):
            return 3 if len(t) <= 60 else None
    elif larger:
        first_alpha = next((c for c in t if c.isalpha()), "")
        if len(t) > 70 or _LOWER_ENUM.match(t) or not first_alpha.isupper():
            return None
    else:
        return 2 if _UPPER_ENUM_TITLE.match(t) else None
    if top_heading_size is not None and l.size < top_heading_size:
        return 3
    return 2


def _is_heading_candidate_size(l: Line, profile: str, body: float) -> bool:
    return _heading_level(l, profile, body, None) is not None


def _assemble(lines: list[Line], profile: str) -> list[tuple[str, str | int]]:
    """Returns blocks: ("h", level) pairs interleaved with text, as (kind, payload)."""
    body = _body_size(lines)
    sizes = [l.size for l in lines if _is_heading_candidate_size(l, profile, body)]
    top = max(sizes) if sizes and len(set(sizes)) > 1 else None

    blocks: list[tuple[str, str | int]] = []
    para: list[Line] = []

    def flush():
        if para:
            text = ""
            for ln in para:
                if text.endswith("-") and ln.text[:1].islower():
                    text = text[:-1] + ln.text
                else:
                    text = f"{text} {ln.text}" if text else ln.text
            blocks.append(("p", text))
            para.clear()

    prev_heading: tuple[Line, int] | None = None
    for l in lines:
        if l.table_md is not None:
            flush()
            prev_heading = None
            blocks.append(("t", l.table_md))
            continue
        level = _heading_level(l, profile, body, top)
        if level is not None:
            flush()
            continuation = (
                prev_heading is not None and blocks and blocks[-1][0] == "h"
                and prev_heading[0].size == l.size and prev_heading[0].page == l.page
                and not prev_heading[0].text.endswith((":", ".")) and not _ENUM.match(l.text)
                and not _REG_CHAPTER.match(l.text) and not _REG_ANNEX.match(l.text)
                and (prev_heading[1] == level or profile == "regulation")
                and len(prev_heading[0].text) + len(l.text) <= 200
            )
            if continuation:
                level = prev_heading[1]
                merged = f"{blocks[-1][1][1]} {l.text}"
                blocks[-1] = ("h", (level, merged))
                prev_heading = (Line(merged, l.size, l.bold, l.page, l.x0, l.y0, l.y1, l.page_h), level)
            else:
                blocks.append(("h", (level, l.text)))
                prev_heading = (l, level)
            continue
        prev_heading = None
        if para:
            last = para[-1]
            gap = l.y0 - last.y1
            height = max(last.y1 - last.y0, 1.0)
            new_para = (
                _ENUM.match(l.text) is not None
                or (l.page == last.page and gap > 0.9 * height)
                or (l.page != last.page and last.text.endswith((".", ":", ";")) and l.text[:1].isupper())
            )
            if new_para:
                flush()
        para.append(l)
    flush()
    return blocks


@dataclass
class Conversion:
    markdown: str
    n_headings: int
    n_tables: int
    chars_in: int
    chars_out: int
    heading_levels: dict[int, int] = field(default_factory=dict)


def convert(pdf: Path, *, title: str, profile: str, doc_id: str = "") -> Conversion:
    raw = _extract_lines(pdf, tables_enabled=profile != "enforcement")
    chars_in = sum(len(l.text) for l in raw)
    lines = [l for l in raw if l.table_md is not None or not _NON_LATIN.search(l.text)]
    if profile == "enforcement":
        lines = [l for l in lines if l.table_md is not None or not _PRESS_LETTERHEAD.search(l.text)]
    lines = _apply_rules(lines, DOC_RULES.get(doc_id, DocRules()))
    lines = _drop_page_furniture(lines)
    lines = _merge_lone_enumerators(lines)
    lines = _drop_toc(lines)
    blocks = _assemble(lines, profile)

    parts = [f"# {title}"]
    levels: collections.Counter[int] = collections.Counter()
    n_tables = 0
    for kind, payload in blocks:
        if kind == "h":
            level, text = payload
            levels[level] += 1
            parts.append(f"{'#' * level} {text}")
        elif kind == "t":
            n_tables += 1
            parts.append(payload)
        else:
            parts.append(payload)
    md = "\n\n".join(parts) + "\n"
    return Conversion(md, sum(levels.values()), n_tables, chars_in, len(md), dict(sorted(levels.items())))
