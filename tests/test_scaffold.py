import importlib

import pytest

from regtech.check import AIP_MODULES
from regtech.manifest import COLUMNS, ManifestError, ManifestRow, init_manifest, load_manifest, write_manifest
from regtech.paths import CORPUS_DIRS, DOC_TYPES


@pytest.mark.parametrize("name", AIP_MODULES)
def test_aip_module_imports(name):
    importlib.import_module(f"aip.{name}")


def test_corpus_dirs_cover_every_doc_type():
    assert set(CORPUS_DIRS) == set(DOC_TYPES)
    for path in CORPUS_DIRS.values():
        assert path.is_dir(), path


def _row(**overrides):
    base = dict(doc_id="fpc-iifl", doc_type="internal_policy", title="IIFL Finance Limited - Fair Practices Code",
                entity_name="IIFL Finance Limited",
                publish_date="2026-04-29", source_url="", file_name="iifl.pdf")
    base.update(overrides)
    return base


def test_valid_row_and_blank_optionals():
    row = ManifestRow(**_row())
    assert row.source_url is None
    assert row.publish_date.isoformat() == "2026-04-29"
    assert ManifestRow(**_row(publish_date="")).publish_date is None


@pytest.mark.parametrize("bad", [
    dict(doc_id="FPC IIFL"),
    dict(doc_id="reg-iifl"),
    dict(doc_type="bank_policy"),
    dict(publish_date="29/04/2026"),
    dict(source_url="www.iifl.com"),
    dict(entity_name="  "),
])
def test_invalid_rows_rejected(bad):
    with pytest.raises(ValueError):
        ManifestRow(**_row(**bad))


def test_roundtrip_and_duplicate_detection(tmp_path):
    path = tmp_path / "manifest.csv"
    assert init_manifest(path) is True
    assert init_manifest(path) is False
    assert load_manifest(path) == []

    rows = [ManifestRow(**_row()), ManifestRow(**_row(doc_id="enf-iifl-2026-02-13", doc_type="enforcement"))]
    write_manifest(rows, path)
    assert load_manifest(path) == rows

    write_manifest([rows[0], rows[0]], path)
    with pytest.raises(ManifestError, match="duplicate"):
        load_manifest(path)


def test_wrong_header_rejected(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_text(",".join(reversed(COLUMNS)) + "\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="header"):
        load_manifest(path)


def test_regulation_titles_are_clean():
    """Regression: the Stage 1 title regex once matched across the whole table of contents (reg-governance)."""
    import re

    from regtech.manifest import load_manifest
    for r in load_manifest():
        assert len(r.title) <= 160, r.doc_id
        if r.doc_type == "regulation":
            assert re.fullmatch(r"Reserve Bank of India \(.+\) Directions, 20\d\d", r.title), r.doc_id
