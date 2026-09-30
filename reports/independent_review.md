# Independent review: RegTech Compliance Copilot

Reviewer stance: a panel member at the final presentation, reading the repo cold on 2026-09-30.
Scope: `BUILD_PLAN.md`, `EVALUATION_REPORT.md`, all `reports/stage*_findings.md`, and selected code in
`regtech/` and `aip/`. Everything below cites a file:line I read or a check I ran. Where I could not verify
something, I say so.

**Checks I ran (all offline, no model calls):**
- `pytest -q`: **139 passed**.
- Offline gate (`AIP_OFFLINE=1 AIP_CACHE_DIR=ci/cache python -m regtech gate`): **GATE PASSED**, 17/17
  metrics, 13 s wall clock. The gate rewrites `reports/stage7_gate.json`; I restored it with `git checkout`.
- Small Python probes that import project functions (quoted in the findings).
- Wilson 95% intervals for the headline rates (table in §5).
- Tabulating the eval JSONL files and the report JSONs.

The review ran on Linux (the cloud checkout at `/home/user/regtech-copilot`, in a scratch venv), not on
the Windows venv. Nothing in the repo was modified except this file.

---

## 1. Verdict

**Yes, it is presentable, and it did build what the plan set out to build.** All eight stages exist. Each
one is measured with the matching lab's method. The failures are written up honestly, and a working
offline CI gate replays the committed cache. That puts it well above a typical course project.

The main weakness is in how the numbers are framed, not in how the system is built:
- Several headline figures are strictly true but read stronger than the evidence. "Citation validity 1.00"
  only checks that each `[n]` index exists. "Missed gaps 0" is 0 of 9 cases, and "false alarms 0" is
  0 of 5.
- Most of the metrics were tuned on the same small, builder-written sets they are reported on.
- The gap check has only been evaluated on one of the 12 Directions.

A sharp panel will find these points within minutes. Fix the framing and state the uncertainty before
Friday. Also note that the actual graded deliverables, the ≤4 slides and the concept note, are **not in
the repo**.

---

## 2. What works well (specific)

1. **Fail-closed design, applied consistently.** Every place a model output enters the system goes
   through a schema check:
   - Q&A: validate, then repair, then refuse (`aip/rag.py:247-267`).
   - Gap assessments: an ungroundable assessment becomes `needs_review` and is never guessed
     (`regtech/policy_gap.py:414-418`).
   - Precedents: no grounded verdict means no precedent is claimed (`regtech/precedent.py:175`).
   - Deadlines: every number must appear in the quote (`regtech/deadlines.py:82-99`).
2. **Grounding is enforced inside the schemas**, so aip's repair loop fixes a fabricated quote like any
   other validation error. Examples: the requirement quote must be verbatim
   (`policy_gap.py:239-251`), and each precedent charge must match one of that case's charges verbatim
   (`precedent.py:118-128`).
3. **Using the eval to find real bugs.** Four real bugs were diagnosed with before/after evidence:
   - the token-budget truncation that looked like over-refusal (stage2_findings §1);
   - process-wide budgets crashing parallel checks (stage3 v1→v2);
   - the section pre-filter losing deadlines (stage5 bug 1);
   - the corrupt Governance title.

   This is exactly the Lab 5 method, and it is well evidenced.
4. **The semantic-cache study** (`reports/stage7_semantic_threshold.md`) is the best single result in
   the project. It rests on a domain insight: "penal interest" vs "penal charges" scores 0.970, and the
   right decision follows from it (ship the cache off). It will land well with a panel.
5. **"No comparable precedent" against a search-only baseline.** The baseline invents a precedent 13/13
   times; the tool does so 0/13. This is a clear, correctly framed safety result
   (stage4_findings "Results").
6. **The regression gate genuinely works.** I reran it offline: it passed in 13 s. It fails closed on a
   cache miss (`regtech/gate.py:141-151`), and there is a planted-bug demo on a separate branch.
7. **Honest failure reporting.** Examples: the I09 policy-washing attack, relabels reported next to the
   original labels, the missed TTFT target, and the κ=0 explanation. The "what it is not safe for"
   section (`EVALUATION_REPORT.md` §6) is strong.
8. **Deterministic entity resolution** that refuses the planned lookalikes. I checked it:
   "Shriram Finance", "Muthoot MCred", "Muthoot Fincorp", "Bajaj Housing Finance" and
   "Tata Capital Housing Finance" are all refused.
9. **The aip extensions are logged** (`aip/CHANGELOG_REGTECH.md`, 30 sections; 14 aip files changed,
   +1,192 lines) and unit-tested (`tests/test_aip_extensions.py`).

---

## 3. What does not work well

Each fix below is **structural**: it changes the mechanism that produced the class of problem, not the
single instance.

### F1. "Citation validity 1.00" measures index range, not support. (High, framing and logic)
- **Location:**
  - `aip/guards.py:264-266`: a citation is "valid" if every `[n]` is in 1..N and at least one exists.
  - `aip/rag.py:105-109`.
  - `aip/rag.py:237, 264`: `citations_valid = ok or refused`, so every refusal counts as valid.
