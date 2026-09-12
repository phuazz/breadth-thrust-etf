"""Current-session overlay validation; no live vendor calls. Python months are 1-indexed."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import overlay_decision as od
import run_risk_overlay as ro
from component_release import write


def test_core_does_not_rebuild_europe_engine():
    from component_scope import engine_in_scope
    assert not engine_in_scope("scripts/run_europe_rotation.py", "core")
    assert engine_in_scope("scripts/run_europe_rotation.py", "europe")
    assert not engine_in_scope("scripts/run_thematic_rotation.py", "europe")
    assert engine_in_scope("scripts/run_thematic_rotation.py", "core")


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    now = datetime(2026, 9, 12, 6, tzinfo=timezone.utc)
    dates = pd.bdate_range(end="2026-09-11", periods=250)
    panel = {"end_date": "2026-09-11", "series": {"dates": dates.strftime("%Y-%m-%d").tolist(), "ma_breadth": [.6]*250}}
    write(tmp_path / "data/breadth_csp1.json", panel)
    monkeypatch.setattr(od, "ROOT", tmp_path)
    monkeypatch.setattr(ro, "NORGATE_STATES_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(ro, "_load_norgate_states", lambda idx: (None, None))
    monkeypatch.setattr(ro, "_load_eem_data", lambda **kw: (None, pd.Series(1., index=dates)))
    return tmp_path, now, panel, dates


def test_current_gate_and_tilt_are_verified(inputs):
    _, now, _, _ = inputs
    result = od.build(now)
    assert result["gate_input_date"] == result["tilt_input_date"] == "2026-09-11"
    assert result["gate_on"] is False


def test_stale_tilt_is_not_accepted_as_a_recent_cache(inputs, monkeypatch):
    _, now, _, dates = inputs
    monkeypatch.setattr(ro, "_load_eem_data", lambda **kw: (None, pd.Series(1., index=dates[:-1])))
    with pytest.raises(ValueError, match="tilt input is incomplete"):
        od.build(now)


def test_present_but_invalid_preferred_gate_does_not_silently_fall_back(inputs):
    root, now, _, _ = inputs
    write(root / "absent.json", {})
    with pytest.raises(ValueError, match="no silent fallback"):
        od.build(now)


@pytest.mark.parametrize("mutation", ["future", "duplicate", "out_of_range"])
def test_invalid_breadth_source_fails(inputs, mutation):
    root, now, panel, _ = inputs
    if mutation == "future": panel["end_date"] = "2026-09-14"
    if mutation == "duplicate": panel["series"]["dates"][-1] = panel["series"]["dates"][-2]
    if mutation == "out_of_range": panel["series"]["ma_breadth"][-1] = 2.0
    write(root / "data/breadth_csp1.json", panel)
    with pytest.raises(ValueError):
        od.build(now)


def test_loader_refetches_missing_required_session_even_within_seven_days(tmp_path, monkeypatch):
    import yfinance as yf
    dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=5)
    cache = pd.DataFrame({ro.EEM_TICKER: 60., ro.EEM_REFERENCE_TICKER: 500.}, index=dates[:-1])
    monkeypatch.setattr(ro, "DATA_DIR", tmp_path)
    cache.to_parquet(tmp_path / ro.EEM_RATIO_CACHE)
    fetched = pd.DataFrame({(ro.EEM_TICKER, "Close"): 61., (ro.EEM_REFERENCE_TICKER, "Close"): 501.}, index=dates)
    calls = []
    def download(*args, **kw):
        calls.append(args)
        return fetched
    monkeypatch.setattr(yf, "download", download)
    _, ratio = ro._load_eem_data(required_session=dates[-1])
    assert len(calls) == 1 and ratio.index[-1] == dates[-1]
