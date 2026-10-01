"""Judge recalibration, v2: a blind labelling sheet that contains every label value by construction.

Why (independent review F8): the Stage 2 sheet held 20 answers from the reasoning model. The human labelled every
one faithful and used only correctness 0 or 2, so faithfulness kappa was 0 by construction and the correctness
kappa of 1.00 only showed the judge can tell right from clearly wrong. It also was not the model the service uses.

This sheet takes the answers the service actually serves (SMALL tier, replayed from the cache) and adds
deliberately corrupted copies, so unfaithful and partial answers are guaranteed to be present:

  original            the served answer, unchanged
  wrong_number        one number in the answer changed          (should read unfaithful and incorrect)
  half_answer         only the first half of the sentences kept (should read faithful but partial, correctness 1)
  unsupported_claim   a plausible, cited, but unsupported sentence appended (should read unfaithful)

Rows are shuffled and renumbered. Which row is which variant is in the key file: do NOT open it before labelling.

  python -m regtech judge-sheet                 # write the sheet (offline works: AIP_OFFLINE=1 AIP_CACHE_DIR=ci/cache)
  ... fill human_faithfulness_0_or_1 and human_correctness_0_1_2 in reports/judge_calibration_v2.csv ...
  python -m regtech judge-kappa --v2            # runs the judge (online, a few cents), prints kappa per criterion
"""
from __future__ import annotations

import csv
import json
import random
import re

from aip.evals import JUDGE_RUBRIC_CORRECTNESS, JUDGE_RUBRIC_FAITHFULNESS, judge_agreement, llm_judge
from aip.guards import split_sentences

from regtech.paths import REPORTS_DIR

SHEET = REPORTS_DIR / "judge_calibration_v2.csv"
KEY = REPORTS_DIR / "judge_calibration_v2_key.json"          # the answer key: open only after labelling
SOURCES = REPORTS_DIR / "judge_calibration_v2_sources.json"  # what the judge sees per row: the same cited sources
SCORES = REPORTS_DIR / "judge_calibration_v2_scores.json"
COLUMNS = ["id", "question", "reference", "answer", "cited_sources", "human_faithfulness_0_or_1",
           "human_correctness_0_1_2"]
MIX = {"original": 12, "wrong_number": 8, "half_answer": 8, "unsupported_claim": 8}
SEED = 20261002

_NUM = re.compile(r"(?<![\[\w.])(\d+)(?![\w\]])")
_WORDS = {"two": "three", "three": "five", "five": "seven", "seven": "ten", "ten": "fifteen", "fifteen": "thirty",
          "thirty": "sixty", "six": "nine", "four": "eight", "eight": "twelve", "one": "two"}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.I)
# Plausible compliance claims that the RBI Directions in the corpus do not make. Each is cited [1] on purpose:
# a citation index check passes them; only reading the source shows they are unsupported.
_UNSUPPORTED = [
    "The NBFC must also report each such case to the RBI's Department of Supervision within 7 days [1].",
    "This requirement applies only to NBFCs with an asset size of Rs 1,000 crore or more [1].",
    "A breach attracts a fixed penalty of Rs 5 lakh for each instance [1].",
    "The Board must review compliance with this requirement every month [1].",
    "The customer must be informed of this in writing within 48 hours [1].",
    "The Chief Compliance Officer must personally approve every exception to this rule [1].",
    "Microfinance loans are exempt from this requirement [1].",
    "The statutory auditor must certify compliance with this requirement every quarter [1].",
]


