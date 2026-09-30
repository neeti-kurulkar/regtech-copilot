"""The evaluation harness.

This is the most important file in the package.

A GenAI system is a stochastic function. You cannot reason about whether a
change helped by looking at three examples, and you certainly cannot reason
about it by reading the diff. The only way to make progress is to build a
harness first and let it arbitrate.

The shape of every evaluation in this module is the same:

    cases  : a golden set — inputs with known-good outputs
    system : a callable, input -> output           (the thing under test)
    metric : (output, expected) -> dict[str, float]
    report : per-case rows + aggregate + cost + latency, saved to disk

Three families of metric live here:

    1. Deterministic  — exact match, field accuracy, JSON validity.
                        Free, fast, and unarguable. Use these wherever you can.
    2. Retrieval      — recall@k, MRR, nDCG@k, hit rate.
                        Also free. This is why we label retrieval separately
                        from generation: it lets you localise the failure.
    3. LLM-as-judge   — for open-ended text where 1 and 2 cannot reach.
                        Expensive, biased, and must be calibrated against
                        human labels before you are allowed to trust it.
"""
from __future__ import annotations

import json
import re
import statistics
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aip import cost, tracing


# ==========================================================================
# 1. Deterministic metrics
# ==========================================================================
def exact_match(pred: Any, gold: Any) -> dict[str, float]:
    return {"exact_match": float(str(pred).strip().lower() == str(gold).strip().lower())}


def field_accuracy(pred: dict | Any, gold: dict, fields: Sequence[str] | None = None
                   ) -> dict[str, float]:
    """Per-field correctness for structured extraction.

    Reported two ways, and the difference matters:

      field_accuracy  — fraction of individual fields correct. Forgiving; good
                        for tracking incremental progress.
      record_accuracy — fraction of records with *every* field correct. This is
                        the number the business cares about, because a record
                        with one wrong field still needs a human to look at it.
    """
    if hasattr(pred, "model_dump"):
        pred = pred.model_dump()
    pred = pred if isinstance(pred, dict) else {}
    keys = list(fields) if fields else list(gold.keys())
    per = {}
    for k in keys:
        p, g = pred.get(k), gold.get(k)
        if isinstance(g, str) and isinstance(p, str):
            ok = p.strip().lower() == g.strip().lower()
        elif isinstance(g, (list, tuple)) and isinstance(p, (list, tuple)):
            ok = sorted(map(str, p)) == sorted(map(str, g))
        else:
            ok = p == g
        per[f"field.{k}"] = float(ok)
    correct = sum(per.values())
    return {
        **per,
        "field_accuracy": correct / len(keys) if keys else 0.0,
        "record_accuracy": float(correct == len(keys)),
    }


def json_valid(pred: Any, gold: Any = None) -> dict[str, float]:
    return {"json_valid": float(pred is not None and not isinstance(pred, Exception))}


