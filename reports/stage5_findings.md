# Stage 5 - Findings: extract_deadlines / check_upcoming (the compliance calendar)

Generated artefacts: `stage5_deadline_variants.md` (Lab 2 comparison), `stage5_calendar_audit.csv`
(precision audit), `data/processed/compliance_calendar.json` (the calendar).

## What was built

- **`extract_deadlines`** (Lab 1, scaled to a corpus). Every heading section with temporal language
  goes to `aip.llm.structured` with a self-grounding schema: the quote must be verbatim in the
  section, and every number of days, months or years and every date put in a field must appear in
  that quote, as digits or words (`aip.guards.number_in_text` / `date_in_text`). A model that quotes
  "within 14 days" but writes 15 is sent back to repair. Three kinds of deadline: fixed date,
  recurring (with frequency, day/month offset, weekday), and event-triggered (with trigger and period).
- **A party tag**, from a second, cheap pass: does the deadline bind the NBFC (including as a "credit
  institution", or its Board and officers), or another body (a credit bureau, a working group,
  customers, auditors)? Only NBFC duties are shown by default.
- **`check_upcoming(days, as_of)`**, which projects the calendar onto dates: fixed dates; recurring
  duties on the Indian financial-year grid (quarters end 30 Jun, 30 Sep, 31 Dec, 31 Mar), with stated
  offsets in days or months and weekly weekdays; repeated occurrences grouped; duties with no stated
  day listed separately; event-triggered duties listed as standing obligations. Every assumption is
  printed with the result.

## Choosing the extractor (Lab 2)

Four variants on a fully hand-labelled dev set (11 sections, 17 deadlines, plus trap content: a
worked example with sample dates, classification thresholds, a minimum tenure, historical dates):

| Variant | Precision | Recall | F1 | Fields right | Cost (11 sections) |
|---|---:|---:|---:|---:|---:|
| Prompt A, SMALL tier | 0.88 | 0.82 | 0.85 | 0.93 | $0.012 |
| Prompt A, MAIN tier | 0.85 | 1.00 | 0.92 | 0.94 | $0.076 |
| Prompt B, SMALL tier | 0.88 | 0.88 | 0.88 | 1.00 | $0.013 |
| **Prompt B, MAIN tier** | **0.94** | **1.00** | **0.97** | **1.00** | $0.062 |

Prompt B (explicit field rules and an explicit "not a deadline" list) is better on both tiers.
**B-MAIN is the default**:
- A missed deadline is the expensive error in a compliance calendar.
- The build is a one-off batch (about $0.75 for the corpus).
- The cheap tier is unstable: B-SMALL scored F1 0.97 on one run and 0.88 on the next, on an
  unchanged dev set.

B-MAIN's one "false" item is the 30 June 2026 transition date split out of the "within one year"
KYC rule. That is correct under the prompt's own splitting rule, which the gold label did not
anticipate. With 17 items, each item is 0.06.

## Three bugs found by looking at the output (Lab 5)

1. **The section pre-filter was the weakest stage.** The first full build found only 73 deadlines;
   the Responsible Business Conduct Directions produced 4. Per-section diagnosis showed no
   extraction failures, but the regex that picks sections to send only knew "within <digits> days".
   Sections written "within a maximum period of seven working days" were never sent, including the
   gold-auction surplus refund. The dev set could not catch this because its sections were picked by
   hand. The filter now matches any stated period, and a regression test covers it.
   Calendar: 73 to 93 deadlines (RBC: 4 to 10).
2. **A corrupt title from Stage 1.** Governance citations showed a 4,000-character "source"; the
   title regex had matched across the whole table of contents. Fixed at the source with a regression
   test, and Stages 1-4 re-checked. No accuracy changed; see the Stage 1 and 2 findings for the
   details.
3. **Periods in months and duties on other bodies.** "Within two months" was lost ("within ? days"),
   and some entries were duties of credit bureaus or a working group. Month fields and the party tag
   fix both; 10 of 93 deadlines use months, and 10 are tagged as other bodies' duties.

## Precision of the full calendar (audit)

A seeded random sample of 20 calendar entries, each checked against its source section (my review;
the sheet is `reports/stage5_calendar_audit.csv`, open for your spot-check):

- 18 of 20 are real NBFC deadlines (0.90). The misses are a working group's duty (now hidden by the
  party tag) and an optional window ("may apply for deregistration by 31 Dec 2026").
- After the party filter, 18 of the 19 shown entries are real (0.95).
- Fields are right in 17 of 18. The miss: incremental credit files due 4 days after the 9th, 16th
  and 23rd are really recurring, but were classed as event-triggered, so they are listed, not dated.

## The calendar today (as of 30 Sep 2026, next 90 days)

93 deadlines in total (38 recurring, 47 event-triggered, 8 fixed dates). `upcoming 90` shows
**18 obligations on 38 dates**. Examples: the weekly large-borrower default report every Friday;
the full credit-bureau file by the 5th of each month; the quarterly theft and robbery return and
the director-change statement on 15 Oct; the half-yearly relief-measures upload on 30 Oct; the
Data Quality Index review report by 30 Nov. Also listed: 14 recurring duties with no stated day and
43 standing event-triggered duties.

## Limits

- A "quarterly" duty with no stated day is shown at quarter end: an assumption, printed on each such line.
- Holidays are not adjusted, and event-triggered deadlines cannot be dated because the system
  cannot see the events.
- The calendar covers the 12 Directions in the corpus only.
- The party tag still hides one borderline asset-freezing duty and one shared credit-bureau/NBFC
  complaint timeline.
