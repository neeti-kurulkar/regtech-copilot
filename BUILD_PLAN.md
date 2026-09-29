# RegTech Compliance Copilot — Build Plan

Reference copy of the staged plan and the decisions made before building.
Each stage ends with a stop: summary, how to verify, and explicit approval
before the next stage starts.

**Assignment:** AI-in-Practice I, final assignment, Option 1 (Fintech track).
**Due:** Friday 2026-10-02, end of day.
**Deliverables:** a presentation (at most 4 slides) and a concept note.
**Viva:** 5 min presentation + 3 min questions, before 2026-10-06.
Lab 7 is being done separately; this project still builds all stages 0–8.

---

## Ground rules

- Build on `aip`, never beside it: use aip's harness, metrics, retrievers, chunkers, budgets
  and cache instead of writing parallel versions. When the project needs something general,
  add it to the right `aip` module and log it in `aip/CHANGELOG_REGTECH.md`.
- Project-specific code (NBFC corpora, the three tools, the agent) lives in `regtech/`.
- Windows-native: no Makefile or bash tooling; commands run as
  `python -m regtech <command>`, and paths use `pathlib`.
- Provider: `gemini` profile (key in `.env`, never committed).
- No functionality beyond the brief without checking first.

---

## Commands

Run from the repo root with the venv active (`.venv\Scripts\Activate.ps1`):

| Command | What it does |
|---|---|
| `python -m regtech check` | Environment, all `aip` imports, settings, corpus layout, manifest validity |
| `python -m regtech check --live` | The same, plus one tiny chat + embed call (cached after the first run) |
| `python -m regtech init-manifest` | Create `data/manifest.csv` with its header row |
| `python -m regtech ingest [--only DOC_ID]` | PDF → Markdown for every manifest doc into `data/processed/`; writes `reports/stage1_ingestion.md` |
| `python -m regtech eval-retrieval [--corpus C]` | Lab 3 retrieval sweep per corpus; writes `reports/stage1_retrieval.{md,json}` |
| `python -m regtech ask "QUESTION" [--strict]` | Grounded, cited answer from the RBI Directions |
| `python -m regtech eval-qa [--no-judge]` | Lab 4 evaluation of the Q&A; writes `reports/stage2_qa.{md,json}` and the judge calibration sheet |
| `python -m regtech gap "COMPANY" "TOPIC" [--file PATH] [--json]` | `check_policy_gap`: the company's Code vs the RBI rules on a topic |
| `python -m regtech gap "COMPANY" "TOPIC" --precedents` | Gap check, then a precedent search for every gap found |
| `python -m regtech build-cases` | Extract the structured enforcement case table (`data/processed/enforcement_cases.json`) |
| `python -m regtech precedent "RISK"` | `find_enforcement_precedent`: has the RBI penalised this kind of failure? |
| `python -m regtech eval-precedent` | Stage 4 evaluation; writes `reports/stage4_precedent.{md,json}` |
| `python -m regtech eval-deadlines` | Stage 5 Lab 2 comparison of four extraction variants; writes `reports/stage5_deadline_variants.*` |
| `python -m regtech build-calendar [--prompt B --tier MAIN]` | Extract every deadline in the Directions (and tag who it binds) into `data/processed/compliance_calendar.json` |
| `python -m regtech upcoming [DAYS] [--as-of YYYY-MM-DD]` | `check_upcoming`: what is due in the next N days |
| `python -m regtech deadlines DOC_ID` | `extract_deadlines` for one document |
| `python -m regtech audit-calendar [--n 20]` | Seeded random sample of calendar entries for a precision audit |
| `python -m regtech agent "MESSAGE" [--layers default\|all\|none\|delimit+detect+...] [--confirm]` | Stage 6 agent: picks and calls the tools under the guardrail layers (default: all but `structured`); `--confirm` asks you before the (simulated) send tool runs |
| `python -m regtech make-fixtures` | Write the poisoned Tata Capital policy PDFs into `tests/fixtures/redteam/` (never into the corpus) |
| `python -m regtech redteam [--final-only\|--default-only] [--only ID]` | Stage 6 red-team: 18 attacks + 5 controls under 6 cumulative layer configs (or just the default); writes `reports/stage6_redteam*.{md,json}` |
| `python -m regtech eval-gap [--label L] [--tier T] [--assess-tier SMALL]` | Stage 3 evaluation (assessments on SMALL by default since Stage 6); writes `reports/stage3_policy_gap_<L>.{md,json}` |
| `python -m regtech judge-kappa` | Cohen's κ between the LLM judge and your hand labels in `reports/stage2_judge_calibration.csv` |
| `python -m pytest` | Offline unit tests |

Later stages add their own subcommands here.

---

## The corpus (as finalised before Stage 0)