# ==========================================================================
# 2. Retrieval metrics
# ==========================================================================
def retrieval_metrics(retrieved: Sequence[str], relevant: Sequence[str],
                      ks: Sequence[int] = (1, 3, 5, 10)) -> dict[str, float]:
    """Standard IR metrics for one query.

    retrieved : ranked list of ids (most relevant first)
    relevant  : set of ids that are actually relevant (unranked, binary)

    hit_rate@k  — did at least one relevant doc appear in the top k?
                  The metric that matters for RAG: the generator only needs
                  one good passage.
    recall@k    — what fraction of all relevant docs appeared in the top k?
    precision@k — what fraction of the top k are relevant? (context pollution)
    mrr         — 1 / rank of the first relevant doc. Rewards putting the
                  right answer first, which matters when k is small.
    ndcg@k      — rank-discounted gain. The general-purpose comparison metric.
    """
    rel = set(relevant)
    ranked = list(retrieved)
    out: dict[str, float] = {}

    for k in ks:
        topk = ranked[:k]
        n_hit = len(rel & set(topk))
        out[f"hit_rate@{k}"] = float(n_hit > 0)
        out[f"recall@{k}"] = n_hit / len(rel) if rel else 0.0
        out[f"precision@{k}"] = n_hit / k if k else 0.0
        dcg = sum(1.0 / _log2(i + 2) for i, d in enumerate(topk) if d in rel)
        idcg = sum(1.0 / _log2(i + 2) for i in range(min(len(rel), k)))
        out[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0

    out["mrr"] = next(
        (1.0 / (i + 1) for i, d in enumerate(ranked) if d in rel), 0.0
    )
    return out


def _log2(x: float) -> float:
    import math

    return math.log2(x)


# --------------------------------------------------------------------------
# [regtech] Evidence-level (passage-level) relevance
# --------------------------------------------------------------------------
# Lab 3 labels relevance per document. For clause-level citation that is too
# coarse: "the KYC Directions" is relevant to every KYC question. Instead a
# label names the passage by a short verbatim evidence phrase, which scores
# every chunking strategy against the same ground truth.
#
#   relevant = [{"doc_id": "reg-rbc", "evidence": "within 21 days from the date"},
#               {"doc_id": "enf-iifl-2026-02-13"}]          # doc-level: no evidence
#
# A chunk satisfies a label if it is from that doc and contains the phrase, or
# at least `min_fraction` of it as a prefix/suffix (so a chunk boundary through
# the phrase is not scored as a total miss). If every label is doc-level, the
# ranking is de-duplicated to documents first, exactly as in Lab 3.

from aip.guards import normalise_text  # noqa: E402  [regtech] one definition, shared with guards


def _contains_evidence(chunk_text: str, evidence: str, min_fraction: float) -> bool:
    if evidence in chunk_text:
        return True
    cut = max(20, int(len(evidence) * min_fraction))
    return evidence[:cut] in chunk_text or evidence[-cut:] in chunk_text


def evidence_labels(ranked: Sequence[tuple[str, str]], relevant: Sequence[dict],
                    min_fraction: float = 0.6) -> tuple[list[str], list[str]]:
    """Map ranked (doc_id, text) results to (retrieved_ids, relevant_ids) for retrieval_metrics.

    Each relevant label gets an id "<doc_id>#<i>" and is credited at most once;
    positions that satisfy no label get a unique placeholder so ranks are kept.
    """
    units = [(f"{r['doc_id']}#{i}", r["doc_id"],
              normalise_text(r["evidence"]) if r.get("evidence") else None)
             for i, r in enumerate(relevant)]
    relevant_ids = [u for u, _, _ in units]
    if all(ev is None for _, _, ev in units):
        docs: list[str] = []
        for doc_id, _ in ranked:
            if doc_id not in docs:
                docs.append(doc_id)
        by_doc = {d: u for u, d, _ in units}
        return [by_doc.get(d, f"_x:{d}") for d in docs], relevant_ids

    credited: set[str] = set()
    labels: list[str] = []
    for rank, (doc_id, text) in enumerate(ranked):
        norm = normalise_text(text)
        label = f"_x{rank}"
        for u, d, ev in units:
            if u not in credited and d == doc_id and ev is not None and _contains_evidence(norm, ev, min_fraction):
                label = u
                credited.add(u)
                break
        labels.append(label)
    return labels, relevant_ids


def evidence_retrieval_metrics(ranked: Sequence[tuple[str, str]], relevant: Sequence[dict],
                               ks: Sequence[int] = (1, 3, 5, 10), k: int | None = None,
                               min_fraction: float = 0.6) -> dict[str, float]:
    """retrieval_metrics() over evidence-phrase (or doc-level) labels. `k` truncates after mapping."""
    retrieved, rel = evidence_labels(ranked, relevant, min_fraction)
    return retrieval_metrics(retrieved[:k] if k else retrieved, rel, ks)


def refusal_metrics(refused: Sequence[bool], should_refuse: Sequence[bool]) -> dict[str, float]:
    """[regtech] Refusal recall AND precision, with the raw counts behind them.

    Lab 4's rule: never report one without the other (refusing everything gives
    recall 1.0), and report counts, because on a handful of unanswerable
    questions one case moves the ratio by 0.2.
    """
    if len(refused) != len(should_refuse):
        raise ValueError("refused and should_refuse must be the same length")
    tp = sum(1 for r, s in zip(refused, should_refuse) if r and s)
    n_refused, n_should = sum(map(bool, refused)), sum(map(bool, should_refuse))
    return {
        "refusal_recall": tp / n_should if n_should else float("nan"),
        "refusal_precision": tp / n_refused if n_refused else float("nan"),
        "refused_correctly": float(tp),
        "refused_total": float(n_refused),
        "should_refuse_total": float(n_should),
        "over_refusals": float(n_refused - tp),
    }


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """[regtech] 95% Wilson score interval for k successes in n trials (sensible at 0/n and n/n, unlike
    the normal approximation). Small eval sets need this printed next to every rate."""
    if n <= 0:
        return float("nan"), float("nan")
    p, z2 = k / n, z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def format_rate(k: int, n: int) -> str:
    """[regtech] A rate that cannot be read without its sample size: '17/18 (0.94; 95% CI 0.74-0.99)'."""
    if n <= 0:
        return "0/0 (not measured)"
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} ({k / n:.2f}; 95% CI {lo:.2f}-{hi:.2f})"