- **Evidence (ran):**
  - `validate_answer("The CCO tenure is 3 years [1]. The CCO must be paid Rs 1 crore. Fraud must be
    reported in 2 days.", 6)` returns `[]`, so two uncited claims pass.
  - `validate_answer("Gold loans need 50% LTV [2].", 6)` returns `[]`, even though [2] may say nothing of
    the kind.
  - Fail-closed turns every invalid answer into a refusal, so the metric is 1.00 **by construction**.
- **Why it matters:** the report presents 1.00 as a quality result (`EVALUATION_REPORT.md:30`, in bold).
  A panel member who asks "does [2] actually say that?" gets no answer from this metric. Support is
  covered only by the faithfulness judge, and that judge is not calibrated on unfaithful answers (F8).
- **Structural fix:**
  - Change the answer contract from free text to a list of claims, each with its own citations. Use
    `aip.llm.structured`, which the project already uses everywhere else.
  - Then "every claim is cited" becomes a schema rule rather than a prompt request.
  - Add a per-claim support check: the cited chunk must contain the claim's numbers and key terms, via
    the existing `number_in_text`, plus the judge.
  - Report two metrics: *citation index validity* (the mechanism) and *citation support rate* (the
    quality).

### F2. A refusal is detected by string prefix, so anything after the refusal sentence skips validation. (High, logic)
- **Location:** `aip/rag.py:85-88` (`startswith(REFUSAL[:40])`) and `aip/rag.py:103-104`, which return
  before the citation check.
- **Evidence (ran):** `validate_answer(REFUSAL + " However, NBFCs must report fraud within 3 days and
  the penalty is Rs 5 crore.", 6)` returns `[]` with `is_refusal=True`. Uncited, possibly invented
  content ships as a "refusal", and the refusal metrics count it as a correct refusal.
- **Structural fix:** make the outcome a typed field instead of text-sniffing. Either:
  - `refused: bool` in the structured answer (the same contract as F1); or
  - at minimum, define a refusal as the answer being *exactly* the refusal sentence after normalisation,
    and validate everything else as an answer.

  One definition, used by the pipeline, the metrics and the service alike.
- **Caveat:** changing validation can trigger repair calls that are not in `ci/cache`. The gate will then
  fail closed and need an online `gate --record` (a paid run, your call).

### F3. Gap-check headline rates have tiny, partly hidden denominators, and the tool was only ever evaluated on one Direction. (High, metrics)
- **Location:**
  - `data/eval/gap_cases.jsonl`: I tabulated it.
  - `regtech/gap_eval.py:85-96`: ambiguous cases are excluded from missed-gap and false-alarm.
