"""Guard layer for the ETF scanner's universe resolver.

A resolver that derives its universe from the engines is only useful if
the derivation is checked. Two guards live in ``scanner_universe`` and
this module proves both actually fire rather than merely existing:

1. the explanation invariant — no scanned ticker may differ from its
   engine ticker without a recorded, traceable reason;
2. manifest reconciliation — the derived set must match the committed
   snapshot, so a sleeve gaining or losing a member fails the build
   instead of silently changing the page.

The composition test re-derives the expected counts from the engine
modules directly rather than from the resolver, so it is an independent
check and not a restatement of the code under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scanner_universe as su  # noqa: E402

NAMES_PATH = ROOT / "data" / "etf_names.json"


@pytest.fixture(scope="module")
def rows() -> list[su.ScannerRow]:
    return su.resolve_universe()


# =========================================================================
# Composition, derived independently of the resolver
# =========================================================================
def test_composition_matches_the_engines(rows):
    """Counts come from the engines, not from the resolver's own output."""
    import run_asset_class_rotation as sleeve_b
    import run_thematic_rotation as sleeve_c
    from etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS

    expected = {
        su.SLEEVE_A: len(UNIVERSE_ETFS),
        su.SLEEVE_B: len(sleeve_b.TICKERS),
        su.SLEEVE_C: len(sleeve_c.TICKERS),
        su.SLEEVE_D: len(UNIVERSE_EUROPE_SECTORS),
        su.SLEEVE_OV: 1,
    }
    actual = {
        sleeve: sum(1 for r in rows if sleeve in r.sleeves)
        for sleeve in su.SLEEVE_ORDER
    }
    assert actual == expected


def test_rows_are_deduplicated_across_sleeves(rows):
    """SPY is sleeve A's CSP1 proxy and a native sleeve B member: one row,
    two sleeves, two origins."""
    scan_tickers = [r.scan_ticker for r in rows]
    assert len(scan_tickers) == len(set(scan_tickers))

    spy = next(r for r in rows if r.scan_ticker == "SPY")
    assert spy.sleeves == (su.SLEEVE_A, su.SLEEVE_B)
    assert {o.engine_ticker for o in spy.origins} == {"CSP1", "SPY"}


def test_sleeve_b_excludes_the_cash_proxy(rows):
    """SHY is in CASH_ONLY_TICKERS, not TICKERS, so it must never appear —
    technical signals on a cash proxy are meaningless (spec §2)."""
    assert not any(r.scan_ticker == "SHY" for r in rows)


def test_registry_proxies_are_applied(rows):
    """Sleeve A scans the traded proxies, and SOXX — which has no proxy
    field — scans as itself."""
    by_ticker = {r.scan_ticker: r for r in rows}
    for scan, engine in [
        ("SPY", "CSP1"), ("QQQ", "CNDX"), ("IJR", "IDP6"),
        ("XLE", "IUES"), ("XLRE", "IUSP"), ("EXV1.DE", "EXV1"),
    ]:
        row = by_ticker[scan]
        assert engine in {o.engine_ticker for o in row.origins}
        assert row.is_proxied

    soxx = by_ticker["SOXX"]
    assert {o.engine_ticker for o in soxx.origins} == {"SOXX"}
    assert not soxx.is_proxied


def test_the_crypto_substitution_is_declared_and_traceable(rows):
    """IBIT is scanned because it is what is traded; the reason must cite
    where that is established in the engine."""
    ibit = next(r for r in rows if r.scan_ticker == "IBIT")
    origin = next(o for o in ibit.origins if o.sleeve == su.SLEEVE_C)
    assert origin.engine_ticker == "BTC-USD"
    assert origin.reason and "run_thematic_rotation.py" in origin.reason
    assert su.DECLARED_SUBSTITUTIONS["BTC-USD"][0] == "IBIT"


# =========================================================================
# FX and naming completeness
# =========================================================================
def test_every_non_usd_row_has_an_fx_rule(rows):
    for row in rows:
        if row.currency == "USD":
            assert row.fx_ticker is None and row.fx_direction is None
        else:
            assert row.fx_ticker, f"{row.scan_ticker}: {row.currency} with no FX ticker"
            assert row.fx_direction in (su.FX_MULTIPLY, su.FX_DIVIDE)


def test_fx_directions_are_the_documented_ones(rows):
    """Getting these backwards is a silent scale error, not a crash:
    EURUSD quotes USD per EUR (multiply); USDCNY quotes CNY per USD (divide)."""
    by_ticker = {r.scan_ticker: r for r in rows}
    assert by_ticker["EXV1.DE"].fx_ticker == "EURUSD=X"
    assert by_ticker["EXV1.DE"].fx_direction == su.FX_MULTIPLY
    assert by_ticker["159801.SZ"].fx_ticker == "USDCNY=X"
    assert by_ticker["159801.SZ"].fx_direction == su.FX_DIVIDE


