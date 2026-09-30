"""The headline table in EVALUATION_REPORT.md must be exactly what the committed artefacts say."""
from aip.evals import format_rate, wilson_interval

from regtech.headline import block, current_block


def test_wilson_and_format():
    lo, hi = wilson_interval(0, 9)
    assert lo == 0.0 and 0.29 < hi < 0.31
    assert format_rate(17, 18) == "17/18 (0.94; 95% CI 0.74-0.99)"
    assert format_rate(0, 0) == "0/0 (not measured)"


def test_report_table_matches_artefacts():
    assert current_block() is not None, "EVALUATION_REPORT.md lost its headline markers"
    assert current_block() == block(), ("EVALUATION_REPORT.md headline table is out of date with reports/: "
                                        "run `python -m regtech headline --write`")


def test_eval_split_switch(monkeypatch, tmp_path):
    import pytest
    from regtech import paths
    monkeypatch.delenv("REGTECH_EVAL_SPLIT", raising=False)
    assert paths.eval_file("gap_cases.jsonl") == paths.EVAL_DIR / "gap_cases.jsonl"
    monkeypatch.setenv("REGTECH_EVAL_SPLIT", "test")
    monkeypatch.setattr(paths, "EVAL_DIR", tmp_path)
    with pytest.raises(SystemExit, match="no held-out test set"):
        paths.eval_file("gap_cases.jsonl")
    monkeypatch.setenv("REGTECH_EVAL_SPLIT", "holdout")
    with pytest.raises(SystemExit):
        paths.eval_split()


def test_judge_sheet_corruptions_are_real():
    import random
    from regtech.judge_sheet import half_answer, unsupported_claim, wrong_number
    rng = random.Random(0)
    a = "The KFS is valid for three working days [1]. For loans under seven days it is one working day [2]."
    assert wrong_number("Refund within 7 working days [1].", rng) == "Refund within 14 working days [1]."
    assert "[1]" in wrong_number("Refund within 7 working days [1].", rng), "citation markers are never changed"
    assert wrong_number(a, rng) != a
    assert half_answer(a) == "The KFS is valid for three working days [1]."
    assert half_answer("One sentence only [1].") is None
    assert unsupported_claim(a, rng).startswith(a) and unsupported_claim(a, rng).endswith("[1].")
