"""Tests for the daily mark-to-market overlay.

Covers the pure-logic pieces of ``scripts/mark_to_market_live.py``
that do not require yfinance:
  - ``_resolve_yf_symbol`` (ticker -> yfinance symbol + FX handling)
  - ``_build_effective_weights`` (sleeve NAV weights with EEM tilt +
    regime gate scaling)

The end-to-end pipeline (yfinance fetch + price math) is verified
manually by running the script; that path is exercised by the
GitHub Actions daily workflow on every weekday.
"""

from __future__ import annotations

import pytest

from scripts.mark_to_market_live import (
    _build_effective_weights,
    _resolve_yf_symbol,
)


# ---------------------------------------------------------------------------
# Symbol resolver
# ---------------------------------------------------------------------------

# Minimal stand-in registry — just the fields the resolver cares about.
FAKE_REGISTRY = {
    "IUES": {"yfinance_trading_proxy": "XLE"},
    "IUSP": {"yfinance_trading_proxy": "VNQ"},
    "SPY":  {"yfinance_trading_proxy": "SPY"},
    "CSP1": {"yfinance_trading_proxy": "SPY"},  # CSP1 trades via SPY
}


def test_resolver_strategy_a_uses_us_listed_proxy():
    """IUES is an Irish UCITS; the registry maps it to XLE for trading.
    The resolver should return XLE with no FX (XLE is USD)."""
    sym, fx = _resolve_yf_symbol("IUES", FAKE_REGISTRY)
    assert sym == "XLE"
    assert fx == "none"


def test_resolver_self_proxy_returns_same_symbol():
    """When trading_proxy == ticker (SPY -> SPY), resolver should
    return the original symbol without double-mapping."""
    sym, fx = _resolve_yf_symbol("SPY", FAKE_REGISTRY)
    assert sym == "SPY"
    assert fx == "none"


def test_resolver_strategy_b_passes_through_unknown():
    """Strategy B tickers (EEM, DBC, etc.) are not in the registry —
    resolver should pass them through as-is, assuming USD."""
    sym, fx = _resolve_yf_symbol("EEM", FAKE_REGISTRY)
    assert sym == "EEM"
    assert fx == "none"


def test_resolver_europe_appends_de_and_flags_fx():
    """Strategy D Stoxx 600 sector UCITS need .DE suffix and EUR/USD."""
    for ticker in ("EXV1", "EXH1", "EXV3", "EXH3", "EXH9"):
        sym, fx = _resolve_yf_symbol(ticker, FAKE_REGISTRY)
        assert sym == f"{ticker}.DE"
        assert fx == "eur_to_usd"


def test_resolver_china_a_share_flags_cny_fx():
    """159801.SZ / 588200.SS are CNY-denominated and need USDCNY=X."""
    for ticker in ("159801.SZ", "588200.SS"):
        sym, fx = _resolve_yf_symbol(ticker, FAKE_REGISTRY)
        assert sym == ticker
        assert fx == "cny_to_usd"


def test_resolver_btc_passes_through_as_usd():
    """BTC-USD is a direct yfinance USD ticker — no proxy, no FX."""
    sym, fx = _resolve_yf_symbol("BTC-USD", FAKE_REGISTRY)
    assert sym == "BTC-USD"
    assert fx == "none"


# ---------------------------------------------------------------------------
# Effective weights builder
# ---------------------------------------------------------------------------

def _fake_sleeve(date: str, holdings: list[tuple[str, float]]) -> dict:
    """Build a minimal sleeve JSON with a single trade_history entry."""
    return {
        "headline": {
            "trade_history": [
                {
                    "date": date,
                    "holdings": [{"etf": e, "weight": w} for e, w in holdings],
                }
            ]
        }
    }


def test_weights_normal_state_no_tilt_no_risk_off():
    """No EEM tilt, RISK_ON: sleeves at 35/35/10/20 with no SHY topup
    (since the test sleeves fill 100% within-sleeve)."""
    sleeves = {
        "a": _fake_sleeve("2026-05-22", [("IUES", 0.6), ("IUSP", 0.4)]),
        "b": _fake_sleeve("2026-05-22", [("SPY", 1.0)]),
        "c": _fake_sleeve("2026-05-22", [("ICLN", 1.0)]),
        "d": _fake_sleeve("2026-05-22", [("EXH1", 1.0)]),
    }
    w = _build_effective_weights(sleeves, p22_active=False, regime_state="RISK_ON")
    assert w["IUES"] == pytest.approx(0.35 * 0.6)
    assert w["IUSP"] == pytest.approx(0.35 * 0.4)
    assert w["SPY"]  == pytest.approx(0.35)
    assert w["ICLN"] == pytest.approx(0.10)
    assert w["EXH1"] == pytest.approx(0.20)
    assert sum(w.values()) == pytest.approx(1.0)
    assert "SHY" not in w  # no residual cash
    assert "EEM" not in w  # tilt inactive


