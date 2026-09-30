from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CORPUS_DIR = DATA_DIR / "corpus"
MANIFEST_PATH = DATA_DIR / "manifest.csv"
PROCESSED_DIR = DATA_DIR / "processed"
EVAL_DIR = DATA_DIR / "eval"
REPORTS_DIR = REPO_ROOT / "reports"

DOC_TYPES = ("regulation", "internal_policy", "enforcement")

CORPUS_DIRS: dict[str, Path] = {
    "regulation": CORPUS_DIR / "regulations",
    "internal_policy": CORPUS_DIR / "internal_policies",
    "enforcement": CORPUS_DIR / "enforcement",
}


def eval_split() -> str:
    """Which evaluation set the eval commands read: 'dev' (default; the sets everything was tuned on) or 'test'
    (held out, under data/eval/test/, run once after the design is frozen). Set REGTECH_EVAL_SPLIT=test."""
    import os
    split = os.getenv("REGTECH_EVAL_SPLIT", "dev").strip().lower()
    if split not in ("dev", "test"):
        raise SystemExit(f"REGTECH_EVAL_SPLIT must be 'dev' or 'test', not {split!r}")
    return split


def eval_file(name: str) -> Path:
    """An evaluation set by file name, from the current split (see data/eval/README.md)."""
    split = eval_split()
    path = (EVAL_DIR if split == "dev" else EVAL_DIR / "test") / name
    if split == "test" and not path.exists():
        raise SystemExit(f"no held-out test set at {path}: write it first (data/eval/README.md)")
    return path

