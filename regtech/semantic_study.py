"""Stage 7 (Lab 7 B1): at what similarity does a semantic cache start answering the wrong question?

40 labelled pairs (data/eval/semantic_pairs.jsonl): 20 paraphrases that deserve the same answer and 20
near-misses that differ in the one word that changes the answer (penal *interest* vs penal *charges*,
high- vs low-risk KYC, microfinance vs other loans). Both questions are embedded exactly as the service's
SemanticCache embeds them (aip.embed, query side), and every threshold is scored on:

- paraphrase hit rate: share of same-answer pairs that would be served from the cache (the benefit)
- wrong-hit rate: share of near-miss pairs that would be served the OTHER question's answer (the harm)
"""
from __future__ import annotations

import json

import numpy as np

from aip.embed import embed_batch

from regtech.paths import EVAL_DIR, REPORTS_DIR


def load_pairs() -> list[dict]:
    return [json.loads(x) for x in (EVAL_DIR / "semantic_pairs.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def similarities(pairs: list[dict]) -> list[float]:
    a = embed_batch([p["a"] for p in pairs], input_type="query")
    b = embed_batch([p["b"] for p in pairs], input_type="query")
    return [float(x) for x in np.sum(a * b, axis=1)]     # vectors are L2-normalised by aip.embed


def sweep(pairs: list[dict], sims: list[float], thresholds: list[float]) -> list[dict]:
    same = [s for p, s in zip(pairs, sims) if p["same"]]
    diff = [s for p, s in zip(pairs, sims) if not p["same"]]
    return [{"threshold": t,
             "paraphrase_hit_rate": sum(s >= t for s in same) / len(same),
             "wrong_hit_rate": sum(s >= t for s in diff) / len(diff)} for t in thresholds]


def safe_threshold(pairs: list[dict], sims: list[float], margin: float = 0.005) -> float:
    """Just above the most similar near-miss pair: the lowest threshold with zero wrong hits on this set."""
    return round(max(s for p, s in zip(pairs, sims) if not p["same"]) + margin, 3)


def run() -> dict:
    pairs = load_pairs()
    sims = similarities(pairs)
    thresholds = [round(0.80 + 0.01 * i, 2) for i in range(20)]
    rows = sweep(pairs, sims, thresholds)
    safe = safe_threshold(pairs, sims)
    at_safe = sweep(pairs, sims, [safe])[0]
    ranked = sorted(({**p, "similarity": round(s, 4)} for p, s in zip(pairs, sims)), key=lambda r: -r["similarity"])
    out = {"pairs": len(pairs), "safe_threshold": safe, "at_safe": at_safe, "sweep": rows, "ranked": ranked}
    (REPORTS_DIR / "stage7_semantic_threshold.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    lines = ["# Stage 7: semantic cache threshold (Lab 7 B1)", "",
             "40 labelled pairs (`data/eval/semantic_pairs.jsonl`): 20 paraphrases that deserve the same answer, 20 "
             "near-misses that differ in the word that changes the answer. Cosine similarity of the query embeddings "
             "(`aip.embed`, the vectors `aip.response_cache.SemanticCache` uses).", "",
             "| Threshold | Paraphrases served from cache | Near-misses served the WRONG answer |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['threshold']:.2f} | {r['paraphrase_hit_rate']:.2f} | {r['wrong_hit_rate']:.2f} |")
    lines += ["", f"**Lowest threshold with no wrong hit on this set: {safe}**, where paraphrase hit rate is "
              f"{at_safe['paraphrase_hit_rate']:.2f}.", "", "## Most similar pairs", "",
              "| Pair | Same answer? | Similarity | A | B |", "|---|---|---|---|---|"]
    for r in ranked[:15]:
        lines.append(f"| {r['id']} | {'yes' if r['same'] else '**no**'} | {r['similarity']:.4f} | {r['a']} | {r['b']} |")
    (REPORTS_DIR / "stage7_semantic_threshold.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
