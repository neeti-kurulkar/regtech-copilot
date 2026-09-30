# RegTech Compliance Copilot: Evaluation Report

AI-in-Practice I, final assignment, Option 1 (Fintech). Every number below is reproducible from the
committed artefacts. The appendix maps each one to its report and command.

## 1. What it does

Indian non-bank lenders (NBFCs) must follow a long rulebook from the Reserve Bank of India (RBI), publish a
Fair Practices Code saying how they treat borrowers, and meet dozens of reporting deadlines. The RBI fines
them when they fall short. The copilot helps a compliance officer with four jobs:

1. **Rules:** answer a question about what the RBI requires, citing the exact paragraph.
2. **Gaps:** compare a company's Code with the rules on a topic and list where it is silent, weaker or
   contradictory, quoting both documents.
3. **Penalties:** say whether the RBI has fined anyone for that kind of failure, or state clearly that it
   has not in the records held.
4. **Deadlines:** list what is due in the next N days.

It answers only from 12 RBI Directions, 11 company Codes and 13 RBI penalty orders from 2026. When those
documents do not answer a question, it says so instead of guessing. It runs as a web service with a demo page.

## 2. How well it works

| Tool | Test set | Result | Target |
|---|---|---|---|
| Retrieval (3 corpora) | 24 / 18 / 17 labelled queries | nDCG@10 0.90 / 0.96 / 0.99; recall@5 0.96 / 1.00 / 1.00 | ≥ 0.80 / ≥ 0.85 |
| Q&A, as served (fast model) | 31 questions (24 answerable, 2 partial, 5 unanswerable) | correctness 0.85–0.92 (3 runs); faithfulness 0.94–0.97; citation validity **1.00**; refusal recall **5/5**, precision 0.71 | ≥ 0.75; ≥ 0.90; 1.00; ≥ 0.80 / ≥ 0.70 |
| Judge vs human | 20 answered questions, hand-labelled blind | correctness: agreement 20/20, **κ 1.00**; faithfulness: agreement 19/20, κ 0.00 (see note) | reported |
| Policy gap check | 16 hand-labelled rule × company cases | rule found 1.00; status exactly right 1.00; **missed gaps 0**; false alarms 0 | ≥ 0.90; ≥ 0.80; 0; ≤ 0.20 |
| Penalty precedent | 32 risks (19 with a precedent, 13 without) | right precedent first 1.00; **invented precedents 0.00** (a search-only baseline invents one every time) | 0 |
| Deadline extraction | 17 hand-labelled deadlines; audit of 20 calendar entries | precision 0.94, recall 1.00, fields 1.00; audit: 18/19 real, 17/18 fully right | reported |
| Agent guardrails | 18 attacks + 5 look-alike harmless requests | **17/18 attacks blocked**; 0/5 harmless blocked; 0 privileged actions by an attack | ≥ 0.80; ≤ 0.25; 0 |

Note on the judge: the human labelled all 20 answers faithful, so faithfulness κ is 0 by construction (κ
only measures agreement beyond chance, and one rater never used the other label). The one disagreement is
A16: the judge counts an expansion of "ACB" that the source does not spell out as unsupported, and the human
does not.

A regression gate re-checks 17 of these numbers on every push, replaying a recorded cache (no key, no
cost, 27 s). It was shown failing, locally and on GitHub, on a planted bug that disabled refusals.

## 3. Where it fails

