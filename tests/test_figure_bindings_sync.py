"""The build rewrites the prose fallbacks; these pin how (2026-09-02).

WHY IT EXISTS. `<span data-fig="...">` literals are what a reader sees with no
JavaScript, and they were maintained BY HAND. Every refresh that moved a bound
figure failed test_figure_bindings, which failed pytest, which made
refresh_all report a failed step, which made scheduled_refresh refuse to
commit. An unattended run could rebuild every panel, pass every integrity
guard, and push nothing. The scheduled pair last pushed on 2026-08-01, and
this was one of the two reasons.

WHAT THE SYNC MUST NOT BECOME. Rewriting a literal from data means a wrong
datum reaches the prose without anyone typing it — the human who used to
retype the number was also, incidentally, reading it. So the sync is loud, it
refuses formats it does not know rather than inventing one, and it leaves
alone any binding the spec does not describe.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import figure_bindings as fb

SPEC = '''// __FIGURE_SPEC_START__
const FIGURE_SPEC = {"a.sharpe": {"path": "multi.a", "fmt": "sharpe"},
 "a.cagr": {"path": "multi.c", "fmt": "pct1s"}};
// __FIGURE_SPEC_END__'''


def _fixture(tmp_path: Path, lit_sharpe: str, lit_cagr: str,
             extra_span: str = "") -> tuple[Path, Path]:
    tpl = tmp_path / "template.html"
    tpl.write_text(
        f'<p>{SPEC}</p>\n'
        f'<span data-fig="a.sharpe">{lit_sharpe}</span>\n'
        f'<span data-fig="a.cagr">{lit_cagr}</span>\n{extra_span}',
        encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "multi_strategy.json").write_text(
        json.dumps({"a": 1.2649, "c": 0.15234}), encoding="utf-8")
    return tpl, data


def test_drifted_literals_are_rewritten_from_the_data(tmp_path):
    tpl, data = _fixture(tmp_path, "+1.24", "+14.9%")
    changes = fb.sync(tpl, data, verbose=False)
    assert sorted(changes) == [
        ("a.cagr", "+14.9%", "+15.2%"),
        ("a.sharpe", "+1.24", "+1.26"),
    ]
    out = tpl.read_text(encoding="utf-8")
    assert '<span data-fig="a.sharpe">+1.26</span>' in out
    assert '<span data-fig="a.cagr">+15.2%</span>' in out


def test_a_matching_template_is_left_byte_identical(tmp_path):
    """No spurious rewrite: a clean build must not dirty the working tree."""
    tpl, data = _fixture(tmp_path, "+1.26", "+15.2%")
    before = tpl.read_bytes()
    assert fb.sync(tpl, data, verbose=False) == []
    assert tpl.read_bytes() == before


def test_check_mode_reports_without_writing(tmp_path):
    tpl, data = _fixture(tmp_path, "+1.24", "+14.9%")
    before = tpl.read_bytes()
    changes = fb.sync(tpl, data, dry_run=True, verbose=False)
    assert len(changes) == 2
    assert tpl.read_bytes() == before, "--check must not write"


def test_a_binding_outside_the_spec_is_left_alone(tmp_path):
    """The spec is the contract. Blanking a figure it does not describe would
    be worse than leaving a stale one visible, so it is reported, not touched."""
    tpl, data = _fixture(tmp_path, "+1.26", "+15.2%",
                         extra_span='<span data-fig="mystery.x">99</span>')
    fb.sync(tpl, data, verbose=False)
    assert '<span data-fig="mystery.x">99</span>' in tpl.read_text(encoding="utf-8")


def test_an_unknown_format_raises_rather_than_guessing(tmp_path):
    tpl, data = _fixture(tmp_path, "+1.26", "+15.2%")
    tpl.write_text(tpl.read_text(encoding="utf-8").replace('"sharpe"', '"bananas"'),
                   encoding="utf-8")
    with pytest.raises(fb.SpecError, match="unknown fmt"):
        fb.sync(tpl, data, verbose=False)


def test_a_missing_spec_block_raises(tmp_path):
    tpl, data = _fixture(tmp_path, "+1.26", "+15.2%")
    tpl.write_text('<span data-fig="a.sharpe">+1.26</span>', encoding="utf-8")
    with pytest.raises(fb.SpecError, match="markers missing"):
        fb.sync(tpl, data, verbose=False)


def test_the_build_calls_the_sync():
    """Wiring, not behaviour: the sync is worthless if pipeline never runs it,
    and that failure would look exactly like the hand-maintained status quo."""
    import inspect
    from scripts import pipeline
    src = inspect.getsource(pipeline.main)
    assert "figure_bindings.sync()" in src, (
        "pipeline.main must sync the fallbacks before reading the template")
    assert src.index("figure_bindings.sync()") < src.index("TEMPLATE.read_text"), (
        "the sync must run BEFORE the template is read, or the build injects "
        "one dataset and ships fallbacks from another")
