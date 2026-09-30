# Stage 3 - Findings: check_policy_gap

Generated reports: `stage3_policy_gap_v1.md` (baseline) through `stage3_policy_gap_v4.md` (final).
Each run is kept: Lab 5's rule is to diagnose which stage failed, fix that stage, and prove the
fix with before/after numbers.

## What the tool does

`check_policy_gap(entity | policy_path, topic)` compares one company's Fair Practices Code with the
current RBI Directions on a topic, requirement by requirement:

1. **Resolve the company** deterministically. Lookalikes are refused: "Shriram Finance" is not
   Shri Ram Finance Corporation, and "Muthoot MCred" is not Muthoot Finance.
2. **Retrieve the rulebook**: regulation passages for the topic (Stage 1 index).
3. **Extract requirements** with `aip.llm.structured` (Lab 1). Each requirement carries a quote,
   and the schema checks that the quote is verbatim in its source, so a fabricated quote is
   repaired by aip's own repair loop. Requirements are computed once per topic and shared by
   every company.
4. **Retrieve the policy independently, per requirement**, scoped to that company's Code only
   (`chunk_filter`). This is the cross-corpus step that no lab had.
5. **Assess** each requirement as met / weaker / inconsistent / missing, again through a
   grounding-checked schema. "met" needs a verified policy quote, and a vague promise cannot
   "meet" a specific figure. An assessment that cannot be grounded is reported as `needs_review`,
   never guessed.

Uploaded policies (.pdf/.md/.txt) go through the Stage 1 converter and the same pipeline. Policy
text is treated as untrusted (`delimit_untrusted`), and prompt-injection signatures are flagged in
the report.

## Results (16 hand-labelled cases, 9 companies, 6 topics)

| | v1 baseline | v2 | v3 | v4 final |
|---|---:|---:|---:|---:|
| Target rule extracted | 0.70 | 0.94 | 0.88 | **1.00** |
| Status exactly right (current labels) | 0.86 of 10 | 0.80 | 1.00 of 14 | **1.00** |
| Status right, *original* labels | - | - | - | **0.75** |
| Missed gaps (said "met", policy falls short) | 0 | 1 | 0 | **0** |
| False alarms | 0 | 1 | 0 | **0** |
| Cases that crashed | 6 | 0 | 0 | 0 |
| Cost per check (uncached) | inflated | $0.035 | $0.035 | ~$0.035 |

Cost per check is about $0.035 cold. On a warm cache, shared topic requirements make repeat checks
much cheaper, and the fully cached v4 re-run cost $0.

## What each round fixed (Lab 5)

| Round | Failure found | Stage | Fix |
|---|---|---|---|
| v1 → v2 | 6/16 checks crashed with `BudgetExceeded` | infrastructure | **aip bug:** budgets were process-wide, so parallel checks counted each other's calls. Budgets are now context-local (`aip/cost.py`). |
| v1 → v2 | "no capitalisation of penal charges" merged into another requirement and quoted only the first sentence | 2 extraction | One obligation per requirement; a requirement may claim nothing its quote does not state; up to 8 requirements. |
| v2 → v3 | **Muthoot's "odd hours" judged "met"**, with an explanation inventing a 9am-6pm window | 4 assessment | Deterministic guard in the schema: if the rule sets a figure, "met" needs policy words that state one. The explanation may only restate the quote. |
| v2 → v3 | L&T: the rule's list of harsh practices reduced to its introductory sentence | 2 extraction | A quote that only introduces a list (ends in ':') is rejected; quote the listed items. |
| v3 → v4 | Same topic, different requirement lists for different companies | 2 extraction | Requirements computed once per topic and shared: consistent rulebook, one call. |
| v3 → v4 | Bajaj's capitalisation rule missed by the *evaluator* | evaluation | The rule is stated twice ("capitalisation" / "capitalization"); a rule may list every passage that states it. |

## The tool corrected four of my labels

Reading the tool's cited evidence showed the original label was wrong or too narrow in 4 of 16
cases. All four are listed in the v4 report with reasons, and accuracy under the original labels
(0.75) is reported alongside, so nothing rests on trusting the relabels.

- **Muthoot, loan transfer:** labelled "missing". The Code actually says it "will not entertain
  any request for transfer of borrowal accounts": **inconsistent**, a stronger finding.
- **Cholamandalam, document release:** labelled "missing" (its "30 days" is about complaints),
  but it does promise to "release all securities on repayment", with no 30-day deadline: "weaker".
- **L&T and Mahindra, calling rules:** the RBI text bans persistent calling *and/or* out-of-hours
  calling. L&T omits persistent calling; Mahindra's wording can be read as banning only the
  combination. Both are defensibly "weaker".

This is the Stage 1 lesson again (a label error found by reading the misses): hand labels are a
measurement too, and they need checking.

## Demo findings worth presenting

- **IIFL, gold loan auctions:** the Code has no commitment to refund auction surplus within seven
  working days (RBC para 66). IIFL was fined Rs 3.10 lakh on 15 May 2026 for not paying borrowers
  their auction surplus. The gap tool and the penalty corpus point at the same failure.
- The same check finds IIFL silent on reserve prices, auction location and post-auction statements,
  and credits it for newspaper advertisements and Board-approved auctioneers.
- **Nissan Renault** (Code v4.0, from 2020) still speaks of "penal interest", which the current
  rules forbid: *inconsistent*, not just missing.
- **Muthoot** refuses loan-transfer requests outright for gold loans, against the 21-day rule.

## Limits (for the evaluation report)

- 16 cases is small; each case moves a rate by 0.06.
- The tool compares a policy *document* with the rules. A missing clause means the Code is silent,
  not that the company breaks the rule in practice.
- Up to 8 requirements per topic: a broad topic ("KYC") can hold more obligations than one check covers.
- Applicability is only as good as the extracted requirement's wording (e.g. microfinance-only rules).

## Revisited in Stage 6 (2026-09-30)

Two changes to `check_policy_gap`, both re-measured on this evaluation (`reports/stage3_policy_gap_v5.md`,
empty cache): perfect on every measure, with 0 missed gaps and 0 false alarms.

- **Assessments now run on the SMALL tier** (extraction stays on MAIN). Cold cost per check fell from
  $0.032 to $0.012. SMALL matched MAIN in two cold runs each. SMALL for extraction as well ($0.007) made
  one false alarm in one of two runs, so it was not adopted.
- **A missed gap found by the red-team, and fixed.** The RBI's harsh-recovery rule is a lead-in plus a
  numbered list, and item (2) holds the 9 a.m.–6 p.m. calling window. On some runs the extractor quoted
  only item (1), or only the general sentence before the list. The assessor never saw the hours, so it
  marked Tata Capital's 08:00–19:00 window as met. Now any requirement quote that stops before its list
  ends is completed from the source text by code (`_complete_list`). Asking the model to repair the quote
  was tried first: it cut the list a different way. The evaluation set had no case on this rule, which is
  why it was not caught in Stage 3.