def test_weights_eem_tilt_active_funds_from_b():
    """EEM tilt ON: B drops from 35% to 25%, EEM gets 10% added."""
    sleeves = {
        "a": _fake_sleeve("2026-05-22", [("IUES", 1.0)]),
        "b": _fake_sleeve("2026-05-22", [("SPY", 1.0)]),
        "c": _fake_sleeve("2026-05-22", [("ICLN", 1.0)]),
        "d": _fake_sleeve("2026-05-22", [("EXH1", 1.0)]),
    }
    w = _build_effective_weights(sleeves, p22_active=True, regime_state="RISK_ON")
    assert w["SPY"] == pytest.approx(0.25)  # B dropped 10pp
    assert w["EEM"] == pytest.approx(0.10)  # tilt
    assert w["IUES"] == pytest.approx(0.35)
    assert w["ICLN"] == pytest.approx(0.10)
    assert w["EXH1"] == pytest.approx(0.20)
    assert sum(w.values()) == pytest.approx(1.0)


def test_weights_eem_tilt_when_b_already_holds_eem_adds_correctly():
    """If B already holds EEM (4-5% NAV), adding the 10% tilt should
    stack to the existing position rather than overwrite it."""
    sleeves = {
        "a": _fake_sleeve("2026-05-22", [("IUES", 1.0)]),
        "b": _fake_sleeve("2026-05-22", [("EEM", 0.2), ("SPY", 0.8)]),
        "c": _fake_sleeve("2026-05-22", [("ICLN", 1.0)]),
        "d": _fake_sleeve("2026-05-22", [("EXH1", 1.0)]),
    }
    w = _build_effective_weights(sleeves, p22_active=True, regime_state="RISK_ON")
    # B is at 25% NAV (tilt active), so EEM-from-B = 25% * 20% = 5%
    # Plus tilt = 10%. Total EEM = 15%.
    assert w["EEM"] == pytest.approx(0.05 + 0.10)
    assert w["SPY"] == pytest.approx(0.25 * 0.8)
    assert sum(w.values()) == pytest.approx(1.0)


def test_weights_risk_off_halves_equity_and_adds_shy_residual():
    """RISK_OFF: all equity-side weights halve, 50% NAV moves to SHY."""
    sleeves = {
        "a": _fake_sleeve("2026-05-22", [("IUES", 1.0)]),
        "b": _fake_sleeve("2026-05-22", [("SPY", 1.0)]),
        "c": _fake_sleeve("2026-05-22", [("ICLN", 1.0)]),
        "d": _fake_sleeve("2026-05-22", [("EXH1", 1.0)]),
    }
    w = _build_effective_weights(sleeves, p22_active=False, regime_state="RISK_OFF")
    # Sleeves at half: 17.5 / 17.5 / 5 / 10 = 50%; SHY residual = 50%
    assert w["IUES"] == pytest.approx(0.175)
    assert w["SPY"]  == pytest.approx(0.175)
    assert w["ICLN"] == pytest.approx(0.05)
    assert w["EXH1"] == pytest.approx(0.10)
    assert w["SHY"]  == pytest.approx(0.50)
    assert sum(w.values()) == pytest.approx(1.0)


def test_weights_zero_weight_holdings_are_dropped():
    """A holding with weight 0 should not appear in the effective
    weights map (would otherwise show up as a 0% position)."""
    sleeves = {
        "a": _fake_sleeve("2026-05-22",
                           [("IUES", 0.6), ("IUSP", 0.4), ("PHANTOM", 0.0)]),
        "b": _fake_sleeve("2026-05-22", [("SPY", 1.0)]),
        "c": _fake_sleeve("2026-05-22", [("ICLN", 1.0)]),
        "d": _fake_sleeve("2026-05-22", [("EXH1", 1.0)]),
    }
    w = _build_effective_weights(sleeves, p22_active=False, regime_state="RISK_ON")
    assert "PHANTOM" not in w


def test_weights_partial_sleeve_fill_creates_shy_residual():
    """If sleeves do not sum to 100% within (cash floor scenario), the
    residual goes into SHY at the blend level."""
    # B holds only 1 ETF with 30% within-sleeve weight — 70% sits in
    # the cash floor. At sleeve weight 35%, that's a 35% * 70% = 24.5%
    # SHY contribution at the blend level.
    sleeves = {
        "a": _fake_sleeve("2026-05-22", [("IUES", 1.0)]),
        "b": _fake_sleeve("2026-05-22", [("SPY", 0.3)]),  # 70% in SHY floor
        "c": _fake_sleeve("2026-05-22", [("ICLN", 1.0)]),
        "d": _fake_sleeve("2026-05-22", [("EXH1", 1.0)]),
    }
    w = _build_effective_weights(sleeves, p22_active=False, regime_state="RISK_ON")
    assert w["SPY"] == pytest.approx(0.35 * 0.3)
    assert w["SHY"] == pytest.approx(0.35 * 0.7)
    assert sum(w.values()) == pytest.approx(1.0)
