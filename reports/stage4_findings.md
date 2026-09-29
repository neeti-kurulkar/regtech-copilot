# Stage 4 - Findings: find_enforcement_precedent

Generated report: `stage4_precedent.md`. Case table: `data/processed/enforcement_cases.json`.

## What the tool does

`find_enforcement_precedent(risk | gap)` answers "has the RBI penalised this kind of failure?"

1. **Case table (Lab 1).** Each of the 13 penalty releases was extracted once into a validated
   `CaseRecord`: company, press-release and order dates, fine, legal provision, directions breached,
   the sustained charges, and categories. Charges, directions and the amount must be verbatim in
   the release, and the release date must match the manifest; all of it is checked inside the
   schema, so `aip.llm.structured` repairs a wrong field. The rupee amount is parsed by code from
   the verbatim text. 13/13 records on the first run, $0.05; fines total Rs 72 lakh.
2. **Candidates:** the Stage 1 penalty index retrieves the 6 closest cases for the risk.
3. **Relevance verdicts:** one structured call gives a verdict for *every* candidate (same_failure /
   related / unrelated), quoting the matching charge verbatim. Only same_failure counts as a
   precedent. Otherwise the answer is an explicit "no comparable precedent", with related cases
   listed separately and never presented as proof.
4. **Chaining:** accepts a `check_policy_gap` finding as input (`gap ... --precedents`).

## Results (32 cases)

| | Baseline: top search hit | Tool |
|---|---:|---:|
| Right precedent first (19 risks with precedents) | 0.95 | **1.00** |
| Recall / precision of precedents | - | **1.00 / 1.00** |
| "No precedent" when there is none (13 risks) | 0.00 | **1.00** |
| **Claimed a precedent that does not exist** | **1.00** | **0.00** |
| Cost per query | - | $0.004 |

The baseline is what Stage 1's retrieval alone would claim. Search is excellent at ranking
(0.95 to 1.00) but has no notion of "none of these": it invents a precedent for every risk the
RBI has not penalised. The relevance judgement is what makes the tool safe to use as evidence.

## The chain from Stage 3

`python -m regtech gap "IIFL" "gold loan auctions" --precedents`: of the six auction-related
gaps in IIFL's Code, only the missing surplus-refund commitment is matched to a precedent, IIFL's
own Rs 3.10 lakh penalty of 15 May 2026. The other five are answered "no comparable precedent",
with that penalty listed as *related* (same area, different obligation).

## Fine distinctions it gets right

- Never risk-categorising customers (Shri Ram, Ola) versus not *reviewing* categories every six
  months (Fusion, Muthoot): different obligations, kept apart.
- Near misses stay "related": auctions not advertised versus surplus not refunded; microfinance
  rates above a board cap versus having no pricing policy at all.
- Distant paraphrases still match: "board seats reshuffled without the regulator's nod" leads to
  "change in more than 30 per cent of directors without prior RBI permission".

## Limits (honest)

- **The test set is small and I wrote it** knowing the 13 charges. The positives are saturated,
  as Stage 1 predicted for this corpus. The meaningful evidence is the negatives and the boundary
  cases, where the baseline fails completely.
- **13 penalties is a thin record.** "No comparable precedent" means none *among these 13*, not
  that the RBI has never penalised it. The tool's message says so explicitly.
- Releases often cite pre-November-2025 rulebooks, so precedents match by the *kind* of failure,
  not by paragraph number.