| Corpus | Docs | Contents |
|---|---|---|
| Regulations ("the rulebook") | 12 | RBI NBFC Directions: Compliance Function, Fraud Risk Management, KYC, Scale Based Regulation, Responsible Business Conduct, Income Recognition/Asset Classification/Provisioning, Credit Information Reporting, Microfinance Institution, Credit Facilities, Concentration Risk Management, Governance, Resolution of Stressed Assets |
| Internal policies ("what companies say they do") | 11 | Fair Practice Codes: Bajaj Finance, Cholamandalam, IIFL, L&T Finance (microloans), Muthoot, Nissan Renault FS, Protium, Shri Ram Finance Corporation, Tata Capital, Manappuram (web page), Mahindra Finance (web page) |
| Enforcement ("what failure costs") | 13 | RBI penalty press releases, all dated 2026: Five Star, Fusion, Hero FinCorp, Hinduja Leyland, IIFL ×2, Mahindra, Manappuram, Muthoot, Ola FS, Sammaan Finserve, Satya MicroCapital, Shri Ram Finance Corporation |

**Companies with both a Code and a penalty (end-to-end demos):** IIFL (2),
Muthoot, Manappuram, Mahindra, Shri Ram Finance Corporation.

**Known coverage limits:** Mahindra's internal-ombudsman breach and
Hinduja's securitisation breach have no matching regulation in the corpus.
Several penalty releases cite pre-Nov-2025 rulebooks, so penalties link to
regulations by topic, not by exact provision.

**Stale Codes kept deliberately** (they are what the company publishes;
"policy predates current rules" is a legitimate finding): Cholamandalam
(Mar 2025), L&T (2022), Nissan Renault (2020), Mahindra (undated, cites
2021–22 circulars).

---

## Stages

### Stage 0 — Scaffolding
Repo structure, `regtech/` package, `pyproject.toml`, `.gitignore`,
`data/corpus/{regulations,internal_policies,enforcement}/`, and the
`manifest.csv` schema (validated with Pydantic). An environment check that
imports every `aip` module and makes one tiny live model + embedding call.
No retrieval or generation logic.

### Stage 1 — Corpus ingestion
- PDF → Markdown, preserving heading structure (chapter / paragraph
  numbering) so answers can cite clause-level locations. Strip bilingual
  letterheads, tables of contents, page headers/footers and website chrome.
- Populate `manifest.csv` for all 36 documents.
- Three separate chunked + embedded indices (one per corpus) using
  `aip.chunking` and `aip.embed` / `aip.retrieval`.
- Lab 3 methodology per corpus: compare chunking strategies, measure on a
  small hand-labelled query set (drafted by Claude, reviewed by you).
  Enforcement is tiny (13 one-page docs), so it reports hit_rate@1/@3 and
  MRR rather than leaning on nDCG@10.
- No answer generation.

### Stage 2 — Baseline grounded RAG
Citation-enforced Q&A over the regulation corpus only (Lab 4/5 pattern):
numbered sources, invalid citations caught deterministically, explicit
refusal when the corpus does not support an answer. This is the
reliability floor every later tool must meet.

### Stage 3 — Tool: `check_policy_gap`
- Input: an NBFC name (indexed) or a local policy file path, plus a topic.
- Retrieves from the regulation index and that entity's policy chunks
  independently, then reconciles them.
- Output (Pydantic contract): per requirement — cited regulation clause,
  cited policy clause or "not found", gap type (silent / weaker /
  inconsistent / predates regulation), plain-language description.
- Unit-tested in isolation. Lab 5-style failure diagnosis (retrieval vs.
  generation) with before/after evidence.

### Stage 4 — Tool: `find_enforcement_precedent`
- Input: a described risk, or a gap from `check_policy_gap`.
- Output: cited case(s) — entity, date, penalty amount, violation.
- Must return an explicit "no comparable precedent" rather than the
  nearest irrelevant case. Unit-tested in isolation.

### Stage 5 — Tool: `extract_deadlines` / `check_upcoming`
- Lab 1 reliability pattern: validated Pydantic schema, repair retries.
- Three deadline kinds: **fixed date** (effective dates), **recurring**
  (cadence + offset, projected onto the calendar from an as-of date,
  default today), **event-triggered** ("within 7 days of detection" —
  listed separately, cannot be placed on a calendar).
- `check_upcoming(n_days)` answers "what's due in the next N days".
- Unit-tested in isolation.

### Stage 6 — Agent orchestration + guardrails
- Tool-calling loop over the three tools with `ToolGuard` budgets and
  validated tool contracts (Lab 6 pattern).
- Red-team suite including RegTech-specific attacks, e.g. a poisoned
  internal-policy PDF attempting prompt injection (kept as a test fixture,
  never in the real corpus), and lookalike-entity confusion
  (Shri Ram Finance Corporation vs Shriram Finance; Muthoot Finance vs
  Muthoot MCred).
- Lab 6 targets: attack block rate ≥ 0.80, false-positive rate ≤ 0.25,
  privileged tool invoked by any attack = 0, 100% of tool calls validated,
  loop always terminates, cost per query ≤ $0.02.

### Stage 7 — Service layer
FastAPI (`/ask`, `/metrics`), caching with a measured semantic-cache
threshold, streaming, tracing, cost dashboard, CI regression gate replaying
the committed cache with `AIP_OFFLINE=1` (Lab 7 pattern).

### Stage 8 — Evaluation report
`EVALUATION_REPORT.md` with all required sections, including an explicit
"what it is not safe for" section (not legal advice; blind to regulation
outside the ingested corpus; stale-policy and old-rulebook caveats; etc.).

---

## Assignment deliverables (outside the stage plan)
- Concept note.
- Presentation, at most 4 slides.
