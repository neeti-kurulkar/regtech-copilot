"""Stage 1 retrieval evaluation: our query sets and indices plugged into aip.evals.

aip provides the harness (Case, run_eval, EvalReport, compare) and the metrics
(evidence_retrieval_metrics -> retrieval_metrics). This module only says what
a "case" is for each corpus and how to build the index under test.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from aip.cost import Budget
from aip.evals import Case, EvalReport, evidence_retrieval_metrics, missing_evidence, run_eval

from regtech.index import CorpusIndex, IndexConfig, load_documents
from regtech.paths import eval_file

KS = (1, 3, 5, 10)
K = 10
HEADLINE = ("ndcg@10", "recall@5", "hit_rate@1", "mrr")


def load_queries(doc_type: str) -> list[Case]:
    """The golden set as aip Cases, after checking every evidence phrase exists verbatim."""
    path = eval_file(f"retrieval_{doc_type}.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    docs = {row.doc_id: text for row, text in load_documents(doc_type)}
    problems = missing_evidence({r["id"]: r["relevant"] for r in rows}, docs)
    problems += [f"{r['id']}: unknown scope {r['scope']}" for r in rows if r.get("scope") and r["scope"] not in docs]
    if problems:
        raise ValueError("query set has problems:\n  " + "\n  ".join(problems))
    doc_level = lambda r: all("evidence" not in x for x in r["relevant"])  # noqa: E731
    return [
        Case(id=r["id"],
             input={"query": r["query"], "scope": r.get("scope"),
                    # doc-level labels are de-duplicated to documents, so fetch extra chunks
                    "k": K * 3 if doc_level(r) else K},
             expected=r["relevant"],
             meta={"kind": r["kind"]})
        for r in rows
    ]


def _metric(output: list[dict], expected: list[dict]) -> dict[str, float]:
    return evidence_retrieval_metrics([(h["doc_id"], h["text"]) for h in output], expected, ks=KS, k=K)


@dataclass
class Run:
    corpus: str
    config: IndexConfig
    n_chunks: int
    build: dict
    report: EvalReport

    @property
    def metrics(self) -> dict[str, float]:
        return self.report.aggregate()


def evaluate(doc_type: str, config: IndexConfig, cases: list[Case]) -> Run:
    with Budget(limit_usd=1.0, label=f"build {doc_type} {config.label}") as b:
        index = CorpusIndex(doc_type, config)

    def system(inp: dict) -> list[dict]:
        hits = index.search(inp["query"], k=inp["k"], scope=inp["scope"])
        return [{"doc_id": h.doc_id, "chunk_id": h.chunk.chunk_id, "score": round(h.score, 4),
                 "heading": h.chunk.meta.get("heading", ""), "text": h.text} for h in hits]

    report = run_eval(f"{doc_type}:{config.label}", cases, system, _metric, budget_usd=0.25, progress=False)
    if report.n_errors:
        raise RuntimeError(f"{report.name}: {report.n_errors} case(s) errored, e.g. "
                           f"{next(r.error for r in report.results if r.error)}")
    return Run(doc_type, config, len(index.chunks), b.as_dict(), report)
