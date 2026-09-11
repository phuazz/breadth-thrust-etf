"""Alert semantics and independent deadline coverage. Python months are 1-indexed."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_market_calendars as mcal
import pytest

from scripts.check_pretrade_ready import build_book_report, main
from scripts.etf_registry import UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
from scripts.session_bounds import last_completed_session_on


NOW = datetime(2026, 9, 13, 6, tzinfo=timezone.utc)


def write_book(root, now=NOW):
    sleeves = []
    for name in "ABCD":
        cal = mcal.get_calendar("XETR" if name == "D" else "NYSE")
        last = last_completed_session_on(cal, now)
        fill = cal.schedule(start_date=last + pd.Timedelta(days=1),
                            end_date=last + pd.Timedelta(days=10)).index[0]
        sleeves.append({"sleeve": name, "status": "READY",
                        "decision_session": str(last.date()),
                        "last_completed_session": str(last.date()),
                        "decision_session_for_fill": str(last.date()),
                        "fill_date": str(fill.date())})
        if name in "AD":
            universe = UNIVERSE_EUROPE_SECTORS if name == "D" else UNIVERSE_ETFS
            for etf in universe:
                (root / f"breadth_{etf.lower()}.json").write_text(json.dumps({"end_date": str(last.date())}))
    book = {"computed_at_utc": now.isoformat(), "targets_final": True,
            "sleeves": sleeves, "lines": []}
    save(root, book)
    return book


def save(root, book):
    (root / "live_targets.json").write_text(json.dumps(book))


def check(root, phase="deadline", now=NOW):
    return build_book_report(root / "breadth_csp1.json", now, phase)


def held_book(root):
    book = write_book(root)
    sl = book["sleeves"][-1]
    sl.update(status="HOLD", reason="vendor close unavailable", decision_session="2026-09-10")
    book["targets_final"] = False
    book["lines"] = [{"sleeve": "D", "held": 0.2, "target": 0.2, "delta": 0}]
    for etf in UNIVERSE_EUROPE_SECTORS:
        (root / f"breadth_{etf.lower()}.json").write_text(json.dumps({"end_date": "2026-09-10"}))
    save(root, book)
    return book


@pytest.mark.parametrize("phase,warn,status", [("review", "false", "pending"),
                                               ("deadline", "true", "not_ready")])
def test_missing_book_needs_no_green_marker(tmp_path, phase, warn, status):
    result = check(tmp_path, phase)
    assert result["warn"] == warn
    assert result["status"] == status
    assert "cannot observe its process state" in result["detail"]


@pytest.mark.parametrize("phase", ["review", "deadline"])
def test_ready_is_quiet(tmp_path, phase):
    write_book(tmp_path)
    assert check(tmp_path, phase)["status"] == "ready"
    assert check(tmp_path, phase)["warn"] == "false"


@pytest.mark.parametrize("phase,warn", [("review", "false"), ("deadline", "true")])
def test_current_hold_is_not_missing_book(tmp_path, phase, warn):
    held_book(tmp_path)
    result = check(tmp_path, phase)
    assert result["status"] == "hold"
    assert result["tag"] == "HOLD"
    assert result["warn"] == warn
    assert "retain existing holdings" in result["detail"]
    assert "current instruction unavailable" not in result["summary"]


def test_old_hold_remains_pending(tmp_path):
    book = held_book(tmp_path)
    book["computed_at_utc"] = "2026-09-09T07:00:00+00:00"
    book["sleeves"][-1]["last_completed_session"] = "2026-09-08"
    save(tmp_path, book)
    assert check(tmp_path, "review")["status"] == "pending"
    assert check(tmp_path)["tag"] == "PRE-TRADE"


@pytest.mark.parametrize("mutation", ["delta", "target", "nan", "reason", "lines", "final"])
def test_invalid_hold_alerts_even_at_review(tmp_path, mutation):
    book = held_book(tmp_path)
    if mutation == "delta":
        book["lines"][0]["delta"] = 0.01
    elif mutation == "target":
        book["lines"][0]["target"] = 0.3
    elif mutation == "nan":
        book["lines"][0]["held"] = float("nan")
    elif mutation == "reason":
        book["sleeves"][-1]["reason"] = None
    elif mutation == "lines":
        del book["lines"]
    else:
        book["targets_final"] = True
    save(tmp_path, book)
    result = check(tmp_path, "review")
    assert result["status"] == "error" and result["warn"] == "true"


@pytest.mark.parametrize("which", ["broad", "europe", "decision", "build", "held_evaluation"])
def test_future_dates_are_invalid_everywhere(tmp_path, which):
    book = held_book(tmp_path) if which == "held_evaluation" else write_book(tmp_path)
    if which in ("broad", "europe"):
        etf = "CSP1" if which == "broad" else UNIVERSE_EUROPE_SECTORS[0]
        (tmp_path / f"breadth_{etf.lower()}.json").write_text(json.dumps({"end_date": "2026-09-14"}))
    else:
        if which == "decision":
            book["sleeves"][0]["decision_session"] = "2026-09-14"
        elif which == "build":
            book["computed_at_utc"] = (NOW + timedelta(hours=1)).isoformat()
        else:
            book["sleeves"][-1]["last_completed_session"] = "2026-09-14"
        save(tmp_path, book)
    result = check(tmp_path, "review")
    assert result["tag"] == "DATA-ERROR" and result["warn"] == "true"


@pytest.mark.parametrize("filename", ["live_targets.json", "breadth_csp1.json"])
def test_malformed_json_always_alerts(tmp_path, filename):
    write_book(tmp_path)
    (tmp_path / filename).write_text("{bad-json")
    assert check(tmp_path, "review")["tag"] == "DATA-ERROR"


def test_missing_source_is_pending_not_ready(tmp_path):
    write_book(tmp_path)
    (tmp_path / "breadth_csp1.json").unlink()
    assert check(tmp_path, "review")["status"] == "pending"
    assert check(tmp_path)["warn"] == "true"


@pytest.mark.parametrize("content", ["{bad-json", '{"end_date":"2026-09-14"}'])
def test_missing_instruction_does_not_hide_invalid_panel(tmp_path, content):
    (tmp_path / "breadth_csp1.json").write_text(content)
    assert check(tmp_path, "review")["tag"] == "DATA-ERROR"


@pytest.mark.parametrize("instant", ["2026-05-31T22:00:00+00:00",  # month boundary
                                    "2027-01-03T22:00:00+00:00",  # year boundary
                                    "2026-09-06T22:00:00+00:00"])  # NYSE holiday, Xetra open
def test_deadline_and_observed_delay_keep_required_venue_sessions(tmp_path, instant):
    now = datetime.fromisoformat(instant)
    write_book(tmp_path, now)
    assert check(tmp_path, now=now)["status"] == "ready"
    assert check(tmp_path, now=now + timedelta(hours=11, minutes=48))["status"] == "ready"


def test_manual_cli_defaults_to_deadline_and_outputs_alert(tmp_path, monkeypatch, capsys):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(["--panel", str(tmp_path / "breadth_csp1.json"), "--now", NOW.isoformat()]) == 0
    assert "[PRE-TRADE]" in capsys.readouterr().out
    assert "warn=true" in output.read_text()


def test_review_cli_outputs_quiet_pending(tmp_path, monkeypatch):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(["--panel", str(tmp_path / "breadth_csp1.json"), "--now", NOW.isoformat(),
                 "--phase", "review"]) == 0
    assert "warn=false" in output.read_text()


def test_unsupported_phase_is_not_silently_quiet(tmp_path):
    with pytest.raises(ValueError):
        check(tmp_path, "typo")


@pytest.mark.parametrize("mutation", ["duplicate", "missing_build", "naive_build", "unknown_status", "closed_fill"])
def test_invalid_instruction_metadata_alerts(tmp_path, mutation):
    book = write_book(tmp_path)
    if mutation == "duplicate":
        book["sleeves"][-1]["sleeve"] = "A"
    elif mutation == "missing_build":
        del book["computed_at_utc"]
    elif mutation == "naive_build":
        book["computed_at_utc"] = NOW.replace(tzinfo=None).isoformat()
    elif mutation == "unknown_status":
        book["sleeves"][0]["status"] = "UNKNOWN"
    else:
        book["sleeves"][0]["fill_date"] = "2026-09-13"
    save(tmp_path, book)
    assert check(tmp_path, "review")["tag"] == "DATA-ERROR"


def a_hold_book(root):
    book = write_book(root)
    book["sleeves"][0].update(status="HOLD", reason="sector signal incomplete",
                                decision_session="2026-09-10")
    book["targets_final"] = False
    book["lines"] = [{"sleeve": "A", "held": 0.35, "target": 0.35, "delta": 0}]
    other = next(etf for etf in UNIVERSE_ETFS if etf != "CSP1")
    (root / f"breadth_{other.lower()}.json").write_text(json.dumps({"end_date": "2026-09-10"}))
    save(root, book)
    return book


@pytest.mark.parametrize("phase,warn", [("review", "false"), ("deadline", "true")])
@pytest.mark.parametrize("missing", [False, True])
def test_a_hold_cannot_waive_cross_book_capture(tmp_path, phase, warn, missing):
    a_hold_book(tmp_path)
    panel = tmp_path / "breadth_csp1.json"
    if missing:
        panel.unlink()
    else:
        panel.write_text(json.dumps({"end_date": "2026-09-10"}))
    result = check(tmp_path, phase)
    assert result["status"] == ("pending" if phase == "review" else "not_ready")
    assert result["warn"] == warn
    assert "breadth_csp1.json" in result["detail"]
    if not missing:
        assert "cross-book risk-overlay input" in result["detail"]


def test_a_hold_with_current_broad_market_keeps_hold_verdict(tmp_path):
    a_hold_book(tmp_path)
    assert check(tmp_path)["status"] == "hold"


@pytest.mark.parametrize("which", ["panel", "decision"])
def test_invalid_observation_is_not_also_reported_as_pending(tmp_path, which):
    book = write_book(tmp_path)
    if which == "panel":
        (tmp_path / "breadth_csp1.json").write_text(json.dumps({"end_date": "2026-09-14"}))
    else:
        book["sleeves"][0]["decision_session"] = "2026-09-14"
        save(tmp_path, book)
    result = check(tmp_path)
    assert result["tag"] == "DATA-ERROR"
    assert result["detail"].count("future-dated") == 1
    assert "Pending verification:" not in result["detail"]


@pytest.mark.parametrize("scenario", ["missing", "hold", "invalid"])
def test_live_alert_contains_safe_recovery_steps(tmp_path, scenario):
    if scenario == "hold":
        held_book(tmp_path)
    elif scenario == "invalid":
        (tmp_path / "live_targets.json").write_text("{bad-json")
    result = check(tmp_path)
    assert result["warn"] == "true"
    text = result["detail"]
    assert "Do not start a second refresh while one is active" in text
    assert "C:\\dev\\breadth-thrust-etf-sched" in text
    assert "python scripts/scheduled_refresh.py" in text
    assert "does not commit or push" in text
    assert "Do not trade on the stale card" in text
    assert "broker order cutoffs" in text


@pytest.mark.parametrize("instant,expected", [
    ("2026-09-13T06:00:00+00:00", "NYSE: Tue 2026-09-15 04:00 SGT"),
    ("2027-01-03T06:00:00+00:00", "NYSE: Tue 2027-01-05 05:00 SGT"),
    ("2026-09-06T06:00:00+00:00", "NYSE: Wed 2026-09-09 04:00 SGT"),
])
def test_report_closing_times_follow_dst_and_venue_holidays(tmp_path, instant, expected):
    now = datetime.fromisoformat(instant)
    write_book(tmp_path, now)
    result = check(tmp_path, now=now)
    assert result["status"] == "ready"
    assert expected in result["detail"]
    assert "not broker cutoffs or trade authorisation" in result["detail"]


def test_cli_unexpected_checker_failure_keeps_recovery_guidance(tmp_path, monkeypatch, capsys):
    from scripts import check_pretrade_ready as checker
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic checker failure")
    monkeypatch.setattr(checker, "build_book_report", fail)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    assert checker.main(["--now", NOW.isoformat()]) == 0
    assert "python scripts/scheduled_refresh.py" in capsys.readouterr().out
    assert "warn=true" in (tmp_path / "output").read_text()
