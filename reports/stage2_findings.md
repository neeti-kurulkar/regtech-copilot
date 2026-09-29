# Stage 2 - Findings

Companion to the generated `stage2_qa.md`. The first run is kept as `stage2_qa_run1_max900.md`
as the "before" evidence for the fix described below (Lab 5's before/after pattern).

## Result against the Lab 4 targets

| Metric | Target | Result |
|---|---|---|
| Citation validity | 1.00 | **1.000** (enforced in code, not by the prompt) |
| Faithfulness (judge) | ≥ 0.90 | **0.968**, κ pending your labels |
| Correctness, answerable (judge) | ≥ 0.75 | **0.917**, κ pending your labels |
| Refusal recall | ≥ 4/5 | **5/5** |
| Refusal precision | ≥ 0.70 | **0.83** (5 of 6 refusals were right) |
| Cost per query | ≤ $0.01 | **$0.0045** |
| p95 latency | ≤ 6,000 ms | **6,672 ms**, borderline (p50 4,664 ms; n = 8) |

The judge numbers are not claims yet. Lab 4 forbids reporting a judge score without Cohen's κ
against at least 20 human labels, so `reports/stage2_judge_calibration.csv` holds 20 answers to
hand-label. Then run `python -m regtech judge-kappa`.

## 1. The biggest bug was a budget, not a prompt

The first run looked like a refusal problem: 5 answerable questions refused, precision 0.50. Every
failed answer's validation problems were recorded, and 4 of the 5 "refusals" were not the model
refusing at all. They were answers **cut off at the token limit** (`finish_reason = length`), which
the repair step could not fix and fail-closed then turned into a refusal.

The generator (Gemini 3.7 flash) is a reasoning model: its invisible thinking tokens count against
`max_tokens`, so 900 tokens was not 900 tokens of answer. This is the same trap as the Lab 4 judge
war story, in the generator this time.

**Fix, in aip:** truncation is not a content problem, so `aip.rag` now retries the *same request*
at double the token budget instead of "repairing" it. That is the convention `aip.llm.structured`
and `aip.evals.llm_judge` already followed. The Q&A budget also went up to 2,048 (tokens are billed
only as used).

| | Before (900 tokens) | After |
|---|---:|---:|
| Correctness | 0.792 | **0.917** |
| Refusal precision | 0.50 (5/10) | **0.83 (5/6)** |
| Answers that fell back to refusal | 13% | **0%** |
| Repairs needed | 23% | **0%** |

## 2. Where the remaining errors come from (Lab 4 E2)

| | Correctness |
|---|---:|
| A: gold passages given directly (generation ceiling) | 0.958 |
| B: retrieved passages (the real system) | 0.917 |
| Loss from retrieval (A - B) | 0.042 |
| Loss from generation (1 - A) | 0.042 |

Both losses are small, and they are split evenly. The two failures are both retrieval:
- **A02** "CKYCR upload deadline": the same exact-identifier weakness Stage 1 measured. The answer
  is about CKYCR but not the 10-day deadline.
- **A05** "How often must risk categorisation be reviewed": the passage missed the top 6, and the
  model correctly refused rather than guessing.

The first gold-context measurement came out *higher* for retrieval than for gold (0.917 vs 0.833),
which is impossible for a true ceiling. It exposed two flaws in the gold context itself: one chunk
cut tables and clauses in half, and a partial phrase match picked similar wording elsewhere in the
KYC Directions. Gold now prefers exact matches and includes the neighbouring chunks.

## 3. The refusal dial (Lab 4 C4)

| Prompt | Refused answerable | Refused partial | Refused unanswerable | Precision |
|---|---:|---:|---:|---:|
| balanced | 1/24 | 0/2 | 5/5 | 0.83 |
| strict | 2/24 | 2/2 | 5/5 | 0.56 |

The balanced prompt answers the supported half of a partial question and names the missing half
(e.g. "the CCO's minimum tenure is three years, para 21; the sources do not specify a minimum
salary"). Strict refuses the whole question.

**Product recommendation: balanced.** For a compliance analyst, an over-refusal costs time, but a
partial answer that clearly marks what is *not* covered is more useful than silence. The costly
error, a confident wrong answer, stays at 0 in both variants: all 5 unanswerable questions were
refused under both, including the two traps. The capital-adequacy trap was not answered with
the microfinance-only 15% figure.

## 4. Latency is borderline, and measuring it needed care

- The parallel answer phase inflates latency through queueing, and a cached re-run reports about
  0 ms. Neither is what a user waits for. Latency now comes from a separate probe: warm system,
  one request at a time, cache off.
- The probe's warm-up was first a *cached* question, so the first timed request paid the cold
  start (22 s). The warm-up now makes a real call.
- Warm p50 is about 4.7 s and p95 about 5.3-6.7 s across runs. The reasoning model's thinking is
  most of it. Levers, if needed: a faster tier compared Lab 2-style, or streaming in Stage 7.

## 5. A judge disagreement worth calibrating

A16 (CCO tenure) was judged fully correct but *unfaithful*, with every number supported. The
answer expanded "ACB" to "Audit Committee of the Board", which the cited passage does not spell
out. Two metrics disagreeing about the same answer is exactly the signal Lab 4 says to investigate.
It is either a real (minor) faithfulness slip or an over-strict judge, and the human labels in the
calibration sheet will settle which.

## What aip gained in this stage

See `aip/CHANGELOG_REGTECH.md` (Stage 2): validate, repair and fail-closed, plus truncation
doubling, in `RagPipeline`; `answer_from_hits` for gold-context runs; labelled sources in
`format_context`; the full paragraph range in `annotate_provenance`; and `refusal_metrics`.
