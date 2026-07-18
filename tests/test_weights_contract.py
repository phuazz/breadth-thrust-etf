"""D5 — the blend-weights contract (2026-07-04 implementation audit).

The 35/35/10/20 blend and the 10% fund-from-B tilt are restated across
several surfaces that are held in sync only by hand: the deployed key
string, ``overlay_state`` (Python), the live mark-to-market's effective
weights, and the dashboard's JS port. The audit's parity verdict was
DUAL-BUT-EQUIVALENT — "equivalent today; fragile to any future
reweight". This module is the guard: every surface is asserted against
ONE parse of the deployed key string plus the overlay's published
parameters, so a future reweight that misses a surface fails loudly here
instead of shipping a silent divergence.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from overlay_state import (  # noqa: E402
    BASE_SLEEVE_WEIGHTS,
    DEFAULT_DERISK_FRACTION,
    DEFAULT_TILT_WEIGHT,
    sleeve_nav_weights,
)


def _parse_blend_key(key: str) -> dict[str, float]:
    """'blend_35_35_10_20[...]' -> {'a': 0.35, 'b': 0.35, 'c': 0.10, 'd': 0.20}."""
    m = re.match(r"blend_(\d+)_(\d+)_(\d+)_(\d+)", key)
    assert m, f"deployed key {key!r} does not carry a 4-way weight signature"
    vals = [int(g) / 100.0 for g in m.groups()]
    assert abs(sum(vals) - 1.0) < 1e-9, f"key weights do not sum to 1: {key}"
    return dict(zip("abcd", vals))


def test_deployed_key_constants_agree_across_python_surfaces():
    """The key string named by the email and the live track parses to the
    same weights overlay_state carries."""
    from build_email_body import DEPLOYED_KEY_PREFERENCE
    from mark_to_market_live import DEPLOYED_KEY

    assert DEPLOYED_KEY == DEPLOYED_KEY_PREFERENCE[0]
    assert _parse_blend_key(DEPLOYED_KEY) == pytest.approx(BASE_SLEEVE_WEIGHTS)


def test_live_overlay_parameters_agree_with_defaults():
    """The published overlay's parameters must match the defaults every
    surface falls back to — a drift here means the fallbacks lie.
    Soft-skip on a minimal checkout."""
    p = ROOT / "data" / "risk_overlay.json"
    if not p.exists():
        pytest.skip("risk_overlay.json not present in this checkout")
    ov = json.loads(p.read_text(encoding="utf-8"))
    assert _parse_blend_key(ov["underlying_blend_key"]) == pytest.approx(
        BASE_SLEEVE_WEIGHTS)
    gp = ov.get("gate_parameters") or {}
    assert gp.get("derisk_fraction") == pytest.approx(DEFAULT_DERISK_FRACTION)
    p22 = (ov.get("phase22_eem_tilt") or {}).get("parameters") or {}
    if p22:
        assert p22.get("tilt_weight") == pytest.approx(DEFAULT_TILT_WEIGHT)
        assert p22.get("fund_from_sleeve") in ("strategy_b", "b")


def _single_holding_sleeves():
    """Each sleeve fully invested in one distinct ticker, so the live
    track's effective NAV weight IS the sleeve weight."""
    def _s(etf):
        return {"headline": {"trade_history": [
            {"date": "2026-07-17", "holdings": [{"etf": etf, "weight": 1.0}]},
        ]}}
    return {"a": _s("AAA"), "b": _s("BBB"), "c": _s("CCC"), "d": _s("DDD")}


@pytest.mark.parametrize("tilt_on", [False, True])
@pytest.mark.parametrize("risk_off", [False, True])
def test_live_track_weights_equal_overlay_state(tilt_on, risk_off):
    """mark_to_market_live's hand-coded effective weights must equal
    overlay_state's for every overlay state combination."""
    from mark_to_market_live import _build_effective_weights

    got = _build_effective_weights(
        _single_holding_sleeves(), p22_active=tilt_on,
        regime_state="RISK_OFF" if risk_off else "RISK_ON")

    events = ([{"date": "2026-01-02", "direction": "EM_TILT_ON"}]
              if tilt_on else [])
    gate = ([{"date": "2026-01-02", "direction": "RISK_OFF"}]
            if risk_off else [])
    ov = {"events": gate,
          "gate_parameters": {"derisk_fraction": DEFAULT_DERISK_FRACTION},
          "phase22_eem_tilt": {"enabled": True, "events": events,
                                "parameters": {"tilt_weight": DEFAULT_TILT_WEIGHT}}}
    st = sleeve_nav_weights(ov, "2026-07-17")

    assert got["AAA"] == pytest.approx(st["a"])
    assert got["BBB"] == pytest.approx(st["b"])
    assert got["CCC"] == pytest.approx(st["c"])
    assert got["DDD"] == pytest.approx(st["d"])
    assert got.get("EEM", 0.0) == pytest.approx(st["tilt_nav"])
    assert got.get("SHY", 0.0) == pytest.approx(st["shy_overlay"], abs=1e-9)
    assert sum(got.values()) == pytest.approx(1.0)


def test_dashboard_js_port_carries_the_same_constants():
    """The JS port (_sleeveNavWeightsOn in template.html) restates the
    weights as literals; pin them to overlay_state so a Python-side
    reweight that misses the dashboard fails here."""
    html = (ROOT / "template.html").read_text(encoding="utf-8")
    m = re.search(r"function _sleeveNavWeightsOn\([^)]*\)\s*\{(.*?)\n\}",
                  html, re.DOTALL)
    assert m, "_sleeveNavWeightsOn not found in template.html"
    body = m.group(1)

    def _lit(pattern, label):
        mm = re.search(pattern, body)
        assert mm, f"could not locate the {label} literal in the JS port"
        return float(mm.group(1))

    assert _lit(r"a:\s*([0-9.]+)\s*\*\s*s", "sleeve A") == pytest.approx(
        BASE_SLEEVE_WEIGHTS["a"])
    assert _lit(r"tiltOn\s*\?\s*([0-9.]+)\s*-\s*tiltW", "sleeve B base") == \
        pytest.approx(BASE_SLEEVE_WEIGHTS["b"])
    assert _lit(r"c:\s*([0-9.]+)\s*\*\s*s", "sleeve C") == pytest.approx(
        BASE_SLEEVE_WEIGHTS["c"])
    assert _lit(r"d:\s*([0-9.]+)\s*\*\s*s", "sleeve D") == pytest.approx(
        BASE_SLEEVE_WEIGHTS["d"])
    assert _lit(r"tilt_weight\)\s*\|\|\s*([0-9.]+)", "tilt weight default") == \
        pytest.approx(DEFAULT_TILT_WEIGHT)
    assert _lit(r"derisk_fraction\s*\|\|\s*([0-9.]+)", "derisk default") == \
        pytest.approx(DEFAULT_DERISK_FRACTION)
