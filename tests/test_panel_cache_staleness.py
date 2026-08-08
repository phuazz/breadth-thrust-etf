"""The guard on stale constituent price caches feeding the Data tab panels.

The condition being guarded is not a crash. ``prices_cache_*.parquet`` is
gitignored, so a local rebuild runs off whatever that machine last fetched,
and the weekly resample stamps the last observation WITHIN each week with
that week's Friday label. A cache that simply stopped advancing therefore
produces a complete, well-formed series in which the final points carry
prices days older than the dates above them — indistinguishable from the
legitimate case of a market shut on the Friday.

So these test the failure paths harder than the success one, and they
assert the property that actually matters: a stale panel is NOT written,
because the committed file is newer than anything the run could produce.

Session gaps below were measured against the real NYSE calendar rather
than counted by hand — including the two boundary cases CLAUDE.md requires,
where a naive calendar-day count would disagree with the session count
(2025-12-31 -> 2026-01-02 is ONE session, New Year's Day being a holiday).
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_panel_series as bps  # noqa: E402
from build_panel_series import (  # noqa: E402
    MAX_CACHE_LAG_SESSIONS,
    StalePriceCacheError,
)

# The 2026-08-08 near-miss, exactly: a cache ending Tuesday against a
# Friday last-completed session. Three sessions behind, and the panel
# would have been stamped 2026-08-07 regardless.
NEAR_MISS_CACHE_END = "2026-08-04"
NEAR_MISS_EXPECTED = date(2026, 8, 7)


def _write_cache(data_dir: Path, etf: str, last_bar: str, n: int = 80) -> None:
    """A price cache whose final bar is ``last_bar``."""
    idx = pd.bdate_range(end=last_bar, periods=n)
    frame = pd.DataFrame(
        {"AAA": range(n), "BBB": range(n)}, index=idx, dtype=float
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(data_dir / f"prices_cache_{etf.lower()}.parquet")


@pytest.fixture
def panel_env(tmp_path, monkeypatch):
    """Isolated DATA_DIR/OUT_DIR, no network, guard override cleared."""
    data, out = tmp_path / "data", tmp_path / "panel"
    monkeypatch.setattr(bps, "DATA_DIR", data)
    monkeypatch.setattr(bps, "OUT_DIR", out)
    # _proxy_series downloads the ETF line live. Stubbed out: it is exactly
    # the asymmetry that makes the bug invisible (fresh ETF line drawn over
    # stale constituent lines), but it is not what is under test here.
    monkeypatch.setattr(bps, "_proxy_series", lambda symbols: {})
    monkeypatch.delenv(bps.OVERRIDE_ENV, raising=False)
    return data, out


# --- the budget ---------------------------------------------------------

def test_current_cache_builds(panel_env):
    data, _ = panel_env
    _write_cache(data, "CSP1", "2026-08-07")
    payload = bps.build_panel("CSP1", expected=NEAR_MISS_EXPECTED)
    assert payload is not None
    assert payload["dates"][-1] == "2026-08-07"


def test_cache_at_the_budget_is_accepted(panel_env):
    """Two sessions behind is the documented cross-calendar allowance."""
    data, _ = panel_env
    _write_cache(data, "CSP1", "2026-08-05")          # 2 sessions behind
    assert bps.build_panel("CSP1", expected=NEAR_MISS_EXPECTED) is not None


def test_cache_past_the_budget_raises(panel_env):
    """The 2026-08-08 near-miss itself: three sessions behind."""
    data, _ = panel_env
    _write_cache(data, "CSP1", NEAR_MISS_CACHE_END)
    with pytest.raises(StalePriceCacheError) as exc:
        bps.build_panel("CSP1", expected=NEAR_MISS_EXPECTED)
    assert "CSP1" in str(exc.value)
    assert exc.value.etfs == ["CSP1"]


def test_budget_is_below_the_observed_near_miss():
    """A budget of 3+ would have waved the real incident through."""
    from nyse_sessions import sessions_behind
    observed = sessions_behind(
        date.fromisoformat(NEAR_MISS_CACHE_END), NEAR_MISS_EXPECTED
    )
    assert observed == 3
    assert MAX_CACHE_LAG_SESSIONS < observed


def test_no_expected_disables_the_check(panel_env):
    """Callers that have already established freshness pay no calendar cost."""
    data, _ = panel_env
    _write_cache(data, "CSP1", "2020-01-06")
    assert bps.build_panel("CSP1", expected=None) is not None


# --- date boundaries ----------------------------------------------------

def test_month_boundary_counts_sessions_not_days(panel_env):
    """31 Jul -> 4 Aug is 4 calendar days but only 2 sessions: accepted."""
    data, _ = panel_env
    _write_cache(data, "CSP1", "2026-07-31")
    assert bps.build_panel("CSP1", expected=date(2026, 8, 4)) is not None

    _write_cache(data, "SOXX", "2026-07-30")          # 3 sessions
    with pytest.raises(StalePriceCacheError):
        bps.build_panel("SOXX", expected=date(2026, 8, 4))


def test_year_boundary_absorbs_the_new_year_holiday(panel_env):
    """31 Dec -> 2 Jan is 2 calendar days but ONE session, 1 Jan being a
    holiday. A calendar-day guard would have to be looser and would then
    miss real staleness in an ordinary week."""
    data, _ = panel_env
    _write_cache(data, "CSP1", "2025-12-31")
    assert bps.build_panel("CSP1", expected=date(2026, 1, 2)) is not None

    _write_cache(data, "SOXX", "2025-12-29")          # 3 sessions
    with pytest.raises(StalePriceCacheError):
        bps.build_panel("SOXX", expected=date(2026, 1, 2))


# --- write_all: the property that matters -------------------------------

def test_stale_panel_is_not_overwritten(panel_env, monkeypatch):
    """The committed file is newer than this run's output — leave it."""
    data, out = panel_env
    monkeypatch.setattr(bps, "ETF_REGISTRY", {"CSP1": {}})
    _write_cache(data, "CSP1", NEAR_MISS_CACHE_END)
    out.mkdir(parents=True, exist_ok=True)
    committed = out / "CSP1.json"
    committed.write_text('{"sentinel":"fresher"}', encoding="utf-8")

    with pytest.raises(StalePriceCacheError):
        bps.write_all(expected=NEAR_MISS_EXPECTED)

    assert json.loads(committed.read_text(encoding="utf-8")) == {
        "sentinel": "fresher"
    }


