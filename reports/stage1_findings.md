# Stage 1 - Findings

Companion to the generated tables in `stage1_ingestion.md` and `stage1_retrieval.md`.

The evaluation runs on aip's own harness: each configuration is one `aip.evals.run_eval`, each
sweep step one `aip.evals.compare`, relevance scored by `aip.evals.evidence_retrieval_metrics`.
The aip additions this needed are listed in `aip/CHANGELOG_REGTECH.md`.

## Chosen index per corpus

| Corpus | Config | nDCG@10 | recall@5 | hit@1 | MRR | Queries |
|---|---|---:|---:|---:|---:|---:|
| Regulations (12 docs) | sliding 800 chars, dense | 0.900 | 0.958 | 0.792 | 0.872 | 24 |
| Internal policies (11 docs, company-scoped search) | sliding 1600 chars, dense | 0.959 | 1.000 | 0.889 | 0.944 | 18 |
| Enforcement (13 docs, document-level) | markdown 800 chars, dense | 0.995 | 1.000 | 1.000 | 1.000 | 17 |

Lab 3 targets (nDCG@10 >= 0.80, recall@5 >= 0.85, hit_rate@1 >= 0.65) are met on all three.
Embedding spend for the whole sweep: about $0.27 (first builds; every re-run is $0 from the aip cache).

## What the measurements say

1. **Lab 3's winning chunker loses here.** Heading-aware `markdown_chunks` won on the Aurora corpus;
   on RBI directions it scores nDCG@10 0.819 vs 0.900 for sliding windows. The directions have many
   short sub-sections (A.1, A.2, ...), and `markdown_chunks` never merges small sections, so the
   regulation corpus becomes 2,201 small fragments with little surrounding context. Clause-level
   citation is kept anyway: every chunk, whatever the strategy, is located back in its source
   document and tagged with its heading path and the paragraph number it starts in.
2. **Chunk size is not monotonic.** Regulations: 400 -> 0.845, 800 -> 0.900, 1600 -> 0.757
   (dilution: a 1600-char window mixes several paragraphs, so the vector no longer points at one rule).
   Policies tie at 800 and 1600; 1600 was chosen (fewer chunks, and more surrounding context for
   gap analysis, where a clause split across two chunks can look "missing").
3. **Hybrid loses to dense again, and the per-kind table shows why.** On regulations, BM25 alone is
   far weaker (nDCG@10 0.609). Hybrid helps the identifier queries (MRR 0.806 vs 0.736 dense: exact
   tokens like `CKYCR`, `SMA-2`) but damages paraphrase queries (0.683 vs 0.944). Net: dense wins.
   Same shape as Lab 3's Q44 / Q41 trade-off.
4. **Enforcement retrieval is saturated.** 13 one-page releases and unambiguous charge descriptions
   leave no headroom (several configs at MRR 1.000), so these numbers do not discriminate. The hard
   part of `find_enforcement_precedent` is deciding when there is *no* comparable precedent - that is
   a threshold/refusal problem for Stage 4, not a ranking problem.
5. **Whole-document embedding is worse for press releases** (MRR 0.931 vs 1.000). About 80% of each
   release is identical boilerplate, which drowns the one sentence that names the actual charge.
   The heading-path prefix also matters here (0.995 vs 0.970 without it): for press releases the
   prefix is the title, which puts the company name into every chunk.

## Known weak spots (inputs to Stage 2 / Lab 5 diagnosis)

- R02 "CKYCR upload deadline" (MRR 0.17) and R18 "SMA-2 overdue days" (MRR 0.25): exact identifiers,
  the case dense retrieval is known to be weakest on. The SMA table lives in the stressed-assets
  directions; dense retrieval prefers the asset-classification directions' prose about SMA dates.
- One labelling error was found by inspecting misses and corrected: R09's top hit was a second,
  equally valid paragraph ("no capitalization of the penal charges", different spelling) that the
  original label omitted. Correcting it moved regulation nDCG@10 from 0.882 to 0.900. The query sets
  are small (24 / 18 / 17), so differences under about 0.02 are within noise.

## Conversion limitations

- Web-page printouts (Manappuram, Mahindra) needed per-document trim rules; one Mahindra intro line
  is still mis-tagged as a heading.
- KYC paragraph 63's heading wraps in a way that splits it ("Registry (CKYCR)" becomes its own heading).
- Reranking (Lab 3 Part C) was not run: the cross-encoder needs PyTorch (not installed), and the LLM
  reranker adds a per-query model cost. Revisit only if Stage 2 diagnosis shows ranking failures.

## Correction made during Stage 5 (30 Sep 2026)

The manifest title for `reg-governance` was corrupt: the title regex matched from the first
"Reserve Bank of India (" all the way across the table of contents to the first
") Directions, 2025", producing a 4,218-character "title". It showed up as unreadable source labels
in the Stage 5 calendar. The title is fixed, the document re-converted, and a regression test
(`test_regulation_titles_are_clean`) now checks every regulation title.

Re-running this sweep afterwards: `sliding@800` is unchanged at nDCG@10 0.900, and `fixed@800`
rose from 0.880 to 0.901 (a Governance query improved once the junk title was gone), so the
automatic winner flips to `fixed@800` by 0.001. That is far inside the noise level noted above
(differences under about 0.02 on 24 queries), `sliding@800` keeps the higher MRR (0.872 vs 0.868),
and Stages 2-4 are built and measured on it, so **`sliding@800` remains the default**.
Stages 3 and 4 re-evaluated with identical results; Stage 2 accuracy is identical (see its findings).
