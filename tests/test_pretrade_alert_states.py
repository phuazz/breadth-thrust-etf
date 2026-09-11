"""Alert semantics and independent deadline coverage. Python months are 1-indexed."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_market_calendars as mcal
import pytest

from scripts.check_pretrade_ready import build_book_report, build_report, main
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
    if which == "broad":
        assert build_report(tmp_path / "breadth_csp1.json", NOW)["tag"] == "DATA-ERROR"


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
