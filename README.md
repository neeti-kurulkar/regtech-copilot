# RegTech Compliance Copilot

A compliance assistant for Indian NBFCs (non-bank lenders), built on the course's `aip` library. It answers
from three document sets and cites every claim:

| Corpus | What it is | Used for |
|---|---|---|
| 12 RBI Directions | the rulebook | "What does the RBI require?" |
| 11 Fair Practices Codes | what companies say they do | "Where does this company's Code fall short?" |
| 13 RBI penalty orders (2026) | what failure costs | "Has the RBI fined anyone for this?" |

Four tools sit behind one agent: `ask_regulation`, `check_policy_gap`, `find_enforcement_precedent` and
`check_upcoming`. They are served over HTTP with guardrails, caching, streaming, tracing and a CI gate.
**It is not legal advice**; see `EVALUATION_REPORT.md` for what it must not be used for.

Plain-language walkthrough of every stage: [`docs/project_guide.html`](docs/project_guide.html).
Stage plan and all commands: [`BUILD_PLAN.md`](BUILD_PLAN.md).

## Run it (about five minutes)

Needs Python 3.11 to 3.14. Windows commands are shown; on macOS/Linux use `source .venv/bin/activate`.

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Without an API key**, you can replay what CI replays: the tests and the regression gate run offline from
the committed cache.

```bash
python -m pytest -q
$env:AIP_OFFLINE="1"; $env:AIP_CACHE_DIR="ci/cache"; python -m regtech gate
```

**With a Gemini key** (put `GEMINI_API_KEY=...` in a `.env` file at the repo root):

```bash
python -m regtech serve
```

Then, in a second terminal:

```bash
streamlit run regtech/ui.py
```

- UI: http://localhost:8501 (Ask the rules / Ask the agent; citations expand to the source text).
- API docs: http://localhost:8000/docs.

The first start embeds the corpus (a few minutes, about $0.05; cached afterwards).

```bash
curl -s localhost:8000/ask -H "content-type: application/json" -d "{\"question\": \"Within how many days must property documents be returned after repayment?\"}"
curl -s localhost:8000/ask -H "content-type: application/json" -d "{\"question\": \"Check Tata Capital's Code on the Key Facts Statement\", \"mode\": \"agent\"}"
curl -s localhost:8000/metrics
```

Operations dashboard (reads the traces):

```bash
streamlit run regtech/dashboard.py
```

## The service

| Endpoint | What it does |
|---|---|
| `POST /ask` `{question, mode: qa\|agent, upload_id?, as_of?}` | Answer, citations, cost, whether it was cached, and a **trace id** |
| `POST /ask/stream` | Q&A as server-sent events: sources, tokens, then a **verdict** that confirms the citations or retracts the draft |
| `POST /upload` | A policy PDF for the agent to check (the only files the agent may read) |
| `GET /health` | Index sizes, models, caches, corpus version |
| `GET /metrics` | Cost, latency p50/p95/p99 by mode, cache hit rate, refusal rate, errors by status, tool calls, alerts |

Errors: a malformed request gets 422, a spend limit 429, a provider outage 503 with `Retry-After`, and a bug
500 with the trace id (never a stack trace).

## Repository map

| Path | Contents |
|---|---|
| `aip/` | The course library, extended where the project needed something general. Every change is logged in `aip/CHANGELOG_REGTECH.md` |
| `regtech/` | The project: ingestion, indices, the four tools, the agent, the service, UI, dashboard, gate |
| `data/` | Corpora (PDFs), processed Markdown, the manifest, and the hand-labelled evaluation sets |
| `reports/` | Every stage's measurements and findings |
| `ci/` | Gate thresholds and the slim replay cache |
| `tests/` | Offline unit tests, plus the red-team's poisoned policy fixtures |