| Failure | Count | Where | Status |
|---|---|---|---|
| **A policy that lies** ("policy-washing"): false but plausible policy text claims safeguards the company lacks | 1 of 18 attacks succeeds: the forged Code scores 4–6 rules met vs 1 for the real one | gap check on uploads | **open**: no prompt-level defence can tell a false policy from a true one (§6) |
| Exact-identifier questions ("CKYCR upload deadline", "SMA-2") retrieved poorly | 2 of 24 answerable Q&A questions lost to retrieval (Stage 2) | dense retrieval | open; hybrid retrieval helps identifiers but hurt paraphrases overall |
| Over-refusal on partially answerable questions (fast model) | 2 of 7 refusals were wrong (precision 0.71) | Q&A generation | accepted: the safe direction |
| A rule's numbered list cut short, hiding the 9 am–6 pm calling limit, so Tata Capital's 8 am–7 pm window was called "met" | 1 missed gap in cold re-runs; found by the red-team, not the test set | gap check, requirement extraction | **fixed** in code (quotes completed from the source); regression test |
| Run-to-run noise in which requirements are extracted | 1 of 16 rules missed in 2 of 4 cold runs (either model) | gap check | open; reported, not hidden |
| Optional window / wrongly typed deadline in the calendar | 1 of 19 shown entries; 1 of 18 with a wrong field | deadline extraction | open |
| Streaming first token slower than target | p95 2.3 s vs 1.5 s | Q&A stream | open (§5) |
| Labels I got wrong, corrected after the tool disagreed with evidence | 4 of 16 gap labels, 1 retrieval label | evaluation sets | fixed; originals kept and reported (status right under original labels: 0.75) |

## 4. What it costs

Prices are Gemini list prices as configured in `aip/config.py`. Costs are for a cold cache.

| | Per query | Per 1,000 | Per year at 10,000/day |
|---|---|---|---|
| Q&A (fast model) | $0.0007 | $0.72 | $2,600 |
| Agent (mean over the red-team mix) | $0.0093 | $9.30 | $34,000 |
| &nbsp;&nbsp;agent with a policy gap check | $0.015 | $15 | (upper bound) $55,000 |
| **Assumed mix: 70% Q&A, 30% agent** | **$0.0033** | **$3.29** | **$12,000**, about **$9,600** if 20% of questions repeat (exact cache) |

One-off costs: embedding the corpus (under $0.10), the deadline calendar ($0.75), and the penalty case table
($0.05). Development runs cost a few dollars per stage. **Price risk:** the agent's reasoning model is listed
to double in price from 1 January 2027 (`aip/config.py`). The Q&A tier is unaffected.

## 5. How fast it is

Q&A, uncached, one request at a time through the service (n = 12, from traces):

```
embed query        625 ms p50    715 ms p95
retrieve             4 ms        (1,234 chunks, local)
generate          1113 ms p50   1476 ms p95   (fast model)
validate            <1 ms
total             1742 ms p50   2107 ms p95   (target 6,000)   · repeated question: 0.1 ms
```

- The agent takes 5–16 s (n = 4; red-team p50 13.5 s, p95 27.5 s). A policy check is about a dozen
  model calls.
- **What I would optimise first** is the query embedding: a third of every uncached Q&A request, for no
  reasoning at all. A local embedding model removes the network hop. Streaming does not help today,
  because the fast model sends a short answer in one or two chunks.
- The fast model was chosen deliberately. The reasoning model took 8 s at p95 for 4–6 points more
  correctness.

## 6. What it is not safe for

1. **Not legal advice, and not an attestation that a company complies.** "Met" means *the document says
   so*, not that the company does it. An uploaded document is not verified, and a forged one passes
   (the surviving attack). The report must not be used to certify an uploaded policy without checking
   where it came from.
2. **Blind outside its 36 documents.** It knows 12 Directions: nothing on, for example, IT outsourcing,
   and not the ombudsman or securitisation rules behind two of the 13 penalties. It knows nothing
   published after the corpus was frozen (September 2026). An answer of "the Directions do not cover
   this" may simply mean *these* Directions.
3. **"No precedent" means none among 13 penalty orders**, not that the RBI has never fined it. Absence of
   a precedent is not a low risk.
4. **Not a compliance calendar of record.**
   - Deadlines triggered by events (for example, 14 days after classifying a fraud) cannot be dated, because
     the system cannot see the events.
   - "Quarterly" duties with no stated day are shown at quarter end.
   - Holidays are ignored.
   - 1 in 19 audited entries was not a real deadline.