def test_every_row_can_be_named(rows):
    """The page must never render a blank Name. Each row carries either an
    engine label or an entry in the committed names file."""
    assert NAMES_PATH.exists(), "data/etf_names.json is not committed"
    names = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
    missing = [
        r.scan_ticker for r in rows if not r.name and not names.get(r.scan_ticker)
    ]
    assert not missing, f"no name available for: {missing}"


# =========================================================================
# Guard 1 — the explanation invariant
# =========================================================================
def test_the_live_universe_passes_the_explanation_invariant(rows):
    assert su.explanation_failures(rows) == []


def test_an_unexplained_proxy_is_rejected():
    """A future edit that hardcodes a different ticker with no reason must
    fail, which is the whole point of recording origins."""
    bad = su.ScannerRow(
        scan_ticker="XLK",
        origins=(su.Origin(su.SLEEVE_A, "IUIT", None),),
        name="Tech",
        currency="USD",
        fx_ticker=None,
        fx_direction=None,
    )
    failures = su.explanation_failures([bad])
    assert failures and "no recorded reason" in failures[0]


def test_a_reason_that_is_neither_registry_nor_declared_is_rejected():
    """A plausible-looking free-text reason is not enough: it has to be
    the registry's proxy field or an entry in DECLARED_SUBSTITUTIONS."""
    bad = su.ScannerRow(
        scan_ticker="VOO",
        origins=(su.Origin(su.SLEEVE_A, "CSP1", "looked equivalent to me"),),
        name="S&P 500",
        currency="USD",
        fx_ticker=None,
        fx_direction=None,
    )
    failures = su.explanation_failures([bad])
    assert failures and "neither a registry proxy nor a declared" in failures[0]


# =========================================================================
# Guard 2 — manifest reconciliation
# =========================================================================
def test_the_committed_manifest_reconciles(rows):
    manifest = su.load_manifest()
    assert manifest is not None, (
        "no manifest committed — run "
        "`python scripts/scanner_universe.py --write-manifest`"
    )
    assert su.reconcile(rows, manifest) == []


def test_a_missing_manifest_is_itself_a_failure(rows):
    assert su.reconcile(rows, None) == [
        "no manifest committed — run --write-manifest to establish one"
    ]


def test_an_added_ticker_is_detected(rows):
    """Simulates a sleeve gaining a member without the manifest being
    regenerated: the page must not just quietly grow a row."""
    manifest = su.fingerprint(rows)
    manifest["sleeves"][su.SLEEVE_C] = [
        t for t in manifest["sleeves"][su.SLEEVE_C] if t != "URA"
    ]
    manifest["row_count"] -= 1
    diffs = su.reconcile(rows, manifest)
    assert any("URA added since the manifest" in d for d in diffs)
    assert any("row count" in d for d in diffs)


def test_a_removed_ticker_is_detected(rows):
    manifest = su.fingerprint(rows)
    manifest["sleeves"][su.SLEEVE_B].append("HYG")
    diffs = su.reconcile(rows, manifest)
    assert any("HYG removed since the manifest" in d for d in diffs)


def test_a_changed_substitution_is_detected(rows):
    manifest = su.fingerprint(rows)
    manifest["substitutions"] = {"BTC-USD": "GBTC"}
    assert any("substitutions" in d for d in su.reconcile(rows, manifest))


def test_a_changed_fx_mapping_is_detected(rows):
    manifest = su.fingerprint(rows)
    manifest["fx"]["EXV1.DE"] = "GBPUSD=X"
    assert any("FX mapping" in d for d in su.reconcile(rows, manifest))


def test_assert_reconciled_raises_and_names_the_problem(monkeypatch, rows):
    """run_scanner calls this on every build, so it has to raise rather
    than warn — and the message has to say what moved."""
    stale = su.fingerprint(rows)
    stale["sleeves"][su.SLEEVE_D] = [
        t for t in stale["sleeves"][su.SLEEVE_D] if t != "EXH9.DE"
    ]
    stale["row_count"] -= 1
    monkeypatch.setattr(su, "load_manifest", lambda *a, **k: stale)

    with pytest.raises(su.ScannerUniverseDrift) as excinfo:
        su.assert_reconciled()
    assert "EXH9.DE added since the manifest" in str(excinfo.value)


def test_assert_reconciled_returns_the_rows_when_clean(rows):
    resolved = su.assert_reconciled()
    assert [r.scan_ticker for r in resolved] == [r.scan_ticker for r in rows]


def test_manifest_on_disk_is_not_hand_edited(rows):
    """The committed file must be exactly what the writer produces, so a
    hand-tweak to silence a drift warning shows up as a test failure."""
    committed = json.loads(su.MANIFEST_PATH.read_text(encoding="utf-8"))
    derived = su.fingerprint(rows)
    assert {k: v for k, v in committed.items() if k != "_comment"} == derived
    assert "_comment" in committed