def missing_evidence(relevant_by_case: dict[str, Sequence[dict]], docs: dict[str, str]) -> list[str]:
    """Labels whose doc is unknown or whose evidence phrase is not verbatim in the doc.

    Run this before trusting any number: a mistyped phrase silently scores zero.
    """
    normed = {d: normalise_text(t) for d, t in docs.items()}
    problems = []
    for case_id, labels in relevant_by_case.items():
        for r in labels:
            if r["doc_id"] not in normed:
                problems.append(f"{case_id}: unknown doc_id {r['doc_id']}")
            elif r.get("evidence") and normalise_text(r["evidence"]) not in normed[r["doc_id"]]:
                problems.append(f"{case_id}: evidence not found verbatim in {r['doc_id']}: {r['evidence']!r}")
    return problems


# ==========================================================================
# 3. LLM-as-judge
# ==========================================================================
JUDGE_RUBRIC_FAITHFULNESS = """\
You are grading whether an ANSWER is fully supported by the provided CONTEXT.

Rules:
- Judge support only. Do NOT judge whether the answer is helpful, well written,
  or matches your own knowledge.
- An answer is unsupported if it states anything the context does not contain,
  even if that statement is true in the real world.
- Refusing to answer when the context is genuinely insufficient is SUPPORTED.

CONTEXT:
{context}

ANSWER:
{answer}

Reply as JSON: {{"score": 0 or 1, "unsupported_claims": [..], "reason": "one sentence"}}
"""

JUDGE_RUBRIC_CORRECTNESS = """\
Compare a CANDIDATE answer to a REFERENCE answer for the same question.

Score 2 = same substantive content as the reference (wording may differ).
Score 1 = partially correct: some correct content, but omits something the
          reference states, or adds something the reference contradicts.
Score 0 = wrong, or refuses when the reference answers.

QUESTION: {question}
REFERENCE: {reference}
CANDIDATE: {candidate}

Reply as JSON: {{"score": 0|1|2, "reason": "one sentence"}}
"""


def llm_judge(prompt: str, *, tier: str = "LARGE",
              max_tokens: int = 2048) -> dict[str, Any]:
    """One judge call. Returns the parsed JSON verdict.

    Uses a *different and stronger* tier than the system under test by
    default. Self-preference bias is real and well documented: a model rates
    its own output higher than a neutral judge does (Zheng et al. 2023). Never
    judge a model with itself when you can avoid it, and when you cannot, say
    so in your report.
    """
    from aip.llm import chat, extract_json

    # max_tokens must be generous. Reasoning-capable judges spend most of their
    # output budget on invisible thinking tokens, and a 512-token cap truncates
    # the JSON *after* the thinking -- which surfaces as an unparseable verdict
    # scored 0, i.e. a silent, systematic downward bias on your headline metric.
    # This is T1 failure mode #4, and it cost this module a faithfulness score
    # of 0.667 that was really 0.911.
    res = chat(prompt, tier=tier, temperature=0.0, max_tokens=max_tokens,
               return_full=True)
    out, finish = res["text"], res.get("finish_reason")

    if finish == "length":
        res = chat(prompt, tier=tier, temperature=0.0, max_tokens=max_tokens * 2,
                   return_full=True)
        out, finish = res["text"], res.get("finish_reason")

    try:
        verdict = extract_json(out)
    except ValueError:
        return {"score": 0, "parse_error": True, "truncated": finish == "length",
                "reason": f"unparseable judge output "
                          f"({'truncated' if finish == 'length' else 'malformed'}): "
                          f"{out[:200]}"}

    # Judges return a bare list surprisingly often -- typically a one-element
    # list wrapping the verdict, sometimes a list of per-claim verdicts. Callers
    # do verdict.get("score"), so an unnormalised list is an AttributeError
    # deep inside an eval run that has already cost money. Normalise here.
    if isinstance(verdict, list):
        dicts = [v for v in verdict if isinstance(v, dict)]
        if not verdict:
            return {"score": 0, "parse_error": True,
                    "reason": "judge returned an empty list -- usually a truncated object"}
        if len(dicts) == 1:
            verdict = dicts[0]
        elif dicts:
            # Several verdicts: take the minimum score, which is the
            # conservative reading for a faithfulness-style rubric.
            verdict = min(dicts, key=lambda d: d.get("score", 0))
        else:
            return {"score": 0, "parse_error": True,
                    "reason": f"judge returned a non-dict list: {str(verdict)[:200]}"}
    if not isinstance(verdict, dict):
        return {"score": 0, "parse_error": True,
                "reason": f"judge returned {type(verdict).__name__}: {str(verdict)[:200]}"}
    return verdict


