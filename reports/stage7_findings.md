# Stage 7 findings: the service layer (Lab 7)

`regtech/service.py` (FastAPI), `regtech/ui.py` (Streamlit), `regtech/dashboard.py` (ops, from traces),
`regtech/gate.py` + `ci/thresholds.yml` + `.github/workflows/eval.yml` (regression gate), `README.md`.

## Targets

| Lab 7 requirement | Target | Result |
|---|---|---|
| `POST /ask` grounded answer with citations, cost and trace id | working | working: two modes, `qa` and `agent` |
| `GET /health`, `GET /metrics` | working | working: `/metrics` has cost today and per query, p50/p95/p99 by mode, cache hit rate, refusal rate, errors by status, tool calls, alerts |
| Errors | 422 / 429 / 503 + Retry-After / 500 | all four mapped; unit-tested with fake pipelines; no stack trace ever returned |
| p95 latency, cached | ≤ 800 ms | **0.1 ms** (exact response cache) |
| p95 latency, uncached (Q&A) | ≤ 6,000 ms | **2,107 ms** (p50 1,742; n = 12, serial, all caches off) |
| Streaming time to first token | ≤ 1,500 ms | **p50 1,518 ms, p95 2,341 ms: not met** (see below) |
| Cost per query (Q&A) | ≤ $0.01 | **$0.0007** (cold); agent $0.004–0.019 |
| Semantic cache with its breaking threshold | reported | **no safe threshold exists on this domain; shipped off** |
| Regression gate, seen failing | fails the build on a breach | 17 gated metrics; replays offline in 27 s; **fails on a planted bug** (`reports/stage7_gate_break_demo.txt`) |

The agent is a different product: 5–16 s and up to $0.019 per request, because a policy check is about a
dozen model calls. It is measured and reported against its own 40 s p95 SLO, not the Q&A's 6 s.

## Decisions (agreed before building)

1. **Two modes.** `qa` is Stage 2's grounded Q&A; `agent` is Stage 6's agent with its default layers.
2. **Q&A on the SMALL tier.** On Stage 2's 31 questions (cold, two SMALL runs against one MAIN run):

   | | MAIN | SMALL run 1 | SMALL run 2 | Target |
   |---|---|---|---|---|
   | Correctness (judge) | 0.917 | 0.875 | 0.854 | ≥ 0.75 |
   | Faithfulness | 1.00 | 0.968 | 0.968 | ≥ 0.90 |
   | Citation validity | 1.00 | 1.00 | 1.00 | 1.00 |
   | Refusal recall / precision | 1.00 / 0.83 | 1.00 / 0.71 | 1.00 / 0.71 | ≥ 0.80 / ≥ 0.70 |
   | p95 latency | **7,986 ms** | 1,937 ms | 1,947 ms | ≤ 6,000 ms |
   | Cost per query | $0.0044 | $0.0007 | $0.0007 | ≤ $0.01 |

   SMALL meets every target, and MAIN misses latency. The price is 4–6 points of correctness and one more
   over-refusal (the safe direction). The gate's own recording scored SMALL at 0.917, so
   run-to-run noise is of the same size. The CLI `ask` stays on MAIN.
3. **Streaming: "stream, then verdict".** The draft streams; at the end a `verdict` event confirms the
   citations or carries the repaired / fail-closed replacement with `retracted: true`. The UI shows
   "checking citations…" until then. Stage 2 needed a repair on 0 of 31 questions, so retractions should be
   rare; one was simulated in the unit tests.
4. **Uploads.** `POST /upload` accepts only PDFs (magic bytes checked, 10 MB cap) into `uploads/` under a
   random id. The agent's gap tool may read only PDFs inside that folder, which closes the "check the policy
   at `.env`" hole (tested; the live agent was refused and said so).

## Caching

**Exact cache** (`aip.response_cache.ExactCache`): keyed by the normalised question plus a scope. The scope
is the mode and a corpus version (a hash of the manifest, calendar and case table), plus the date and the
upload for the agent, so a cached answer never outlives its corpus or its calendar date. Runs that hit a
limit are never cached. A repeat costs $0 and 0.1 ms.

**Semantic cache: the interesting number** (`reports/stage7_semantic_threshold.md`). We tested 40 labelled
pairs: 20 paraphrases, and 20 near-misses that differ in the one word that changes the answer.

| Threshold | Paraphrases served from cache | Near-misses served the **wrong** answer |
|---|---|---|
| 0.90 | 50% | 30% |
| 0.95 (a common default) | 35% | **15%** |
| 0.97 | 25% | 5% |
| 0.988 (first with no wrong hit) | **0%** | 0% |

