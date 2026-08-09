"""The guard on the scheduled publication-lag probe.

A guard is only worth having if it fails when it should, so these test the
failure paths harder than the success one. The condition being guarded is
not a crash — it is a probe that exits 0 having recorded nothing usable, so
the log grows, the workflow stays green, and the measurement window is
quietly empty.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from check_lag_probe import EXPECTED_ETFS, check, load_rows  # noqa: E402

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _row(etf, minutes_ago=1, latest="2026-08-06", errors=None):
    return {
        "probe_utc": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "etf": etf,
        "latest_with_data": latest,
        "sessions_behind_nyse": 1,
        "errors": errors or {},
    }


def _full_run(**kw):
    return [_row(e, **kw) for e in sorted(EXPECTED_ETFS)]


def test_healthy_run_passes():
    code, msgs = check(_full_run(), NOW, 45)
    assert code == 0
    assert any(m.startswith("OK") for m in msgs)


def test_empty_log_fails():
    code, msgs = check([], NOW, 45)
    assert code == 1
    assert "wrote nothing" in " ".join(msgs)


def test_stale_rows_fail_even_though_the_log_is_populated():
    """The trap this exists for. Yesterday's rows would otherwise satisfy
    every other assertion, and a probe that silently stopped writing would
    keep passing for as long as the file existed."""
    code, msgs = check(_full_run(minutes_ago=60 * 26), NOW, 45)
    assert code == 1
    assert "appended nothing" in " ".join(msgs)


def test_run_with_no_data_for_any_etf_fails():
    """Rows exist, so the log grew and the job would look successful — but
    an endpoint outage recorded as data is not an observation."""
    code, msgs = check(_full_run(latest=None), NOW, 45)
    assert code == 1
    assert "recorded no observation" in " ".join(msgs)


def test_missing_etf_fails():
    rows = [_row(e) for e in sorted(EXPECTED_ETFS)][:-1]
    code, msgs = check(rows, NOW, 45)
    assert code == 1
    assert "missing ETFs" in " ".join(msgs)


def test_partial_data_warns_but_passes():
    """One quiet ETF is a real observation about that fund, not a failed
    run — a fund whose holdings are genuinely unpublished is exactly what
    the probe is measuring."""
    rows = _full_run()
    rows[0]["latest_with_data"] = None
    code, msgs = check(rows, NOW, 45)
    assert code == 0
    assert any(m.startswith("WARN") for m in msgs)


def test_per_date_errors_warn_but_pass():
    rows = _full_run()
    rows[1]["errors"] = {"2026-08-07": "HTTP 503"}
    code, msgs = check(rows, NOW, 45)
    assert code == 0
    assert "per-date errors" in " ".join(msgs)


def test_malformed_line_fails(tmp_path):
    """An append-only log with a broken line means a run died mid-write."""
    p = tmp_path / "log.jsonl"
    p.write_text('{"probe_utc": "2026-08-08T11:59:00+00:00", "etf": "CSP1"}\n'
                  '{ truncated\n', encoding="utf-8")
    code, msgs = check(load_rows(p), NOW, 45)
    assert code == 1
    assert "malformed" in " ".join(msgs)


def test_naive_timestamps_are_treated_as_utc():
    """The probe writes tz-aware stamps, but a naive one must not be read as
    local time and silently fall outside the freshness window."""
    rows = _full_run()
    for r in rows:
        r["probe_utc"] = (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    code, _ = check(rows, NOW, 45)
    assert code == 0


def test_missing_log_file_reads_as_empty(tmp_path):
    assert load_rows(tmp_path / "nope.jsonl") == []


# --- the stratified read (--summary) ------------------------------------
#
# The failure mode here is not a crash either. It is a reader — human or
# scheduled — averaging sessions-behind across the whole log and reporting a
# confident number that tracks the sample's composition rather than the funds'
# behaviour. The first 24 rows sampled 3 UCITS to 1 US. These pin the two
# properties that stop that: legacy rows never join a cohort, and no pooled
# statistic is ever printed.


def _balanced(etf, dom, behind, latest="2026-08-07", day="2026-08-09"):
    return {"probe_utc": f"{day}T03:00:00+00:00", "etf": etf, "domicile": dom,
            "latest_with_data": latest, "sessions_behind_nyse": behind}


def _legacy(etf, behind, latest="2026-08-06", day="2026-08-08"):
    """Pre-widening: no domicile key. That absence is the vintage marker."""
    return {"probe_utc": f"{day}T03:00:00+00:00", "etf": etf,
            "latest_with_data": latest, "sessions_behind_nyse": behind}


def test_legacy_rows_are_never_counted_into_a_cohort():
    """The whole point. A legacy SOXX row must not swell the US cohort — it
    was gathered under a sample that could not separate domicile from fund."""
    from check_lag_probe import summarise
    rows = [_balanced("IVV", "US", 0), _balanced("CSP1", "UK", 1),
            _legacy("SOXX", 1), _legacy("CSP1", 1)]
    text = "\n".join(summarise(rows))
    # One balanced row per cohort, NOT two — the legacy SOXX and CSP1 rows
    # are the ones that must not be absorbed.
    assert "US  n=1" in text, text
    assert "UK  n=1" in text, text
    assert "LEGACY ROWS (no domicile field, pre-widening): 2 rows" in text, text
    assert "funds=['IVV']" in text, "legacy SOXX leaked into the US cohort"


def test_row_domicile_marks_untagged_rows_as_unbalanced_vintage():
    from check_lag_probe import row_domicile
    assert row_domicile(_balanced("IVV", "US", 0)) == ("US", True)
    dom, balanced = row_domicile(_legacy("SOXX", 1))
    assert balanced is False, "an untagged row must not read as balanced"
    assert dom == "US", "domicile is still inferable for display, just not usable"


def test_summary_reports_no_pooled_average():
    """A single mean over the log is the wrong statistic and must not appear,
    however tempting it is to add one later."""
    from check_lag_probe import summarise
    lines = summarise([_balanced("IVV", "US", 0),
                       _balanced("CSP1", "UK", 1), _legacy("SOXX", 1)])
    assert not any("mean" in ln.lower() for ln in lines), lines
    # "average" may appear only in the warning telling the reader not to.
    offenders = [ln for ln in lines
                 if "average" in ln.lower() and "not average" not in ln.lower()]
    assert not offenders, offenders


def test_below_bar_warning_fires_on_a_single_friday():
    from check_lag_probe import summarise
    text = "\n".join(summarise([_balanced("IVV", "US", 0, latest="2026-08-07")]))
    assert "BELOW BAR" in text


def test_two_fridays_clears_the_bar_warning():
    """2026-08-07 and 2026-07-31 are both Fridays — verified via date.weekday()
    inside summarise, not asserted from memory."""
    from check_lag_probe import summarise
    rows = [_balanced("IVV", "US", 0, latest="2026-08-07", day="2026-08-09"),
            _balanced("IVV", "US", 0, latest="2026-07-31", day="2026-08-01")]
    assert "BELOW BAR" not in "\n".join(summarise(rows))


def test_a_non_friday_latest_does_not_count_as_a_friday_observation():
    """2026-08-06 is a Thursday. Guards the weekday arithmetic itself."""
    from check_lag_probe import summarise
    text = "\n".join(summarise([_balanced("CSP1", "UK", 1, latest="2026-08-06")]))
    assert "Friday observations in the balanced sample: 0" in text


def test_summary_survives_an_empty_log():
    from check_lag_probe import summarise
    assert "none yet" in "\n".join(summarise([]))