def wrong_number(text: str, rng: random.Random) -> str | None:
    digits = [m for m in _NUM.finditer(text)]
    if digits:
        m = rng.choice(digits)
        n = int(m.group(1))
        new = n * 2 if n < 10 else n + max(1, n // 2)
        return text[:m.start(1)] + str(new) + text[m.end(1):]
    words = list(_WORD_RE.finditer(text))
    if words:
        m = rng.choice(words)
        repl = _WORDS[m.group(1).lower()]
        repl = repl.capitalize() if m.group(1)[0].isupper() else repl
        return text[:m.start()] + repl + text[m.end():]
    return None


def half_answer(text: str) -> str | None:
    sents = split_sentences(text)
    if len(sents) < 2:
        return None
    keep = sents[: (len(sents) + 1) // 2]
    return " ".join(keep)


def unsupported_claim(text: str, rng: random.Random) -> str:
    return f"{text.rstrip()} {rng.choice(_UNSUPPORTED)}"


def served_answers() -> list[tuple[object, dict]]:
    """The service's Q&A (SMALL tier) on every answerable and partial question that it did not refuse."""
    from regtech.qa import RegulationQA
    from regtech.qa_eval import _as_output, load_cases
    from regtech.service import QA_TIER

    qa = RegulationQA(tier=QA_TIER)
    out = []
    for c in load_cases():
        if c.meta["kind"] == "unanswerable":
            continue
        o = _as_output(qa.ask(c.input))
        if not o["refused"]:
            out.append((c, o))
    return out


def build(force: bool = False) -> dict:
    if SHEET.exists() and not force:
        with SHEET.open(newline="", encoding="utf-8") as f:
            if any(r["human_faithfulness_0_or_1"].strip() or r["human_correctness_0_1_2"].strip() for r in csv.DictReader(f)):
                raise SystemExit(f"{SHEET.name} already has human labels; not overwriting (use --force to discard them)")
    rng = random.Random(SEED)
    answers = served_answers()
    rng.shuffle(answers)
    make = {"original": lambda o: o["text"], "wrong_number": lambda o: wrong_number(o["text"], rng),
            "half_answer": lambda o: half_answer(o["text"]), "unsupported_claim": lambda o: unsupported_claim(o["text"], rng)}
    rows, used, taken = [], {k: 0 for k in MIX}, set()
    # most constrained variant first (half_answer needs 2+ sentences); prefer questions not used yet, so the
    # labeller rarely sees two versions of one answer, and reuse a question only when the pool runs out
    for variant in sorted(MIX, key=lambda v: v != "half_answer"):
        for prefer_fresh in (True, False):
            for c, o in answers:
                if used[variant] >= MIX[variant] or (prefer_fresh and c.id in taken):
                    continue
                if any(r[0].id == c.id and r[2] == variant for r in rows):
                    continue
                text = make[variant](o)
                if text:
                    rows.append((c, o, variant, text))
                    used[variant] += 1
                    taken.add(c.id)
    rng.shuffle(rows)
    key, sources = {}, {}
    with SHEET.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for i, (c, o, variant, text) in enumerate(rows, 1):
            rid = f"J{i:02d}"
            # The human and the judge must grade against the SAME evidence: the full text of the cited sources.
            # (The v2 sheet first cut each source at 700 characters while the judge saw everything, so a claim
            # supported past the cut looked unsupported to the human only.)
            cited = "\n---\n".join(f"[{s['n']}] {s['label']}\n{s['text']}" for s in o["sources"] if s["n"] in o["cited"])
            w.writerow([rid, c.input, c.expected["reference"], text, cited, "", ""])
            key[rid] = {"question_id": c.id, "variant": variant}
            sources[rid] = cited
    KEY.write_text(json.dumps(key, indent=2), encoding="utf-8")
    SOURCES.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"rows": len(rows), **{k: v for k, v in used.items()}}


def kappa() -> dict:
    """Run the judge on every labelled row (online) and compare with the human labels."""
    with SHEET.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    missing = [r["id"] for r in rows if not (r["human_faithfulness_0_or_1"].strip() and r["human_correctness_0_1_2"].strip())]
    if missing:
        raise SystemExit(f"label every row first; unlabelled: {', '.join(missing)}")
    key = json.loads(KEY.read_text(encoding="utf-8"))
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    scores = {}
    for r in rows:
        f = llm_judge(JUDGE_RUBRIC_FAITHFULNESS.format(context=sources[r["id"]], answer=r["answer"]))
        c = llm_judge(JUDGE_RUBRIC_CORRECTNESS.format(question=r["question"], reference=r["reference"],
                                                      candidate=r["answer"]))
        scores[r["id"]] = {"faithfulness": None if f.get("parse_error") else float(f.get("score", 0)),
                           "correctness_0_1_2": None if c.get("parse_error") else int(c.get("score", 0))}
    out: dict = {"n": len(rows)}
    for crit, col, k in (("faithfulness", "human_faithfulness_0_or_1", "faithfulness"),
                         ("correctness", "human_correctness_0_1_2", "correctness_0_1_2")):
        pairs = [(scores[r["id"]][k], float(r[col])) for r in rows if scores[r["id"]][k] is not None]
        out[crit] = judge_agreement([float(a) for a, _ in pairs], [b for _, b in pairs])
    # Does each rater catch the planted defects? (a rate per variant, human and judge side by side)
    by_variant: dict = {}
    for r in rows:
        v = key[r["id"]]["variant"]
        d = by_variant.setdefault(v, {"n": 0, "human_unfaithful": 0, "judge_unfaithful": 0,
                                      "human_correctness": [], "judge_correctness": []})
        d["n"] += 1
        d["human_unfaithful"] += int(float(r["human_faithfulness_0_or_1"]) == 0)
        d["judge_unfaithful"] += int(scores[r["id"]]["faithfulness"] == 0)
        d["human_correctness"].append(float(r["human_correctness_0_1_2"]))
        d["judge_correctness"].append(scores[r["id"]]["correctness_0_1_2"])
    out["by_variant"] = by_variant
    SCORES.write_text(json.dumps({"scores": scores, "summary": out}, indent=2, default=str), encoding="utf-8")
    return out
