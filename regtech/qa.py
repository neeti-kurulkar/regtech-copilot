"""Stage 2: citation-enforced Q&A over the regulation corpus (aip.rag.RagPipeline).

aip supplies the pipeline (retrieve -> generate -> validate -> repair -> fail
closed), numbered context, untrusted-content delimiting and citation checks.
This module supplies what is specific to RBI Directions: the answer rules, the
source labels ("KYC Directions | Chapter VI > B. ... | paras 38-40") and the
rendering of citations for a reader.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from aip.guards import UNTRUSTED_SYSTEM_CLAUSE
from aip.rag import REFUSAL, RagAnswer, RagPipeline
from aip.retrieval import Hit

from regtech.index import CorpusIndex

_RULES_COMMON = """\
3. Every factual sentence ends with the citation of the source(s) that support it, e.g. [1] or [2][4].
   Never cite a number you were not given.
4. Quote numbers, time limits, amounts and thresholds exactly as the source states them
   (e.g. "within 21 days", "at least once in every two years") and keep their conditions and exceptions.
5. If a source applies only to a particular kind of NBFC (e.g. NBFC-MFIs, Upper or Middle Layer),
   say so. Never generalise a rule for one kind of NBFC to all NBFCs.
6. When the source header gives a paragraph number, name it (e.g. "para 19 of the Responsible
   Business Conduct Directions").
7. If sources disagree, say so and cite both.
8. Be concise: one to four sentences, unless the question asks for a list.
"""

ANSWER_SYSTEM_BALANCED = f"""\
You are a compliance analyst answering questions about the Reserve Bank of India's Directions for
Non-Banking Financial Companies (NBFCs), using ONLY the numbered sources provided. Each source header
names the Direction, the chapter/section, and the paragraph numbers it contains.

Rules, in priority order:
1. If the sources do not answer the question at all, reply exactly:
   "{REFUSAL}"
   Do not use general knowledge of Indian regulation, even if you are confident it is right.
2. If the sources answer only part of the question, answer that part with citations, then state
   plainly which part the sources do not cover. Do not begin with the refusal sentence in that case.
{_RULES_COMMON}
{UNTRUSTED_SYSTEM_CLAUSE}
"""

# Lab 4 C4: the same rules with the refusal dial turned up.
ANSWER_SYSTEM_STRICT = ANSWER_SYSTEM_BALANCED.replace(
    """2. If the sources answer only part of the question, answer that part with citations, then state
   plainly which part the sources do not cover. Do not begin with the refusal sentence in that case.""",
    f"""2. If the sources do not fully answer every part of the question, reply exactly:
   "{REFUSAL}"
   Answer only when every part is supported.""",
)

VARIANTS = {"balanced": ANSWER_SYSTEM_BALANCED, "strict": ANSWER_SYSTEM_STRICT}

_SHORT_TITLE = re.compile(r"\((?:Non-Banking Financial Companies\s*[–-]\s*)?(.+?)\) Directions, (\d{4})")


def short_title(title: str) -> str:
    m = _SHORT_TITLE.search(title)
    return f"{m.group(1)} Directions, {m.group(2)}" if m else title


def paragraph_range(numbers: list[str]) -> str:
    if not numbers:
        return ""
    return f"para {numbers[0]}" if len(numbers) == 1 else f"paras {numbers[0]}-{numbers[-1]}"


def source_label(hit: Hit) -> str:
    """Human-readable source header: Direction | section path | paragraph range."""
    meta = hit.chunk.meta
    path = [p for p in meta.get("heading", "").split(" > ")[1:] if p and not p.startswith("Reserve Bank of India")]
    parts = [short_title(meta.get("title", hit.doc_id)), " > ".join(path[-2:]), paragraph_range(meta.get("numbers", []))]
    return " | ".join(p for p in parts if p)


@dataclass
class Answer:
    question: str
    text: str
    refused: bool
    citations_valid: bool
    sources: list[dict]
    cited: list[int]
    repairs: int
    fallback: bool
    problems: list[str]
    budget_doublings: int = 0

    def render(self) -> str:
        lines = [self.text, ""]
        if self.cited:
            lines.append("Sources:")
            lines += [f"  [{i}] {self.sources[i - 1]['label']}" for i in self.cited]
        return "\n".join(lines).rstrip()


class RegulationQA:
    """Grounded Q&A over the RBI Directions corpus."""

    def __init__(self, variant: str = "balanced", k: int = 8, final_k: int = 6, index: CorpusIndex | None = None,
                 tier: str = "MAIN"):
        self.variant, self.tier = variant, tier
        self.index = index or CorpusIndex("regulation")
        self.pipeline = RagPipeline(
            self.index, k=k, final_k=final_k, tier=tier, system=VARIANTS[variant],
            # MAIN is a reasoning model: its hidden thinking counts against max_tokens, and 900
            # truncated 4 of 31 answers in the first Stage 2 run (reports/stage2_qa.md).
            max_repairs=1, fail_closed=True, context_label=source_label, max_tokens=2048,
        )

    def ask(self, question: str) -> Answer:
        return self._wrap(self.pipeline.answer(question))

    def ask_with_context(self, question: str, hits: list[Hit]) -> Answer:
        return self._wrap(self.pipeline.answer_from_hits(question, hits))

    @staticmethod
    def _wrap(r: RagAnswer) -> Answer:
        sources = [{"n": i, "doc_id": h.doc_id, "chunk_id": h.chunk.chunk_id, "label": source_label(h),
                    "text": h.text} for i, h in enumerate(r.hits, start=1)]
        return Answer(r.question, r.answer, r.refused, r.citations_valid, sources, r.cited_indices,
                      r.repairs, r.fallback, r.problems, r.budget_doublings)
