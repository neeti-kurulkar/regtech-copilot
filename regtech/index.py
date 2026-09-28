"""Three independent indices (one per corpus), built from aip.chunking + aip.retrieval."""
from __future__ import annotations

from dataclasses import dataclass

from aip.chunking import STRATEGIES, Chunk, annotate_provenance
from aip.retrieval import Bm25Retriever, DenseRetriever, Hit, HybridRetriever, Retriever

from regtech.ingest import processed_path
from regtech.manifest import ManifestRow, load_manifest


@dataclass(frozen=True)
class IndexConfig:
    strategy: str = "markdown"
    size: int = 800
    retriever: str = "dense"

    @property
    def label(self) -> str:
        return f"{self.strategy}@{self.size}/{self.retriever}"


# Chosen from the Stage 1 retrieval evaluation (reports/stage1_retrieval.md).
DEFAULT_CONFIG: dict[str, IndexConfig] = {
    "regulation": IndexConfig("sliding", 800, "dense"),
    "internal_policy": IndexConfig("sliding", 1600, "dense"),
    "enforcement": IndexConfig("markdown", 800, "dense"),
}


def load_documents(doc_type: str) -> list[tuple[ManifestRow, str]]:
    rows = [r for r in load_manifest() if r.doc_type == doc_type]
    return [(r, processed_path(r).read_text(encoding="utf-8")) for r in rows]


def build_chunks(doc_type: str, strategy: str = "markdown", size: int = 800) -> list[Chunk]:
    chunker = STRATEGIES[strategy]
    chunks: list[Chunk] = []
    for row, text in load_documents(doc_type):
        pieces = chunker(text, row.doc_id) if strategy == "whole" else chunker(text, row.doc_id, size=size)
        # Numbered paragraphs ("12.") are the citation unit in RBI directions only.
        annotate_provenance(text, pieces, number_pattern=r"(?m)^(\d{1,3})\.\s" if doc_type == "regulation" else None)
        for c in pieces:
            c.meta.update({
                "doc_type": doc_type,
                "entity": row.entity_name,
                "title": row.title,
                "publish_date": row.publish_date.isoformat() if row.publish_date else "",
            })
        chunks.extend(pieces)
    return chunks


def _make_retriever(kind: str, chunks: list[Chunk]) -> Retriever:
    if kind == "dense":
        return DenseRetriever(chunks, show_progress=False)
    if kind == "bm25":
        return Bm25Retriever(chunks)
    if kind == "hybrid":
        return HybridRetriever([DenseRetriever(chunks, show_progress=False), Bm25Retriever(chunks)])
    raise ValueError(f"unknown retriever {kind!r}")


class CorpusIndex:
    """One corpus, one chunking config. `scope` restricts a search to a single document."""

    def __init__(self, doc_type: str, config: IndexConfig | None = None):
        self.doc_type = doc_type
        self.config = config or DEFAULT_CONFIG[doc_type]
        self.chunks = build_chunks(doc_type, self.config.strategy, self.config.size)
        self.retriever = _make_retriever(self.config.retriever, self.chunks)

    @property
    def doc_ids(self) -> list[str]:
        return sorted({c.doc_id for c in self.chunks})

    def search(self, query: str, k: int = 8, scope: str | None = None) -> list[Hit]:
        if scope is None:
            return self.retriever.search(query, k=k)
        if scope not in self.doc_ids:
            raise KeyError(f"no chunks for doc_id {scope!r} in the {self.doc_type} index")
        return self.retriever.search(query, k=k, chunk_filter=lambda c: c.doc_id == scope)
