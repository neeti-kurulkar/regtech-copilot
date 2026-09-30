# Evaluation sets

## Dev sets (this folder)

Every `*.jsonl` here is a **dev** set. Each item was drafted with an AI assistant, then reviewed and labelled by
the builder. Prompts, guards, the retrieval config, the Q&A model tier and the injection detector were all tuned
on these sets, so the scores on them are optimistic. The regression gate (`python -m regtech gate`) runs on
them, and only on them.

| File | Tool | Items |
|---|---|---|
| `retrieval_regulation.jsonl`, `retrieval_internal_policy.jsonl`, `retrieval_enforcement.jsonl` | retrieval (Stage 1) | 24 / 18 / 17 |
| `qa_regulation.jsonl` | grounded Q&A (Stage 2) | 31 |
| `gap_cases.jsonl` | `check_policy_gap` (Stage 3); all rules from the Responsible Business Conduct Directions | 16 |
| `precedent_cases.jsonl` | `find_enforcement_precedent` (Stage 4) | 32 |
| `deadline_dev.jsonl` | deadline extraction (Stage 5) | 11 sections |
| `semantic_pairs.jsonl` | semantic-cache threshold study (Stage 7) | 40 pairs |

## Held-out test sets (`test/`)

A test set has the **same file name and schema** as its dev set, in `data/eval/test/`. The eval commands read
it when `REGTECH_EVAL_SPLIT=test` is set, and refuse to run if the file is missing. For example:

```powershell
$env:REGTECH_EVAL_SPLIT="test"; python -m regtech eval-gap --label test
```

The protocol is what makes the numbers worth reporting:

1. **Write the items before running the system on them.** Do not look at any output first.
2. **Ideally someone other than the builder writes and labels them** (a classmate, or better, a compliance
   professional). Record who did it in each item: `"drafted_by"`, `"labelled_by"`.
3. **Cover what the dev set misses:**
   - gap rules from Directions other than Responsible Business Conduct;
   - Concentration Risk questions (no eval item today);
   - at least as many `met` cases as gap cases.
4. **Freeze the system first**, then run the test set **once** and report the result as it comes out. If you
   change anything afterwards because of a test result, that test set has become dev. Move it here and write a
   new one.
5. Report test results next to the dev results, as `k/n` with an interval (`aip.evals.format_rate`).

Start small: 10–15 items per tool is enough to show whether the dev numbers hold up.