- **Evidence (ran):**
  - **All 16 cases test rules from `reg-rbc`**, zero from the other 11 Directions. "Rule found 1.00"
    therefore says nothing about KYC, fraud or microfinance topics.
  - `false_alarm` has 5 eligible cases (G04, G07, G09, G11, G15), so "0 false alarms" is 0/5
    (95% CI 0–0.43).
  - `missed_gap` has 9 eligible cases, so 0/9 (95% CI 0–0.30).
  - Two cases (G12, G16) were relabelled from `met` to `met|weaker`. That change also **removed them
    from both the missed-gap and the false-alarm denominators**.
  - 5 of 16 cases accept two statuses, yet the report calls the metric "status exactly right".
  - Only **16 of the 106 findings** the 15 reports produced are scored (v5 JSON: 57 missing, 25 met,
    21 weaker, 3 inconsistent). The other 90 findings, including the 25 "met" verdicts, are unaudited.
  - The one out-of-sample probe (the red-team's Tata calling hours) found a **missed gap** that the eval
    set could not see (stage3_findings, "Revisited in Stage 6").
- **Structural fix:**
  - Have the report writer emit `k/n` and a Wilson CI for every rate, generated from the JSON so no bare
    rate can appear.
  - Stratify the gap set by Direction × status.
  - Add a random-sample audit of *all* findings, the same method as the Stage 5 calendar audit, so that
    "met" precision is measured, not assumed.

### F4. Every metric was tuned on the set it is reported on; there is no held-out set. (High, metrics)
- **Evidence (read):**
  - Retrieval config picked on the same 24 queries it is reported on, including a relabel that moved
    nDCG from 0.882 to 0.900 (stage1_findings).
  - Q&A fixes and the tier choice made on the same 31 questions (stage2, stage7). 7 of the 31 Q&A
    questions also appear in the retrieval tuning set (I checked the overlap).
  - Gap prompts v1→v5 iterated on the same 16 cases.
  - The injection detector was tuned to drop exactly the patterns that fired on 3 of the 5 controls,
    then reported as FPR 0/5 on those same 5 (stage6_findings "v1 → v2").
  - The deadline variant was chosen on the 11-section dev set and its F1 reported on that set.
  - The gate thresholds were set "just below the recorded values" (`ci/thresholds.yml:1-3`).
- **Structural fix:**
  - Freeze a dev/test split per tool. Tune only on dev.
  - Commit a small test set (even 10–15 items per tool) labelled by someone else, and run it once.
  - Make the report writers refuse to publish a headline number computed on a set marked `dev`.

  The calendar audit is the model to copy: a seeded random sample, checked after the design was frozen.

### F5. The eval data was drafted by the AI assistant, not only "labelled by the builder". (Medium, provenance)
- **Location:**
  - `BUILD_PLAN.md:110`: "small hand-labelled query set (drafted by Claude, reviewed by you)".
  - `EVALUATION_REPORT.md:127`: "labelled by the builder". Stage 4: "I wrote it knowing the 13 charges".
- **Why it matters:** the first panel question about any small eval is "who wrote it?". The honest
  answer is "an LLM drafted it, I reviewed it, and the same person and assistant built the system". That
  is defensible, but only if it is said up front.
- **Structural fix:**
  - Record provenance per eval item: `drafted_by`, `labelled_by`, `reviewed_by` and date, as fields in
    the JSONL.
  - Print the provenance breakdown in each report header.

### F6. The stale-policy signal is non-functional for exactly the stale Codes, and it silently replaced a planned gap type. (Medium, plan drift and logic)
- **Location:**
  - `BUILD_PLAN.md` Stage 3 lists the gap types as "silent / weaker / inconsistent / **predates
    regulation**".
  - Built: `GapStatus` at `policy_gap.py:33` has no "predates". It is a report-level boolean instead,
    `policy_gap.py:445`, which is `None` when the policy has no date.
- **Evidence (read):**
  - `data/manifest.csv` has an empty `publish_date` for `fpc-nrfsi`, `fpc-lnt-microloans`,
    `fpc-mahindra-finance` and `fpc-protium`. So the three Codes the plan calls stale (Nissan Renault
    2020, L&T 2022, Mahindra) get **no** predates flag.
  - Meanwhile every *dated* Code predates the mid-2026 re-issued Directions (reg dates 2025-11 to
    2026-08), so the flag is `True` for nearly everyone else. It is uninformative both ways.
- **Structural fix:**
  - Make `publish_date` required in the manifest schema, or require an explicit `undated` with a reason
    (validated at ingest by `regtech check`).
  - Compute "predates" per requirement, against the effective date of the cited paragraph's obligation,
    not against the Direction's re-issue date.
  - Record the plan change in the findings.

### F7. The gap check's "vague promise can't meet a figure" guard is digit-based. (Medium, logic)
- **Location:** `policy_gap.py:265, 270, 289`: `_FIGURE = re.compile(r"\d")`.
- **Evidence (ran):**
  - The requirement "Refund auction surplus within **seven** working days" has no digit, so the guard is
    off. A vague policy ("will refund surplus promptly") was accepted as `met`.
  - The requirement "within 7 working days" with the policy quote "as per Board policy in **2023**
    guidelines" was accepted as `met`, because any digit, including a year, satisfies the guard.
  - RBI text mixes words and digits ("seven (7) working days"), so both paths occur in the corpus.
- **Why it matters:** this guard is the deterministic protection against the most dangerous error
  (missed gap). As written, the model is the only real defence.
- **Structural fix:**
  - Build one quantity layer, shared by every tool. It parses numbers written as words or digits, with
    units (days, hours, working days, rupees, per cent).
  - Stage 5 already solves the parsing half (`_figure_in` in `deadlines.py`); promote it into
    `aip.guards`.
  - Make requirements carry typed quantities `(value, unit, direction)`, and let code compare the
    requirement's quantity with the policy's. The model extracts; code decides "stricter or weaker".

### F8. The judge calibration does not cover the served model or unfaithful answers. (Medium, metrics)
- **Evidence (read):**
  - The 20 calibration answers are the Stage 2 MAIN-tier run (`reports/stage2_judge_scores.json`,
    A01-A20). The service answers with SMALL (stage7_findings, decision 2).
  - Human correctness labels use only 0 and 2 (18×2, 2×0; the CSV was tabulated), so correctness κ=1.00
    only tests "right vs clearly wrong".
  - Faithfulness: all 20 human labels are 1, so κ is undefined or 0, as the report correctly says.
- **Structural fix:** calibrate the judge on a set built to contain every label value. Seed it with
  deliberately corrupted answers (swapped numbers, wrong paragraph), which gives unfaithful and partial
  cases by construction, and draw it from the *served* configuration. Re-calibrate whenever the served
  tier changes, and gate on κ.

### F9. The service has no authentication, no rate limit and no global spend cap. The "human approval" in production is a domain allowlist. (Medium, security)
- **Location:**
  - `regtech/service.py:290-338`: no auth dependency on any route.
  - `service.py:309`: the budget is per request ($0.25 per agent call), with no daily cap.
  - `service.py:421-431`: unlimited uploads, no expiry.
  - `service.py:190`: `new_agent` passes no `confirm_fn`, so `agent.py:212-215 internal_only` applies.
- **Evidence (read):**
  - Anyone who can reach the port can spend $0.25 per request without limit (denial of wallet) and fill
    the disk with 10 MB uploads.
  - Any caller can reference any upload by path in the question text; there is no per-user namespace.
  - The privileged `send_compliance_report` is approved automatically for any `@nbfc-compliance.internal`
    recipient. The red-team "privileged calls = 0" therefore measures an allowlist, not a human in the
    loop.
  - Mitigation: `main()` binds to 127.0.0.1 (`service.py:457-459`), and §6.6 of the report discloses the
    lack of access control.
- **Structural fix:**
  - Put identity, rate limiting and a global daily budget in **one middleware at the service boundary**,
    so every route inherits them.
  - Namespace uploads by caller.
  - Route privileged tools to an out-of-band approval queue (pending, then approved by a person), never
    to an in-process predicate.

### F10. File-access control lives in the caller, not the tool; the red-team ran without it. (Medium, security)
- **Location:**
  - `policy_gap.py:43`: `PolicyGapRequest.policy_path` accepts any path.
  - `policy_gap.py:320-323`: non-PDF files are read with `read_text`.
  - The only restriction is in the agent wrapper, and only when `policy_root` is set
    (`agent.py:228, 249, 273-279`).
  - `redteam.py:331` and `__main__.py:118` build agents **without** `policy_root`.
- **Why it matters:**
  - The tool itself will read any `.md` or `.txt` file (or an extension-less file such as `.env`) that a
    caller names.
  - The service is safe only because it happens to pass a root.
  - The red-team numbers come from a configuration whose file boundary differs from the served one, and
    the suite has no path-traversal attack. I did not run a live model to test this (no paid calls); this
    point is from reading the code.
- **Structural fix:**
  - Enforce the capability at the resource: `PolicyGapRequest` accepts an opaque `upload_id` that the
    tool resolves inside the upload store, never a filesystem path.
  - Run the red-team against the exact object the service constructs (one factory for both).

### F11. Streaming bypasses the output filter until the verdict. (Low-Medium, security)
- **Location:** `service.py:367-371` puts raw `token` events on the wire. The output filter runs only on
  the final verdict (`service.py:374`).
- **Why it matters:** whatever the draft contains (an external link, an email address, PII, a prompt
  fragment) reaches the client before redaction. The protection depends on the client honouring the
  verdict. The risk is small today, because Q&A sources are RBI text only.
- **Structural fix:** send every byte a client sees through one egress path, either by filtering each
  chunk with a small look-behind buffer or by holding tokens until a sentence boundary has been filtered.

### F12. The "no comparable precedent" flag conflates "none found" with "could not judge". (Medium, logic)
- **Location:**
  - `precedent.py:175`: a structured-output failure sets `verdicts = []`.
  - `precedent.py:193`: `no_precedent = not precedents`, which is `True` on error.
  - `precedent.py:151, 155-160`: only the top 6 of 13 cases are judged.
- **Why it matters:**
  - The message text differs, but the machine-readable flag that the agent and the chain consume says
    "no precedent" when the judge failed.
  - "None among the 13 penalty cases in the corpus" is, strictly, "none among the 6 retrieved".
- **Structural fix:**
  - Make every tool result tri-state: `found | none_found | could_not_judge`.
  - Count `could_not_judge` as a failure in every metric. Today `needs_review` is also silently excluded
    from missed-gap (`gap_eval.py:95`).
  - With 13 cases, judge all of them; the retrieval cut buys nothing.

### F13. Lookalike handling is closed-world. (Medium, logic)
- **Location:** `regtech/entities.py:66` resolves a name if its tokens are a subset of exactly one
  *indexed* company's name.
- **Evidence (ran):**
  - "Muthoot" resolves to Muthoot Finance, even though Muthoot Fincorp and Muthoot MCred are real
    lookalikes (the plan names MCred).
  - "Mahindra" resolves to Mahindra & Mahindra Financial Services; "Tata" to Tata Capital;
    "Ram Finance" to Shri Ram Finance Corporation.
  - Ambiguity is judged only against the 11 companies in the corpus.
- **Structural fix:**
  - Resolve against a universe of real NBFC names (RBI publishes the registered-NBFC list), not the
    indexed subset.
  - Anything that is not an exact legal name or an explicit alias returns a confirmation step.
  - Echo the resolved legal entity in every output.

### F14. The calendar ignores "working days" and has no recall measurement. (Medium, logic and metrics)
- **Location:** `deadlines.py:79` extracts `working_days`, but `shift()` at `deadlines.py:388-389`
  ignores it.
- **Evidence (ran):** "no later than seven (7) working days following the conclusion of that month" is
  projected to 2026-10-07, 11-07 and 12-07 (calendar days; 7 Nov is a Saturday).
- **Why it matters:**
  - The error is in the early direction, so it is the safe one. But the tool shows a precise date that is
    wrong.
  - The audit measures **precision** only (18/19). Calendar **recall** is unmeasured, even though the
    pre-filter bug showed recall is where it broke (73→93 deadlines).
- **Structural fix:**
  - Contract rule: every extracted field must be consumed by the scheduler or rejected at validation.
    No silent fields.
  - Add a business-day calendar module (RBI holiday list) behind `shift()`.
  - Add a recall audit: a seeded sample of sections that produced *no* deadline, read by a person.

### F15. Some cost figures are mislabelled or will soon be wrong. (Low-Medium, metrics)
- **Checked:** the arithmetic in `EVALUATION_REPORT.md` §4 is correct.
  - Q&A: 0.000724 × 10,000 × 365 = $2,643.
  - Agent: 0.0093 → $33,945.
  - Mix: 0.7 × 0.000724 + 0.3 × 0.0093 = $0.00330 → $12,034 per year, and × 0.8 = $9,627.
- **Issues:**
  1. "(upper bound) $55,000" is not an upper bound. The observed max is **$0.0299** per agent query
     (`reports/stage6_redteam_default.md:18`), about $109k/yr at that rate.
  2. The agent mean comes from the red-team mix, not a realistic workload: 10 of 23 runs are gap checks,
     and 2 are blocked for $0.
  3. The 20% repeat rate has no evidence behind it.
  4. MAIN doubles in price on 2027-01-01 (`aip/config.py:142`). The agent loop, gap extraction and
     precedent judge run on MAIN, so the yearly figure is out of date one quarter after the viva. My
     estimate, not run: roughly $18–21k per year for the same mix.
  5. The stage 6 target "cost per query ≤ $0.02" is met on the mean but not on the max.
- **Structural fix:** a cost model file (workload mix, repeat rate, price schedule by date) that the
  report reads, so each assumption is named once, dated, and changeable.

### F16. The report text drifts from the artefacts, and some dates are in the future. (Low, credibility)
- **Evidence (read):**
  - `EVALUATION_REPORT.md:52` says status under the original labels is 0.75. `stage3_policy_gap_v5.json`
    says **0.875** (0.75 was v4).
  - The report says the gate runs in "27 s"; `stage7_gate.json` records 10.3 s, and I measured 13 s.
  - `stage2_findings.md:19,115`, `stage3_findings.md:92` and `aip/CHANGELOG_REGTECH.md:183,222` are
    dated **2026-10-01**, but the commits that contain them are dated 2026-09-29/30 (`git log`), and
    today is 2026-09-30.
- **Why it matters:** a panellist who notices a date in the future will doubt the claim that the human
  labels were made blind.
- **Structural fix:** generate the headline tables and dates from the JSON artefacts and `git log`, with
  a test that fails when a number in `EVALUATION_REPORT.md` does not match its source.

### F17. The attack block rate is mostly the base model's refusals. (Low, framing)
- stage6_findings says it plainly: 16/18 attacks were blocked with *no* layers, and only `privilege`
  changed an outcome.
- The headline in `EVALUATION_REPORT.md` §2, "17/18 attacks blocked", reads as credit to the guardrails.
- **Structural fix:** always report a guardrail result as a *marginal* table (layer off vs on). The data
  already exists in `stage6_redteam.md`.

---

## 4. Corpus adequacy

**Adequate for a scoped demo. Not adequate for the implicit claim "a compliance copilot for NBFCs".**

- **Regulations (12 Directions):**
  - Good choice of core conduct and prudential Directions.
  - Missing Directions the report itself names: IT and outsourcing, the Internal Ombudsman,
    securitisation. Digital lending, IT governance and cyber security are also absent.
  - Two of the 13 penalties have no matching rule in the corpus.
  - **Concentration Risk has zero eval items** in any of the retrieval, Q&A or gap sets (grep over
    `data/eval`).
  - Evaluation is heavily RBC and KYC: 7 and 5 of the 31 Q&A items, and all 16 gap cases.
- **Codes (11):**
  - Two are web-page printouts.
  - Four are undated (F6), and at least three are stale.
  - L&T's Code covers microloans only (`fpc-lnt-microloans`), but `entities.py` maps "L&T Finance" to it.
    A non-microfinance question about L&T would be checked against the wrong scope.
- **Enforcement (13 releases, all 2026):**
  - A convenience sample. It is fine for showing that the precedent tool refuses correctly.
  - It is too thin for any risk statement, which §6.3 correctly says. Retrieval on it is saturated
    (stage1_findings item 4), so the ranking numbers there carry no information.
- **Structural suggestion:** a generated coverage matrix (Direction × corpus × eval items), committed and
  shown on one slide. It turns "blind outside 36 docs" from a disclaimer into a visible map.

---

## 5. Metrics: trustworthiness table

The Wilson 95% intervals are from my computation.

| Claimed (EVALUATION_REPORT §2) | How it is computed | Caveats | Adjusted reading |
|---|---|---|---|
| Retrieval nDCG@10 0.90 / 0.96 / 0.99 | aip `evidence_retrieval_metrics` on 24/18/17 queries | Config chosen on the same queries; one relabel (+0.018); enforcement saturated; LLM-drafted queries | Good on these queries; ±0.02 is noise (the report says so). Enforcement numbers carry no information. |
| Q&A correctness 0.85–0.92 (SMALL, 3 runs) | Judge 0/1/2 on 24 answerable questions | Tuned on the same set; judge calibrated on MAIN answers, not SMALL; 1 question = 0.04 | "About 0.85–0.92 on 24 builder-written questions." Run-to-run spread (0.854–0.917) is the same size as the tier difference. |
| Faithfulness 0.94–0.97 | Judge 0/1 on 31 answers | Judge never saw a human-labelled unfaithful answer | As the report says: "no unsupported claim found", not a rate. |
| Citation validity **1.00** | `[n]` within 1..N, ≥1 cite; refusals count as valid (`aip/rag.py:237`) | True by construction (fail-closed); ignores support; prefix-refusal bypass (F2) | Rename to "citation index validity (enforced)". No evidence yet on citation *support*. |
| Refusal recall 5/5, precision 0.71 | `refusal_metrics` on 5 unanswerable questions + 7 refusals | n=5: recall CI 0.57–1.00; precision CI 0.36–0.92 | Report as 5/5 and 5/7 with n. |
| Judge vs human κ 1.00 (correctness) | 20 MAIN answers, labels only 0 or 2 | Not the served tier; no partial answers; human is the builder | "Agrees on clear right vs wrong on MAIN answers." |
| Gap: rule found 1.00, status 1.00 | 16 cases; "status exact" = status in the accepted set | 5/16 accept 2 statuses; 4 relabels toward tool output; all cases from one Direction; tuned on the set | 16/16 (CI 0.81–1.00) on current labels; 14/16 = 0.875 on original labels (v5). RBC only. |
| **Missed gaps 0** | Cases with a single gap label only (`gap_eval.py:85-96`) | Denominator 9; relabels removed G12 and G16; only 16/106 findings scored; red-team found a missed gap outside the set | 0/9, CI 0–0.30. Not evidence of a low missed-gap rate in general. |
| False alarms 0 | Cases labelled `met` only | Denominator 5 | 0/5, CI 0–0.43. |
| Precedent: right first 1.00; invented 0.00 | 19 positive / 13 negative risks, builder-written knowing the 13 charges | Positives saturated; judge only sees the top 6 of 13 | The negatives result is the real one: 0/13 invented vs 13/13 for the baseline (CI 0–0.23). |
| Deadlines P 0.94 / R 1.00; audit 18/19 | Dev set of 11 sections (17 items) used to *choose* the variant; audit of a seeded sample of 20 | Dev-set numbers are selection-biased; audit is precision only | Trust the audit (0.95, CI 0.75–0.99). Recall of the full calendar: **unmeasured**. |
| Agent: 17/18 blocked, 0/5 FP, 0 privileged | Deterministic criteria on 23 cases | 16/18 blocked with no layers; detector tuned on the same 5 controls; privilege = domain allowlist; built without `policy_root` | Block rate 0.94 (CI 0.74–0.99), mostly the base model. FP 0/5 (CI 0–0.43) after tuning on those controls. |
| Cost $0.0033 per query; $12k per year | Recorded cold costs × assumed mix | Arithmetic checks out; mix, repeat rate and "upper bound" are assumptions; price doubles in 3 months | Label as a scenario; agent max is $0.030 per query. |
| Q&A p95 2.1 s | n=12, serial, in-process, one laptop | No load test | Fine as stated (the report says so). |

---

## 6. Plan vs built

| Stage | Plan (`BUILD_PLAN.md`) | Built | Status |
|---|---|---|---|
| 0 | Scaffold, manifest schema, env check with a live call | `regtech check`, Pydantic manifest, tests | **Done** |
| 1 | PDF→MD with clause structure; 36 docs; 3 indices; Lab 3 sweep; queries drafted by Claude and reviewed by you | All done; sliding@800 / 1600 / md@800 chosen | **Done**. Reranking not run (disclosed). 4 Codes lack a date in the manifest (**partial**). |
| 2 | Citation-enforced Q&A, invalid citations caught, explicit refusal | `RegulationQA` over `aip.rag` | **Done**, but "citation" means index validity only (F1) and refusal is a prefix match (F2) |
| 3 | Entity or path + topic; per requirement: reg clause, policy clause or "not found", gap type **silent/weaker/inconsistent/predates**; unit tests; Lab 5 diagnosis | met / weaker / inconsistent / missing / needs_review; predates became a report flag | **Silently changed**: "predates" is not a gap type, and the flag is `None` for the stale Codes (F6). Eval covers the RBC Direction only (F3). |
| 4 | Cited cases; explicit "no comparable precedent"; unit tests | Case table + per-candidate verdicts + chaining from gaps | **Done**. The error case collapses into "no precedent" (F12). |
| 5 | Pydantic + repair; fixed / recurring / event kinds; `check_upcoming` | All; plus party tagging and an audit | **Done**. Working days ignored (F14); recall unmeasured. |
| 6 | ToolGuard loop; red-team incl. poisoned PDF and lookalikes; Lab 6 targets | 5 layers, 18+5 suite, all targets met | **Done**. Approver is simulated, also in the service (F9); red-team ran without the file boundary (F10). Mean cost meets ≤$0.02; max ($0.030) does not. |
| 7 | FastAPI `/ask`, `/metrics`, caching with a measured semantic threshold, streaming, tracing, dashboard, CI gate | All, plus `/upload`, `/health`, `/ask/stream` | **Done**. TTFT target missed (disclosed); no auth or limits (F9). |
| 8 | `EVALUATION_REPORT.md` with a "not safe for" section | Present, with an appendix mapping numbers to sources | **Done**; some framing issues (F1, F3, F15-F17) |
| Deliverables | ≤4 slides + concept note (due Fri 2026-10-02) | **Not in the repo** | **Missing** (they may exist elsewhere; I could not see them) |

The plan's ground rule "no functionality beyond the brief without checking first" appears respected:
`/upload`, `/health` and the dashboard are each recorded as "agreed before building" in stage7_findings.

---

## 7. Panel questions

| # | Question a sharp panel would ask | Legit answer in the repo? | Evidence |
|---|---|---|---|
| 1 | "Who wrote and labelled your test sets? Were they independent of the builder?" | **PARTIAL** | Disclosed as builder-labelled (§6.7). The plan says drafted by Claude (`BUILD_PLAN.md:110`), and the report does not mention that. |
| 2 | "Did you tune on the same data you report?" | **PARTIAL** | Before/after rounds are documented (stage 3, stage 6 v1→v2), but there is no held-out set and no statement of that in §2 (F4). |
| 3 | "Citation validity 1.00: does the cited paragraph actually support the sentence?" | **NO** | Only index validity is checked (F1); faithfulness judge not calibrated on unfaithful answers (F8). |
| 4 | "How sure are you about 0 missed gaps?" | **NO** | 0/9, CI up to 0.30; red-team found one outside the set (F3). |
| 5 | "Does the gap check work beyond fair-practices topics?" | **NO** | All 16 cases from `reg-rbc`. |
| 6 | "What if the uploaded policy is a forgery?" | **YES** | I09 analysis and the provenance plan (stage6_findings; EVAL §6.1, §7.1). |
| 7 | "Why no semantic cache?" | **YES** | `stage7_semantic_threshold.md`: a near-miss is the top-scoring pair. |
| 8 | "Is 'no precedent' evidence of low risk?" | **YES** | EVAL §6.3; tool message wording (`precedent.py:191`). |
| 9 | "Which guardrail actually did anything?" | **YES** | stage6_findings "What each layer actually did". Headline framing should match it (F17). |
| 10 | "Is the judge valid for the model you serve?" | **NO** | Calibrated on MAIN; the service answers with SMALL (F8). |
| 11 | "What does it cost at scale, and after the price change?" | **PARTIAL** | Arithmetic correct; mix and repeat rate assumed; 2027 price flagged but not quantified (F15). |
| 12 | "How does it handle Muthoot Fincorp vs Muthoot Finance?" | **PARTIAL** | Fincorp and MCred refused; bare "Muthoot" silently resolves (F13). |
| 13 | "Does it flag an outdated Code?" | **NO** | The flag is `None` for the stale Codes and `True` for nearly all others (F6). |
| 14 | "How many real deadlines does the calendar miss?" | **NO** | The audit is precision-only (F14). |
| 15 | "What stops someone running up your bill or reading other users' uploads?" | **PARTIAL** | Per-request budget and localhost bind; no auth, rate limit or global cap; disclosed in §6.6 (F9). |
| 16 | "Did a compliance professional ever use or judge it?" | **NO** | Only mentioned as next step §7.2. |
| 17 | "Does the CI gate catch provider drift?" | **YES** (as a limitation) | stage7_findings "Honest limits". |
| 18 | "What happens when a model output fails validation?" | **YES** | Repair then fail-closed (`aip/rag.py:247-267`), `needs_review`, empty verdicts. |
| 19 | "Why these 13 penalties? Isn't that cherry-picked?" | **PARTIAL** | "All dated 2026" is a stated rule; the selection criterion within 2026 is not stated. |
| 20 | "Why did retrieval choose sliding windows over the Lab 3 winner?" | **YES** | stage1_findings item 1 (0.819 vs 0.900, fragmenting). |

---

## 8. Prioritised fix list (to Friday 2026-10-02)

These are **structural** changes: each one removes the mechanism that produced a class of issues. They
are scoped to fit the time left. Items marked (online) need a paid re-record of the gate, which is your
call.

### Must fix before the demo
1. **Produce the slides and the concept note.** They are the graded artefacts and are not in the repo.
2. **Make every headline rate self-describing.**
   - Add one `format_rate(k, n)` helper that prints `k/n (CI lo–hi)`, used by every report writer.
   - Regenerate the §2 table of `EVALUATION_REPORT.md` from the JSON artefacts, with a test that fails
     when a number in the markdown does not match its source.

   This fixes F3's framing, F16's drift (0.75 vs 0.875, 27 s) and much of Q4 in one move, with no model
   calls.
3. **Define "refusal" in exactly one place, as a typed outcome** (F2). The minimal version is "refusal ==
   the refusal sentence, exactly, after normalisation; anything else is validated as an answer". Use it
   in the pipeline, the metrics and the service. (Offline tests are free. Re-recording the gate is
   (online) *only if* a recorded answer starts with the refusal and continues.)
4. **Make tool results tri-state** (`found | none_found | could_not_judge`), and count `could_not_judge`
   and `needs_review` as failures in the metrics (F12). Pure code, offline, and the gate replays
   unchanged.
5. **Make manifest dates mandatory, or an explicit `undated` with a reason**, validated by
   `regtech check` (F6). Then fill the 4 dates from the documents, so the stale-Code demo (Nissan Renault
   2020) visibly works.

### Should fix in the report text (no code)
- Rename "citation validity" to "citation index validity (enforced by code)". Say that support is judged
  only by the uncalibrated faithfulness judge (F1, F8).
- Replace "status exactly right" with "status within the accepted labels". Show 0/9 and 0/5, with CIs.
  Say that all gap cases are from the Responsible Business Conduct Direction (F3).
- Add one sentence of provenance: "eval items drafted with an AI assistant and reviewed and labelled by
  me; all tuning was done on these same sets; no held-out set" (F4, F5).
- Give the agent block rate as a marginal result: "16/18 refused by the base model; privilege layer
  stopped the 17th" (F17).
- Cost: remove "upper bound". Add the observed max ($0.030) and the post-2027 estimate. Label the mix
  and the 20% repeat rate as assumptions (F15).
- Say that the served "approval" is a domain allowlist (F9).
- Fix the 2026-10-01 dates (F16).

### Can mention as a limitation (with the structural fix as "next step")
- Gap guard is digit-based; the next step is a shared typed-quantity layer (F7).
- Closed-world entity resolution; resolve against the RBI's registered-NBFC list (F13).
- Working days ignored; calendar recall unmeasured; business-day module plus a recall audit (F14).
- The file boundary lives in the agent wrapper; move it to an `upload_id` capability inside the tool
  (F10).
- Streamed tokens are unfiltered until the verdict; use a single egress filter (F11).
- No auth, rate limit or global cap; add service-boundary middleware (F9).
- Judge calibrated on MAIN answers only; build a seeded calibration set covering every label value (F8).
- Corpus coverage: publish the Direction × eval coverage matrix; Concentration Risk has no eval items
  (§4).

---

## 9. Status after the first round of fixes (2026-09-30)

All fixes below were checked offline: 149 tests pass, and the offline gate passes (19 metrics). No change alters
a model request, so the committed CI cache still replays.

| Finding | Status | What changed |
|---|---|---|
| F1 citation support | **measured** | `aip.guards.citation_support`: sentence-level, deterministic. On the served Q&A, 40/40 claims carry their own citation and 29/29 numeric claims have every number in the source they cite. Both are gated. The claims-with-citations answer contract is still a next step. |
| F2 refusal prefix | **fixed** | A refusal is exactly the refusal sentence; "refusal + more text" is sent back for repair. Regression test. |
| F3 denominators | **fixed** | Headline table generated with k/n and 95% CI, with a drift test. The gap eval's missed-gap and false-alarm rates are now over the cases where each error is possible (they were diluted over 14 cases; the false-alarm gate threshold was therefore looser than it read). |
| F4 / F5 tuning and provenance | **text + infrastructure** | Stated in the report. `REGTECH_EVAL_SPLIT=test` reads held-out sets from `data/eval/test/` (protocol in `data/eval/README.md`); the gate refuses to run on them. The test sets themselves still need writing. |
| F6 stale-policy flag | **fixed** | The manifest requires a date or an explicit `undated`, plus `date_basis`. The four missing dates were filled from PDF metadata (L&T, Nissan Renault, Protium) or as "not before" (Mahindra). Only an exact date settles "predates". |
| F8 judge calibration | **prepared** | `python -m regtech judge-sheet` writes a blind 36-row sheet from the served answers: 12 originals plus 8 each with a wrong number, half the answer, or an unsupported cited claim. Needs your labels, then `judge-kappa --v2` (online). |
| F12 precedent outcome | **fixed** | `outcome: found / none_found / could_not_judge`; a failed judgement no longer reads as "no precedent" and counts as a failure in the eval. |
| F15 cost framing | **fixed (text)** | Observed max, post-2027 estimate, assumptions labelled. |
| F16 drift and dates | **fixed** | Table generated; 0.875; dates corrected to 2026-09-30. |
| F17 guardrail framing | **fixed (text)** | Base model alone blocks 16/18, in the generated table. |
| F7, F9, F10, F11, F13, F14 | **open, stated as limitations** | Listed in EVALUATION_REPORT §6 and §7. |