def judge_agreement(judge_scores: Sequence[float], human_scores: Sequence[float]
                    ) -> dict[str, float]:
    """Calibrate a judge against human labels before trusting it.

    Rule for this module: you may not report an LLM-judge number in a lab
    unless you have hand-labelled at least 20 cases and reported the agreement
    here. If Cohen's kappa is below ~0.4, your judge is measuring something
    other than what you think, and the fix is the rubric, not the model.
    """
    if len(judge_scores) != len(human_scores) or not judge_scores:
        raise ValueError("judge and human score lists must be non-empty and equal length")
    n = len(judge_scores)
    agree = sum(1 for a, b in zip(judge_scores, human_scores) if a == b) / n

    labels = sorted(set(judge_scores) | set(human_scores))
    pj = {l: sum(1 for s in judge_scores if s == l) / n for l in labels}
    ph = {l: sum(1 for s in human_scores if s == l) / n for l in labels}
    pe = sum(pj[l] * ph[l] for l in labels)
    kappa = (agree - pe) / (1 - pe) if pe < 1 else 1.0
    return {"raw_agreement": agree, "cohens_kappa": kappa, "n": float(n)}


# ==========================================================================
# The runner
# ==========================================================================
@dataclass
class Case:
    id: str
    input: Any
    expected: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseResult:
    id: str
    output: Any = None
    metrics: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0


