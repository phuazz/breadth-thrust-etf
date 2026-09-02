"""Staged roster changes are inert until promoted (etf_registry, 2026-09-02).

WHY THIS EXISTS. A `ticker_overrides` or exclusion change rewrites every
historical roster the next time the fetch layer re-parses the cached iShares
files, which it does on every refresh — and the weekend refresh is armed to
push. So a verified change lands in STAGED_ROSTER_CHANGES first, where the
default parse must not see it, and is promoted into the live keys as a
deliberate, filed restatement. These tests hold both halves: the default path
ignores staging, and the opt-in path applies exactly what is staged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import etf_registry as reg  # noqa: E402
import fetch_constituents as fc  # noqa: E402

CSV = (
    'Fund Holdings as of,"05/Jan/2018"\n'
    " \n"
    "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,"
    "Shares,Price,Location,Exchange,Market Currency\n"
    '"OLD","OLD CO SA","Energy","Equity","1","1.0","1","1","1.0","France",'
    '"Nyse Euronext - Euronext Paris","EUR"\n'
    '"RGT","SOMECO RIGHTS SA","Energy","Equity","1","0.1","1","1","1.0","France",'
    '"Nyse Euronext - Euronext Paris","EUR"\n'
    '"NEW","NEW CO SE","Energy","Equity","1","2.0","1","1","1.0","France",'
    '"Nyse Euronext - Euronext Paris","EUR"\n'
    '"PLAIN","PLAIN AG","Energy","Equity","1","3.0","1","1","1.0","Germany",'
    '"Xetra","EUR"\n'
    "\n"
)

CFG = {"symbol": "TESTEU", "ticker_overrides": {}, "apply_exchange_suffix": True}
STAGED = {"TESTEU": {"ticker_overrides": {"OLD": "NEW.PA"},
                     "exclude_symbols": ["RGT.PA"]}}


@pytest.fixture
def staged(monkeypatch):
    monkeypatch.setattr(reg, "STAGED_ROSTER_CHANGES", STAGED)
    return STAGED


def _parse(cfg):
    rules = reg.roster_rules(cfg)
    return fc.parse_holdings(CSV, ticker_overrides=rules["ticker_overrides"],
                             apply_exchange_suffix=True, symbol="TESTEU",
                             exclude_symbols=rules["exclude_symbols"]), rules


def test_default_parse_ignores_staged_changes(staged, monkeypatch):
    """Deployed behaviour byte for byte: the old symbol and the rights line
    both survive, and the JSON flag says no staging was applied."""
    monkeypatch.delenv(reg.STAGED_ROSTER_ENV, raising=False)
    tickers, rules = _parse(CFG)
    assert tickers == ["OLD.PA", "RGT.PA", "NEW.PA", "PLAIN.DE"]
    assert rules["staged_applied"] is False
    assert rules["exclude_symbols"] == frozenset()


def test_opt_in_applies_override_and_exclusion(staged, monkeypatch):
    monkeypatch.setenv(reg.STAGED_ROSTER_ENV, "1")
    tickers, rules = _parse(CFG)
    # OLD maps onto NEW.PA and the parser's dedupe collapses the pair into one
    # entry — a rename week cannot double-count. The rights line is gone.
    assert tickers == ["NEW.PA", "PLAIN.DE"]
    assert rules["staged_applied"] is True
    assert "RGT.PA" in rules["exclude_symbols"]


def test_opt_in_is_a_no_op_for_an_etf_with_nothing_staged(staged, monkeypatch):
    monkeypatch.setenv(reg.STAGED_ROSTER_ENV, "1")
    tickers, rules = _parse({**CFG, "symbol": "OTHER"})
    assert tickers == ["OLD.PA", "RGT.PA", "NEW.PA", "PLAIN.DE"]
    assert rules["staged_applied"] is False


def test_live_override_wins_a_collision_with_a_staged_one(staged, monkeypatch):
    """A staged entry may add mappings; it may never redefine a deployed one."""
    monkeypatch.setenv(reg.STAGED_ROSTER_ENV, "1")
    cfg = {**CFG, "ticker_overrides": {"OLD": "OLD.PA"}}
    _, rules = _parse(cfg)
    assert rules["ticker_overrides"]["OLD"] == "OLD.PA"


def test_live_exclusions_apply_without_the_env_var(monkeypatch):
    monkeypatch.delenv(reg.STAGED_ROSTER_ENV, raising=False)
    monkeypatch.setattr(reg, "STAGED_ROSTER_CHANGES", {})
    cfg = {**CFG, "exclude_symbols": ["RGT.PA"]}
    tickers, rules = _parse(cfg)
    assert tickers == ["OLD.PA", "NEW.PA", "PLAIN.DE"]
    assert rules["staged_applied"] is False


def test_env_var_must_be_exactly_one(staged, monkeypatch):
    for value in ("true", "yes", "0", " "):
        monkeypatch.setenv(reg.STAGED_ROSTER_ENV, value)
        _, rules = _parse(CFG)
        assert rules["staged_applied"] is False, value


# ---------------------------------------------------------------------------
# The staged data as committed: well-formed, and pointing at real panels.
# ---------------------------------------------------------------------------

def test_committed_staging_is_well_formed():
    for etf, block in reg.STAGED_ROSTER_CHANGES.items():
        assert etf in reg.ETF_REGISTRY, f"{etf}: staged for an unknown panel"
        assert set(block) <= {"ticker_overrides", "exclude_symbols", "note"}, etf
        for raw, sym in (block.get("ticker_overrides") or {}).items():
            assert raw and sym and raw != sym, (etf, raw, sym)
            assert " " not in sym and "/" not in sym, (etf, raw, sym)
            live = reg.ETF_REGISTRY[etf].get("ticker_overrides") or {}
            assert raw not in live, (
                f"{etf}: {raw} is staged AND live — promote or remove, not both")
        for sym in (block.get("exclude_symbols") or []):
            assert isinstance(sym, str) and sym, (etf, sym)
        # A symbol cannot be both mapped onto and excluded within one panel.
        targets = set((block.get("ticker_overrides") or {}).values())
        assert not targets & set(block.get("exclude_symbols") or []), etf


def test_deployed_europe_panels_carry_no_live_exclusions_yet():
    """Promotion is a filed restatement. When this test fails because an
    entry was promoted, that is the moment to file the restatement and then
    update this expectation — not before."""
    for etf in reg.UNIVERSE_EUROPE_SECTORS:
        assert not reg.ETF_REGISTRY[etf].get("exclude_symbols"), etf
        assert reg.ETF_REGISTRY[etf].get("ticker_overrides") == {}, etf