def test_one_stale_panel_does_not_block_the_fresh_ones(panel_env, monkeypatch):
    data, out = panel_env
    monkeypatch.setattr(bps, "ETF_REGISTRY", {"CSP1": {}, "SOXX": {}})
    _write_cache(data, "CSP1", "2026-08-07")           # fresh
    _write_cache(data, "SOXX", NEAR_MISS_CACHE_END)    # stale

    with pytest.raises(StalePriceCacheError) as exc:
        bps.write_all(expected=NEAR_MISS_EXPECTED)

    assert exc.value.etfs == ["SOXX"]
    assert (out / "CSP1.json").exists()
    assert not (out / "SOXX.json").exists()


def test_error_names_the_fix(panel_env, monkeypatch):
    data, _ = panel_env
    monkeypatch.setattr(bps, "ETF_REGISTRY", {"CSP1": {}})
    _write_cache(data, "CSP1", NEAR_MISS_CACHE_END)
    with pytest.raises(StalePriceCacheError) as exc:
        bps.write_all(expected=NEAR_MISS_EXPECTED)
    message = str(exc.value)
    assert "refresh_all.py" in message
    assert bps.OVERRIDE_ENV in message


def test_override_lets_a_local_rebuild_through(panel_env, monkeypatch):
    data, out = panel_env
    monkeypatch.setattr(bps, "ETF_REGISTRY", {"CSP1": {}})
    monkeypatch.setenv(bps.OVERRIDE_ENV, "1")
    _write_cache(data, "CSP1", NEAR_MISS_CACHE_END)

    assert bps.write_all(expected=NEAR_MISS_EXPECTED) == 0
    assert (out / "CSP1.json").exists()


# --- the audit shares the same policy -----------------------------------
# build_data_audit reads the SAME caches and reports each name's last
# observed close as its current price. Guarding only the panels would leave
# that table publishing days-old prices under today's date.

@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    """Redirected output, override cleared, and BOTH accumulators reset.

    The real build() clears them; these tests stub build() to isolate the
    gate, so without this they leak into one another.
    """
    import build_data_audit as bda

    out = tmp_path / "data_audit.json"
    monkeypatch.setattr(bda, "OUT", out)
    monkeypatch.setattr(bda, "DOCS", tmp_path)
    monkeypatch.delenv(bda.OVERRIDE_ENV, raising=False)
    bda._stale_caches.clear()
    bda._missing_raw.clear()
    yield bda, out
    bda._stale_caches.clear()
    bda._missing_raw.clear()


def test_audit_refuses_to_write_when_a_cache_is_stale(audit_env, monkeypatch):
    bda, out = audit_env
    out.write_text('{"sentinel":"fresher"}', encoding="utf-8")
    # build() is exercised elsewhere; here the gate is what is under test.
    monkeypatch.setattr(bda, "build", lambda: bda._stale_caches.extend(
        ["CSP1", "SOXX"]) or {"summary": []})

    with pytest.raises(StalePriceCacheError) as exc:
        bda.write()

    assert exc.value.etfs == ["CSP1", "SOXX"]
    assert "refresh_all.py" in str(exc.value)
    # The committed file survives untouched.
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "sentinel": "fresher"
    }


def test_audit_refuses_to_write_empty_holdings(audit_env, monkeypatch):
    """A missing iShares payload has a different cause and the same
    consequence: 3,370 populated holdings rows replaced by empty lists in a
    file that stays perfectly well-formed."""
    bda, out = audit_env
    out.write_text('{"sentinel":"populated"}', encoding="utf-8")
    monkeypatch.setattr(bda, "build", lambda: bda._missing_raw.extend(
        ["SOXX"]) or {"summary": []})

    with pytest.raises(StalePriceCacheError) as exc:
        bda.write()

    assert "holdings detail would be written empty" in str(exc.value)
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "sentinel": "populated"
    }


def test_audit_override_publishes_the_thinner_file(audit_env, monkeypatch):
    bda, out = audit_env
    monkeypatch.setenv(bda.OVERRIDE_ENV, "1")
    monkeypatch.setattr(bda, "build", lambda: bda._missing_raw.extend(
        ["SOXX"]) or {"summary": []})

    assert bda.write() == out


def test_audit_writes_when_every_source_is_present(audit_env, monkeypatch):
    bda, out = audit_env
    monkeypatch.setattr(bda, "build", lambda: {"summary": []})

    assert bda.write() == out
    assert json.loads(out.read_text(encoding="utf-8")) == {"summary": []}


def test_both_builders_share_one_policy():
    """A second budget or a second exception type would drift apart."""
    import build_data_audit as bda

    assert bda.StalePriceCacheError is StalePriceCacheError
    assert bda.OVERRIDE_ENV is bps.OVERRIDE_ENV
    assert bda.check_cache_freshness is bps.check_cache_freshness
