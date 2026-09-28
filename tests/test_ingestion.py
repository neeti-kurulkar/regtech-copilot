from regtech.index import build_chunks
from regtech.ingest import processed_path
from regtech.manifest import load_manifest
from regtech.pdf_to_md import Line, _drop_toc, _heading_level, _merge_lone_enumerators
from regtech.retrieval_eval import load_queries


def _line(text, size=12.0, bold=True, page=3):
    return Line(text=text, size=size, bold=bold, page=page, x0=72, y0=100, y1=112, page_h=842)


def test_regulation_heading_levels():
    assert _heading_level(_line("Chapter III – Responsible Lending Conduct"), "regulation", 12.0, None) == 2
    assert _heading_level(_line("A. Fair Practices Code for NBFCs"), "regulation", 12.0, None) == 3
    assert _heading_level(_line("A.1 Applications for loans and their processing"), "regulation", 12.0, None) == 4
    assert _heading_level(_line("9. NBFCs having customer interface shall adopt", bold=False), "regulation", 12.0, None) is None


def test_generic_headings_need_a_cue():
    assert _heading_level(_line("A.Background", bold=False, size=11.0), "generic", 11.0, None) == 2
    assert _heading_level(_line("The company shall display the code on its website", bold=False, size=11.0),
                          "generic", 11.0, None) is None
    long_item = "a. Act efficiently, fairly and diligently in our dealings with all our customers by:"
    assert _heading_level(_line(long_item, size=10.5), "generic", 10.5, None) is None


def test_press_releases_never_get_headings():
    assert _heading_level(_line("RBI imposes monetary penalty on X Limited"), "enforcement", 12.0, None) is None


def test_lone_enumerator_merges_with_title():
    merged = _merge_lone_enumerators([_line("A."), _line("Short Title and Commencement")])
    assert [l.text for l in merged] == ["A. Short Title and Commencement"]


def test_table_of_contents_is_removed():
    lines = [_line("Table of Contents", page=0)] + [
        _line(f"Chapter {r} – Something ........................ {i}", page=0) for i, r in enumerate(["I", "II", "III"], 3)
    ] + [_line("Chapter I – Preliminary", page=2)]
    assert [l.text for l in _drop_toc(lines)] == ["Chapter I – Preliminary"]


def test_every_manifest_doc_is_converted_with_a_title():
    for row in load_manifest():
        text = processed_path(row).read_text(encoding="utf-8")
        assert text.startswith(f"# {row.title}\n"), row.doc_id
        assert len(text) > 1000, row.doc_id


def test_chunk_metadata_carries_entity():
    chunks = build_chunks("regulation", "markdown", 800)
    rbc = next(c for c in chunks if c.doc_id == "reg-rbc" and "within 21 days" in c.text)
    assert rbc.meta["entity"] == "Reserve Bank of India"
    assert rbc.meta["heading"].startswith("Reserve Bank of India")


def test_sliding_regulation_chunks_carry_heading_path_and_paragraph_for_citation():
    chunks = build_chunks("regulation", "sliding", 800)
    hit = next(c for c in chunks if c.doc_id == "reg-rbc" and "within 21 days from the date of receipt" in c.text)
    assert "Chapter III" in hit.meta["heading"] and "Fair Practices Code" in hit.meta["heading"]
    assert hit.meta["number"] in {"17", "18", "19"}


def test_policy_chunks_do_not_get_paragraph_numbers():
    chunks = build_chunks("internal_policy", "sliding", 1600)
    assert all(c.meta.get("number", "") == "" for c in chunks)


def test_query_sets_validate():
    for corpus in ("regulation", "internal_policy", "enforcement"):
        cases = load_queries(corpus)
        assert cases and all(c.meta["kind"] and c.expected for c in cases)
