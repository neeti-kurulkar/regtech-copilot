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

---

## Stage 2 (2026-09-29)

### `aip/rag.py`: Lab 4 Part B in the reference pipeline
- `validate_answer(text, n_sources, finish_reason=None)`: the Lab 4 B2 checks as code (non-empty,
  not truncated, every `[n]` in range, and a non-refusal carries at least one citation).
- `is_refusal(text)`: the whole answer is the refusal. A partial answer that declines one part
  is not a refusal.
- `RagPipeline(..., max_repairs=0, fail_closed=False, context_label=None, max_tokens=600)`:
  - `max_repairs`: on a validation problem, regenerate with a corrective turn that shows the
    model its rejected answer and the reasons.
  - `fail_closed`: if still invalid after the repairs, return `REFUSAL`, never a flawed answer
    (the Lab 4 B3 choice).
  - `context_label`: per-source header passed to `format_context`.
  - Defaults reproduce the original behaviour exactly.
- `RagPipeline.answer_from_hits(question, hits)`: generate, validate and repair over supplied
  passages. `answer()` now calls it after retrieval. Needed for Lab 4 E2 (gold context vs
  retrieved context).
- `RagAnswer` gains `problems`, `repairs`, `fallback` and `cited_indices`.
- Internal `_generate(...)` uses `chat(return_full=True)` so truncation (`finish_reason=length`)
  is visible to validation; `generate()` keeps its signature.
- **Truncation doubles the budget instead of being "repaired".** On `finish_reason=length`,
  `_generate` re-sends the *same* request once at `2 * max_tokens` (a `rag.truncated_retry`
  trace event); `RagAnswer.budget_doublings` counts it. This follows the convention of
  `aip.llm.structured` and `aip.evals.llm_judge`.
  **Why:** the MAIN tier is a reasoning model whose hidden thinking counts against `max_tokens`.
  In the first Stage 2 run, 4 of 31 answers were truncated, the corrective "write a shorter answer"
  turn could not help, and fail-closed turned them into false refusals (refusal precision 0.50,
  0.83 after the fix).

### `aip/retrieval.py`
- `format_context(hits, max_chars=8000, label=None)`: `label(hit)` replaces the bare doc_id in
  each numbered source header. The regulation Q&A shows
  `Responsible Business Conduct Directions, 2025 | A. Fair Practices Code > A.4 General | paras 19-21`,
  so the model can name the paragraph it relies on.

### `aip/chunking.py`
- `annotate_provenance` also records `meta["numbers"]`: every numbered paragraph a chunk touches.
  **Why:** a sliding window that starts in para 17 can hold the answer in para 19. Citing only
  the starting paragraph would point the reader to the wrong clause.

### `aip/evals.py`
- `refusal_metrics(refused, should_refuse)`: refusal recall and precision together, with the
  raw counts (Lab 4: never report one without the other; five unanswerable questions make each
  case worth 0.2).

---

## Stage 3 (2026-09-29)

### `aip/guards.py`
- `normalise_text(s)` (moved here from `aip.evals`, which now imports it so there is one
  definition): lower-case, straight quotes, en/em dashes to hyphens, table pipes and `<br>`
  removed, single spaces.
- `quote_in_source(quote, source, min_fraction=0.9)`: is a quote (near-)verbatim in its
  source? Exact after normalisation, or a leading/trailing 90% of the quote (at least 30 chars)
  to tolerate an ellipsis or a trimmed clause, never a paraphrase.
  **Why:** `enforce_citations` proves a citation *number* exists; this proves the *words*
  attributed to a source are in it. `check_policy_gap` puts it inside the Pydantic schemas it
  hands to `aip.llm.structured`, so a fabricated or paraphrased quote is fed back and repaired
  by aip's own repair loop, like any other validation error.

Stage 1 retrieval numbers are unchanged by the shared normalisation (re-run: identical winners
and metrics).

### `aip/cost.py`: budgets are context-local (bug fix)
- The active-budget stack was one process-wide list. Two budgets entered in parallel threads
  each recorded the *other's* calls. Found in Stage 3: four concurrent `check_policy_gap` runs,
  each with a $0.25 budget, each "spent" the sum of all four (about 110 calls instead of about 12)
  and raised `BudgetExceeded`; 6 of 16 evaluation cases crashed, and the reported cost per check
  was inflated about 2x.
- Now a `contextvars.ContextVar`. A budget counts calls made in its own context only.
- `map_in_context(pool, fn, items)`: `pool.map` with each task in a copy of the caller's
  context, so a budget entered around the submission still counts the pool's work, while a budget
  entered *inside* a task stays private to that task.

### `aip/evals.py`
- `run_eval(workers>1)` submits cases with `cost.map_in_context`, so its budget keeps counting
  the worker threads' calls under context-local budgets. Reported costs are unchanged.

---

## Stage 4 (2026-09-29)

No changes. `find_enforcement_precedent` is built entirely on existing aip pieces: `llm.structured`
(the case table and the relevance verdicts, both with grounding checks inside the schema),
`guards.quote_in_source` / `delimit_untrusted`, `retrieval` via the Stage 1 index,
`evals.run_eval` / `refusal_metrics`, and `cost.Budget`.

---

## Stage 5 (2026-09-29)

### `aip/guards.py`: grounding for extracted figures and dates
- `number_forms(n)` gives the ways a number appears in prose ('21', 'twenty-one', 'one hundred and eighty').
- `number_in_text(n, text)` checks that text states the number, as digits or words, respecting
  digit boundaries (4 is not "found" inside 14).
- `date_in_text(d, text)` checks that text states the date ('March 31, 2026', '31 March 2026',
  '31.03.2026', ISO).
- **Why:** `quote_in_source` proves the quoted words exist. These prove the *structured fields*
  extracted from them are backed by those words: a model that quotes "within 14 days" but puts
  `within_days=15` is caught and repaired by `aip.llm.structured`'s loop. It generalises the
  "a 'met' needs a stated figure" guard from Stage 3. Used by `regtech/deadlines.py`.

Stage 5 also fixed a bug in `number_forms` found by its own test: 180 rendered as
"one hundred and 80" (the shortest form of the remainder was its digits); remainders now use words.