The most similar pair of all 40 is a near-miss: the KFS validity period for loans of "seven days or more"
vs "less than seven days" (0.9825), whose answers are 3 working days vs 1. "Penal *interest*" (forbidden) vs
"penal *charges*" (allowed) scores 0.970, above most true paraphrases. In regulatory text the answer turns
on exactly the words an embedding treats as noise. **There is no threshold at which the semantic cache
helps without answering the wrong question, so it ships off** (`REGTECH_SEMANTIC_THRESHOLD=off`); the code
is there and unit-tested.

## Streaming

TTFT p50 1,518 ms and total p50 1,518 ms: on the SMALL model the first chunk *is* nearly the whole answer.
Gemini Flash-Lite returns a short, cited answer in one or two chunks. So streaming buys almost nothing
on this path today, and the TTFT target is missed at p95 (2.3 s). The time before the first token is the
query embedding (~0.6–0.7 s) plus the model's time to its first chunk. Streaming would matter with longer
answers or a slower model; it is kept for that, and for the verdict protocol.

## Latency budget (Q&A, uncached, from traces)

```
embed query        625 ms p50   715 ms p95
retrieve (dense)     4 ms       (local matrix product over 1,234 chunks)
rerank               0 ms       (none in this pipeline)
generate          1113 ms p50  1476 ms p95   (SMALL tier)
validate             <1 ms      (deterministic)
------------------------------------------
total             1742 ms p50  2107 ms p95
```

What to optimise first: the **query embedding**, about a third of every uncached request, for no reasoning
at all. Options, in order: a local embedding model for queries (no network hop; needs re-indexing with the
same model), or embedding the query in parallel with the exact-cache lookup. Generation is already on the
fastest tier.

## Observability

- Every request writes spans with one `trace_id`, returned in every response and error. This needed an
  aip change: spans in worker threads used to lose their parent. Now all 12 model calls of an agent
  request, including the gap tool's thread pool, sit under the request's trace (checked: 0 orphaned model
  calls).
- **"Why did request X take 9 seconds?"** was answered from traces during the build. The first request
  after start-up took 9.9 s, but its model call took 1.3 s. The difference was the lazy LiteLLM import
  (~8.5 s), now paid at start-up.
- Dashboard: latency by stage, cumulative cost, errors, cache hit rate, and a trace lookup that defaults to
  the slowest request.
- **Alert** (Lab 7 C4): the refusal rate over the last 20 answers is at least double the previous 100 (and
  at least 20%). *What to do:* check `/health` (chunk counts, corpus version); then open a recent refused
  trace and look at `retrieve.dense`: a broken or empty index shows up as irrelevant sources long before any
  quality metric moves. A second alert fires on p95 above the SLO for 5 minutes. The refusal alert is unit-tested; the latency alert is not.

## The regression gate

`python -m regtech gate` runs three golden sets, each through the system *as served*:
- the 31 Q&A questions in the service configuration;
- the 16 gap cases;
- the 23 red-team cases on the agent's default layers.

It checks 17 metrics against `ci/thresholds.yml`, set just below the recorded values (about one standard
error of headroom). Cost and latency are gated on the *recorded* values, because a replay costs $0 and takes
0 ms. CI replays `ci/cache/calls.sqlite3` (1,940 entries, 29 MB, exported by `gate --record`) with
`AIP_OFFLINE=1`: no API key, no cost, 27 s. A change that alters any model request misses the cache, and
the gate fails closed.

**Seen failing** (Lab 7 D3): a planted refactoring bug (`regtech/qa.py` passing `refused=False`) turned
`qa_refusal_recall` from 1.0 to 0.0, and the gate exited 1. Output: `reports/stage7_gate_break_demo.txt`.

**Two bugs the gate found before it worked** (both fixed in aip or regtech, both now tested):
1. *Concurrent identical model requests broke replay.* The poisoned policies differ by one page, so
   parallel red-team cases sent identical assessment prompts at the same moment. Each got its own
   non-deterministic answer, and the cache kept the last. Fixed with single-flight in `aip.llm.raw_call`,
   which also saves the duplicate calls.
2. *Concurrent identical embeddings.* After sanitising, three poisoned uploads become the same document and
   were embedded in parallel. The gate now runs the red-team sequentially. PyMuPDF conversions are also
   serialised, as a documented precaution: a parallel test found no divergence there.

## Honest limits

- Latency was measured in-process (TestClient), serially, on one laptop over a home connection: n = 12
  Q&A and n = 4 agent requests. There was no load test: under concurrency the provider's rate limit, not
  the code, will break first.
- The CI gate proves *behaviour on the recorded cache*. It cannot see provider drift (the same prompt
  answered differently next month). Re-recording on a schedule would.
- The recorded-latency gate counts model time only. Retrieval is a few ms of local compute, but network
  jitter is not modelled.
- Uploads are stored unencrypted on local disk with no expiry and no per-user access control.
