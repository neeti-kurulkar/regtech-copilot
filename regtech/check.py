"""Environment check: aip imports, settings, corpus layout, manifest, optional live call."""
from __future__ import annotations

import importlib
import os
import sys

from regtech.paths import CORPUS_DIRS, MANIFEST_PATH

AIP_MODULES = [
    "config", "llm", "cost", "cache", "tracing", "retry",
    "chunking", "embed", "retrieval", "rag", "evals", "guards",
]

PROFILE_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": None,
}


class _Report:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, msg: str) -> None:
        print(f"  [ OK ] {msg}")

    def fail(self, msg: str) -> None:
        self.failures += 1
        print(f"  [FAIL] {msg}")

    def info(self, msg: str) -> None:
        print(f"         {msg}")


def _check_python(r: _Report) -> None:
    v = sys.version_info
    if (3, 11) <= (v.major, v.minor) < (3, 15):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro} ({sys.executable})")
    else:
        r.fail(f"Python {v.major}.{v.minor} is outside the supported range 3.11-3.14")


def _check_aip_imports(r: _Report) -> bool:
    all_ok = True
    for name in AIP_MODULES:
        try:
            importlib.import_module(f"aip.{name}")
        except Exception as e:  # report every broken module, not just the first
            all_ok = False
            r.fail(f"import aip.{name}: {type(e).__name__}: {e}")
    if all_ok:
        r.ok(f"imported all {len(AIP_MODULES)} aip modules: {', '.join(AIP_MODULES)}")
    return all_ok


def _check_settings(r: _Report) -> None:
    from aip.config import settings

    r.ok(f"profile={settings.profile} offline={settings.offline} cache={settings.cache_enabled} "
         f"budget=${settings.budget_usd:.2f}")
    for tier, model in settings.models.items():
        r.info(f"{tier:<5} -> {model}")
    key = PROFILE_KEYS.get(settings.profile)
    if key is None:
        r.ok(f"profile {settings.profile!r} needs no API key")
    elif os.getenv(key):
        r.ok(f"{key} is set")
    else:
        r.fail(f"{key} is not set (put it in .env at the repo root)")


def _check_chunking_offline(r: _Report) -> None:
    from aip.chunking import markdown_chunks

    sample = "# Chapter I\n## 1. Short title\nThese Directions shall be called ...\n## 2. Applicability\nApplies to all NBFCs."
    chunks = markdown_chunks(sample, doc_id="smoke")
    if chunks and all(c.doc_id == "smoke" for c in chunks):
        r.ok(f"aip.chunking.markdown_chunks works offline ({len(chunks)} chunks from a sample)")
    else:
        r.fail("aip.chunking.markdown_chunks returned nothing for the sample")


def _check_corpus_dirs(r: _Report) -> None:
    for doc_type, path in CORPUS_DIRS.items():
        if path.is_dir():
            n = sum(1 for p in path.iterdir() if p.is_file() and p.name != ".gitkeep")
            r.ok(f"{doc_type:<15} {path.relative_to(path.parents[2])}  ({n} files)")
        else:
            r.fail(f"missing corpus directory {path}")


def _check_manifest(r: _Report) -> None:
    from regtech.manifest import ManifestError, load_manifest

    if not MANIFEST_PATH.exists():
        r.fail(f"missing {MANIFEST_PATH}")
        return
    try:
        rows = load_manifest(check_files=True)
    except ManifestError as e:
        r.fail(str(e))
        return
    r.ok(f"manifest.csv valid ({len(rows)} rows)")


def _live_smoke(r: _Report) -> None:
    from aip import Budget, chat, embed

    with Budget(limit_usd=0.01, label="stage0-smoke") as b:
        try:
            reply = chat("Reply with exactly the word: OK", tier="SMALL", max_tokens=16)
            r.ok(f"chat(tier=SMALL) replied {reply.strip()[:40]!r}")
        except Exception as e:
            r.fail(f"chat(tier=SMALL): {type(e).__name__}: {e}")
        try:
            vec = embed("KYC risk categorisation of customers")
            r.ok(f"embed() returned a {vec.shape[0]}-dim vector")
        except Exception as e:
            r.fail(f"embed(): {type(e).__name__}: {e}")
    r.info(b.report())


def run(live: bool = False) -> int:
    r = _Report()
    print("Python")
    _check_python(r)
    print("aip library")
    if _check_aip_imports(r):
        _check_settings(r)
        _check_chunking_offline(r)
    print("Corpus layout")
    _check_corpus_dirs(r)
    _check_manifest(r)
    if live:
        print("Live model call (costs a fraction of a cent; free on re-run via cache)")
        if r.failures:
            r.info("skipped: fix the failures above first")
        else:
            _live_smoke(r)
    print()
    print("ALL CHECKS PASSED" if r.failures == 0 else f"{r.failures} CHECK(S) FAILED")
    return 0 if r.failures == 0 else 1