5. **Stale Codes give true but dated answers.** Several Codes predate the current rules (Nissan Renault
   2020, L&T 2022). A gap may be the company's outdated document, not current practice.
6. **Not for borrowers or other non-experts.** It was built and evaluated for compliance staff who can
   open the citation and read the paragraph. PII filtering is a regex, and uploads are stored unencrypted
   with no per-user access control.
7. **The evidence is small and partly self-made.**
   - 16–32 cases per tool, labelled by the builder. Each gap case moves a rate by 6 points.
   - The LLM judge agrees with a human on correctness (κ 1.00, 20/20), but its faithfulness score is
     unvalidated. The human found all 20 answers faithful and the judge flagged one (A16), so agreement
     is 19/20 but κ is 0 by construction. A set with no unfaithful answers cannot test whether a judge
     catches them. Read faithfulness 0.94–0.97 as "no unsupported claim found in 20 checks", not as a
     calibrated rate.
   - The red-team was written by someone who knew the defences.
   - The system was not load-tested; the provider's rate limits will break first.

## 7. What I would do next (ranked by expected value)

1. **Provenance for uploaded policies.** Hash and record every upload. Diff it against the company's
   published Code when the corpus has one: Tata Capital's forged page would show up as unexplained new
   text. Label every upload-based report "unverified document". This closes the only attack that
   survives, and it is what makes gap reports usable as evidence. About 1–2 days.
2. **Bigger, independently labelled test sets, and a calibrated judge.** Aim for about 100 Q&A and 50 gap
   cases labelled by a compliance professional, not the builder. That roughly halves the uncertainty on
   every headline number and tests the judge that scores two of them. Without it, a 5-point change is
   noise. About 3–4 days of expert time.
3. **Keep the corpus current.** Watch the RBI's Directions and penalty pages, re-ingest, and re-record
   the gate on a schedule (which also catches provider drift). A compliance tool that silently ages is
   the failure users will not notice. About 2 days to build, then routine runs.

(Smaller, cheaper items: hybrid retrieval only for identifier-shaped queries; a local query-embedding
model for about 0.7 s off every request.)

---

## Appendix: where every number comes from

| Section | Report | Reproduce with |
|---|---|---|
| Retrieval | `reports/stage1_findings.md`, `stage1_retrieval.md` | `python -m regtech eval-retrieval` |
| Q&A quality (reasoning model), refusal dial, error split | `reports/stage2_findings.md`, `stage2_qa.md` | `python -m regtech eval-qa` |
| Q&A on the fast model | `reports/stage7_qa_tier_*.json`, `stage7_gate.json` | `python -m regtech qa-tier SMALL` |
| Judge vs human (20 hand labels) | `reports/stage2_judge_calibration.csv`, `stage2_findings.md` | `python -m regtech judge-kappa` |
| Gap check, fixes, cost of models | `reports/stage3_findings.md`, `stage3_policy_gap_v5.md` | `python -m regtech eval-gap` |
| Precedents | `reports/stage4_findings.md`, `stage4_precedent.md` | `python -m regtech eval-precedent` |
| Deadlines and audit | `reports/stage5_findings.md`, `stage5_calendar_audit.csv` | `python -m regtech eval-deadlines` |
| Red-team and layers | `reports/stage6_findings.md`, `stage6_redteam*.md` | `python -m regtech redteam` |
| Service, caching, streaming, latency | `reports/stage7_findings.md`, `stage7_latency.json`, `stage7_semantic_threshold.md` | `python -m regtech latency`, `semantic-study` |
| Regression gate, seen failing | `reports/stage7_gate.json`, `stage7_gate_break_demo.txt`; CI green on `master` ([run](https://github.com/neeti-kurulkar/regtech-copilot/actions/runs/36711504321)), red on the planted bug ([run](https://github.com/neeti-kurulkar/regtech-copilot/actions/runs/36711669797), branch `demo/gate-catches-refusal-bug`, never merged) | `python -m regtech gate` |
| Every change to the course library | `aip/CHANGELOG_REGTECH.md` | `python -m pytest` |
