# Changes to `aip` for the RegTech Compliance Copilot

`aip/` started as an exact copy of the course's Lab 7 package. The project builds on it rather
than around it: when the project needs something *general* (not NBFC-specific), it is added to
the aip module where it belongs, respecting aip's one-way dependency direction, and logged here.
NBFC- and corpus-specific logic stays in `regtech/`.

Every change is backwards compatible: existing signatures keep their defaults, and code written
for the labs behaves exactly as before. Changed lines are marked `[regtech]` in the source.
Tests for everything below: `tests/test_aip_extensions.py`.

---

## Stage 1 (2026-09-28)

### New module: `aip/retry.py`
- `is_retryable(exc)` and `call_with_retries(fn, *, event, max_retries=None)`: exponential
  backoff with full jitter, tracing a `<event>` span on each retry.
- **Why:** `aip.llm.raw_call` retried transient provider errors but `aip.embed` did not. A single
  Gemini embedding timeout aborted the whole Stage 1 indexing sweep.
- Dependencies: `config`, `tracing` (same level as `cache`/`cost`).

### `aip/llm.py`
- `_is_retryable` now delegates to `aip.retry.is_retryable`. Same markers, same behaviour. The
  retry loop in `raw_call` is unchanged.

### `aip/embed.py`
- `_embed_uncached` wraps the LiteLLM `embedding()` call in `retry.call_with_retries(event="embed.retry")`.

### `aip/chunking.py`
- `markdown_chunks(..., prefix=True)`: new `prefix` flag. `prefix=False` drops the `[heading path]`
  line from the chunk text (still kept in `meta["heading"]`); chunk ids use `::n` instead of `::m`.
  This is Lab 3 exercise A3 (the prefix ablation) as a parameter instead of a copy-pasted variant.
- New strategies registered in `STRATEGIES`:
  - `markdown_noprefix`: `markdown_chunks(prefix=False)`, so it can be swept like any strategy.
  - `whole`: one chunk per document. A baseline for very short documents; on RBI press releases
    it measurably loses to smaller chunks because of boilerplate dilution.
- `strip_heading_prefix(text)`: removes the prefix `markdown_chunks` adds.
- `annotate_provenance(text, chunks, number_pattern=r"(?m)^(\d{1,3})\.\s")`: locates every chunk,
  of any strategy, in its source markdown and sets `meta["start"]`, `meta["heading"]` (if not
  already set) and `meta["number"]` (the numbered paragraph the chunk starts in).
  **Why:** fixed/sliding windows won the Stage 1 regulation sweep, but only markdown chunks
  carried a heading path. This keeps clause-level citation ("Chapter III > A.4, para 19")
  independent of the chunking choice.

### `aip/retrieval.py`
- `DenseRetriever.search`, `Bm25Retriever.search` and `HybridRetriever.search` take an optional
  `chunk_filter: Callable[[Chunk], bool]`. Excluded chunks are masked before ranking, so a
  filtered search still returns up to `k` results. Helper `_top_k` implements the masking.
  **Why:** metadata filtering previously existed only on `ChromaRetriever` (`where=`).
  `check_policy_gap` needs "search only this company's policy" on the exact retrievers.
  Note: a filtered BM25 keeps corpus-wide IDF statistics, which differs slightly from building
  a separate BM25 per document (Stage 1 policy BM25 nDCG@10 moved 0.883 to 0.876).

### `aip/evals.py`
- Evidence-level relevance for `retrieval_metrics`:
  - `normalise_text(s)`
  - `evidence_labels(ranked, relevant, min_fraction=0.6)` maps ranked `(doc_id, text)` results
    plus labels like `{"doc_id": ..., "evidence": "<verbatim phrase>"}` to the id lists
    `retrieval_metrics` expects. Labels without `evidence` are doc-level and use Lab 3's
    document de-duplication.
  - `evidence_retrieval_metrics(ranked, relevant, ks, k=None, min_fraction=0.6)`
  - `missing_evidence(relevant_by_case, docs)` validates that every phrase exists verbatim
    before any number is trusted.
  **Why:** Lab 3 labelled relevance per document. For clause-level citation over 12 long
  regulations that is too coarse, and chunk-id labels would tie the ground truth to one
  chunking strategy.
- `EvalReport.breakdown(cases, key, metric)`: mean metric grouped by `case.meta[key]`
  (Lab 3 B2's per-kind MRR table).
- `EvalReport.latency_percentile(p)`: per-case wall-clock latency. The budget's latency only
  times model calls, which are $0 / 0 ms on a cached re-run.
