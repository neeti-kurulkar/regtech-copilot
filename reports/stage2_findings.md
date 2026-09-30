# Stage 2 - Findings

Companion to the generated `stage2_qa.md`. The first run is kept as `stage2_qa_run1_max900.md`
as the "before" evidence for the fix described below (Lab 5's before/after pattern).

## Result against the Lab 4 targets

| Metric | Target | Result |
|---|---|---|
| Citation validity | 1.00 | **1.000** (enforced in code, not by the prompt) |
| Faithfulness (judge) | ≥ 0.90 | **0.968**; vs human: agreement 19/20, κ 0.00 (by construction, see below) |
| Correctness, answerable (judge) | ≥ 0.75 | **0.917**; vs human: agreement 20/20, **κ 1.00** |
| Refusal recall | ≥ 4/5 | **5/5** |
| Refusal precision | ≥ 0.70 | **0.83** (5 of 6 refusals were right) |
| Cost per query | ≤ $0.01 | **$0.0045** |
| p95 latency | ≤ 6,000 ms | **6,672 ms**, borderline (p50 4,664 ms; n = 8) |

Lab 4 forbids reporting a judge score without Cohen's κ against at least 20 human labels. The 20 answers
in `reports/stage2_judge_calibration.csv` were hand-labelled on 2026-09-30: correctness κ 1.00; faithfulness
19/20 agreement but κ 0.00 (see "Judge calibration" at the end).

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

## Re-run after the Governance title fix (30 Sep 2026)

Correctness, faithfulness, refusal and citation validity are identical. The gold-context ceiling
rose from 0.958 to 0.979 (the Governance gold passages now carry a clean source label), so the
retrieval-attributable loss reads 0.062 and the generation loss 0.021. Latency on this run was
p50 6.9 s and p95 10.0 s, against 4.7 s and 6.7 s earlier, with no code change on the
generation path. Provider latency varies by run and by time of day, so the 6 s p95 target should
be treated as **not reliably met** with this reasoning-tier generator.

## Judge calibration (hand labels added 2026-09-30)

The 20 answered questions in `stage2_judge_calibration.csv` were labelled by hand, blind to the judge's
scores, using the judge's own rubric (`python -m regtech judge-kappa`).

| Criterion | Raw agreement | Cohen's κ | Disagreements |
|---|---|---|---|
| Correctness (0/1/2) | 20/20 | **1.00** | none |
| Faithfulness (0/1) | 19/20 | 0.00 | A16 (human 1, judge 0) |

The correctness judge can be trusted on this set. The faithfulness κ is 0 by construction: the human
labelled all 20 answers faithful, and κ measures agreement beyond chance, which is undefined when one rater
never uses the other label. It is not evidence of a bad judge, and not evidence of a good one: a set with no
unfaithful answers cannot test whether the judge catches them. (One human label, A08, was first entered as
unfaithful by mistake and corrected before this was written.) A16 settles the disagreement flagged in
section 5: the human reads the "ACB" expansion as harmless, and the judge is stricter.
