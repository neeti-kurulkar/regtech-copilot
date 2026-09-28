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
