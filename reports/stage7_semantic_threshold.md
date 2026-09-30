# Stage 7: semantic cache threshold (Lab 7 B1)

40 labelled pairs (`data/eval/semantic_pairs.jsonl`): 20 paraphrases that deserve the same answer, 20 near-misses that differ in the word that changes the answer. Cosine similarity of the query embeddings (`aip.embed`, the vectors `aip.response_cache.SemanticCache` uses).

| Threshold | Paraphrases served from cache | Near-misses served the WRONG answer |
|---|---|---|
| 0.80 | 0.90 | 0.85 |
| 0.81 | 0.90 | 0.80 |
| 0.82 | 0.90 | 0.80 |
| 0.83 | 0.80 | 0.70 |
| 0.84 | 0.75 | 0.60 |
| 0.85 | 0.65 | 0.60 |
| 0.86 | 0.65 | 0.60 |
| 0.87 | 0.65 | 0.50 |
| 0.88 | 0.55 | 0.50 |
| 0.89 | 0.50 | 0.40 |
| 0.90 | 0.50 | 0.30 |
| 0.91 | 0.45 | 0.30 |
| 0.92 | 0.45 | 0.25 |
| 0.93 | 0.40 | 0.25 |
| 0.94 | 0.35 | 0.25 |
| 0.95 | 0.35 | 0.15 |
| 0.96 | 0.25 | 0.10 |
| 0.97 | 0.25 | 0.05 |
| 0.98 | 0.00 | 0.05 |
| 0.99 | 0.00 | 0.00 |

**Lowest threshold with no wrong hit on this set: 0.988**, where paraphrase hit rate is 0.00.

## Most similar pairs

| Pair | Same answer? | Similarity | A | B |
|---|---|---|---|---|
| D06 | **no** | 0.9825 | What is the minimum validity period of the KFS for loans with a tenor of seven days or more? | What is the minimum validity period of the KFS for loans with a tenor of less than seven days? |
| S04 | yes | 0.9770 | What are the permitted calling hours for recovery agents for microfinance loans? | Between what hours can recovery agents call microfinance borrowers? |
| S05 | yes | 0.9769 | Can an NBFC charge penal interest on loans? | Is an NBFC allowed to levy penal interest? |
| S12 | yes | 0.9736 | What must an NBFC do when a fraud is detected? | What are an NBFC's obligations upon detecting fraud? |
| S13 | yes | 0.9728 | What is the minimum validity period of the KFS? | For how long must the KFS remain valid? |
| S09 | yes | 0.9701 | Who should head the compliance function in an NBFC? | Who leads the compliance function at an NBFC? |
| D04 | **no** | 0.9700 | Can an NBFC charge penal interest on loans? | Can an NBFC charge penal charges on loans? |
| S03 | yes | 0.9541 | How often must KYC be updated for high-risk customers? | What is the periodic KYC updation interval for high risk customers? |
| D05 | **no** | 0.9526 | Can NBFCs charge foreclosure charges on floating rate loans to individuals? | Can NBFCs charge foreclosure charges on fixed rate loans to individuals? |
| S17 | yes | 0.9522 | What details of recovery agents must be displayed on the NBFC website? | What recovery agent information has to be published online by the NBFC? |
| D03 | **no** | 0.9446 | What are the permitted calling hours for recovery agents for microfinance loans? | What are the permitted calling hours for recovery agents for non-microfinance loans? |
| D20 | **no** | 0.9403 | What is the minimum Net Owned Fund for an NBFC? | What is the minimum Net Owned Fund for an NBFC-MFI? |
| S15 | yes | 0.9303 | What is a Key Facts Statement? | what's a key facts statement (KFS)? |
| S16 | yes | 0.9210 | How many days after quarter end must the statement on change of directors be filed? | When is the quarterly statement on change of directors due? |
| D09 | **no** | 0.9102 | What must an NBFC report to the RBI after classifying a fraud? | What must an NBFC report to the police after classifying a fraud? |