@dataclass
class EvalReport:
    name: str
    results: list[CaseResult]
    budget: dict[str, Any]
    started_at: str = ""

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def n_errors(self) -> int:
        return sum(1 for r in self.results if r.error)

    def aggregate(self) -> dict[str, float]:
        keys: set[str] = set()
        for r in self.results:
            keys |= set(r.metrics)
        agg = {}
        for k in sorted(keys):
            vals = [r.metrics[k] for r in self.results if k in r.metrics]
            if vals:
                agg[k] = statistics.fmean(vals)
        agg["error_rate"] = self.n_errors / self.n if self.n else 0.0
        return agg

    def summary(self, top_level_only: bool = True) -> str:
        agg = self.aggregate()
        if top_level_only:
            agg = {k: v for k, v in agg.items() if not k.startswith("field.")}
        width = max((len(k) for k in agg), default=10)
        lines = [f"── {self.name} ── n={self.n}  errors={self.n_errors}"]
        lines += [f"   {k:<{width}}  {v:.4f}" for k, v in agg.items()]
        b = self.budget
        lines.append(
            f"   {'cost_usd':<{width}}  {b.get('cost_usd', 0):.4f}   "
            f"(p50 {b.get('latency_p50_ms', 0):.0f} ms / p95 {b.get('latency_p95_ms', 0):.0f} ms, "
            f"{b.get('calls', 0)} calls, {b.get('cached_calls', 0)} cached)"
        )
        return "\n".join(lines)

    def breakdown(self, cases: Sequence[Case], key: str, metric: str) -> dict[str, float]:
        """[regtech] Mean of `metric` grouped by `case.meta[key]` (e.g. MRR by query kind, Lab 3 B2)."""
        group = {c.id: str(c.meta.get(key, "?")) for c in cases}
        buckets: dict[str, list[float]] = {}
        for r in self.results:
            if metric in r.metrics:
                buckets.setdefault(group.get(r.id, "?"), []).append(r.metrics[metric])
        return {g: statistics.fmean(v) for g, v in sorted(buckets.items())}

    def latency_percentile(self, p: float) -> float:
        """[regtech] Per-case wall-clock latency percentile (the budget only times model calls)."""
        xs = sorted(r.latency_ms for r in self.results)
        if not xs:
            return 0.0
        return xs[min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1))))]

    def failures(self, metric: str, limit: int = 10) -> list[CaseResult]:
        """The cases you should actually read. Error triage starts here."""
        bad = [r for r in self.results if r.error or r.metrics.get(metric, 1.0) < 1.0]
        return sorted(bad, key=lambda r: r.metrics.get(metric, -1.0))[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "n": self.n,
            "aggregate": self.aggregate(),
            "budget": self.budget,
            "results": [
                {"id": r.id, "metrics": r.metrics, "error": r.error,
                 "latency_ms": round(r.latency_ms, 1),
                 "output": _jsonable(r.output)}
                for r in self.results
            ],
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        return p


def _jsonable(x: Any) -> Any:
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if isinstance(x, (str, int, float, bool, type(None), list, dict)):
        return x
    return str(x)


def run_eval(
    name: str,
    cases: Sequence[Case],
    system: Callable[[Any], Any],
    metric: Callable[[Any, Any], dict[str, float]],
    *,
    budget_usd: float = 1.0,
    workers: int = 4,
    progress: bool = True,
) -> EvalReport:
    """Run `system` over `cases`, score with `metric`, return a report.

    Concurrency is on by default because a 120-case eval at 1.5 s/call is two
    minutes serially and thirty seconds at 4 workers, and an eval you are
    unwilling to wait for is an eval you will stop running. Keep `workers`
    modest on free tiers or you will spend the saved time in 429 backoff.
    """
    budget = cost.Budget(limit_usd=budget_usd, label=name)
    started = time.strftime("%Y-%m-%d %H:%M:%S")

    def run_one(case: Case) -> CaseResult:
        t0 = time.perf_counter()
        try:
            with tracing.trace("eval.case", case_id=case.id, eval=name):
                out = system(case.input)
            m = metric(out, case.expected)
            return CaseResult(case.id, out, m, None, (time.perf_counter() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001 - a failing case is data, not a crash
            return CaseResult(
                case.id, None, {"error": 1.0}, f"{type(exc).__name__}: {exc}",
                (time.perf_counter() - t0) * 1000,
            )

    with budget:
        if workers <= 1:
            results = []
            for i, c in enumerate(cases, 1):
                results.append(run_one(c))
                if progress:
                    print(f"  {name}: {i}/{len(cases)}", end="\r")
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # [regtech] carry this run's budget into the worker threads (budgets are context-local)
                results = cost.map_in_context(pool, run_one, cases)
        if progress:
            print(" " * 60, end="\r")

    return EvalReport(name, results, budget.as_dict(), started)


def compare(*reports: EvalReport, metrics: Sequence[str] | None = None) -> str:
    """Side-by-side table of several runs. This is your experiment log.

    Print it, paste it into your lab report, and let it make the decision.
    """
    if not reports:
        return "(no reports)"
    aggs = [r.aggregate() for r in reports]
    keys = metrics or sorted(
        {k for a in aggs for k in a if not k.startswith("field.")}
    )
    name_w = max(len(k) for k in [*keys, "cost_usd", "latency_p95_ms"])
    col_w = max(12, max(len(r.name) for r in reports) + 2)

    header = f"{'metric':<{name_w}}" + "".join(f"{r.name:>{col_w}}" for r in reports)
    lines = [header, "-" * len(header)]
    for k in keys:
        row = f"{k:<{name_w}}"
        best = max((a.get(k, float('-inf')) for a in aggs), default=0)
        for a in aggs:
            v = a.get(k)
            cell = "—" if v is None else (f"{v:.4f}" + ("*" if v == best and len(reports) > 1 else ""))
            row += f"{cell:>{col_w}}"
        lines.append(row)
    for bk, label in (("cost_usd", "cost_usd"), ("latency_p95_ms", "latency_p95_ms")):
        row = f"{label:<{name_w}}"
        for r in reports:
            row += f"{r.budget.get(bk, 0):>{col_w}.4f}"
        lines.append(row)
    lines.append("")
    lines.append("* = best on that metric. Remember to read the cost row before celebrating.")
    return "\n".join(lines)


def load_cases(path: str | Path, input_key: str = "input",
               expected_key: str = "expected") -> list[Case]:
    """Load a JSONL golden set."""
    cases = []
    with Path(path).open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if not line.strip():
                continue
            row = json.loads(line)
            cases.append(
                Case(
                    id=str(row.get("id", i)),
                    input=row[input_key],
                    expected=row.get(expected_key),
                    meta={k: v for k, v in row.items()
                          if k not in {"id", input_key, expected_key}},
                )
            )
    return cases
