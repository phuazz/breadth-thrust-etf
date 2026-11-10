"""Tests for the post-fill confirmation and the Thursday miss alert.

Covers the cases the 2026-11-10 brief named: normal week, missed week,
holiday Monday rolling the fill to Tuesday, month boundary, year boundary,
and the dedupe (Tuesday and Wednesday both succeed = one email). Also the
case that made deployed_series_end alone insufficient: the CI daily live
track moves it past the fill with no post-fill run behind it.

Python date months are 1-indexed (January = 1). Weekdays are asserted with
date.weekday(), never assumed.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import date, datetime, timezone

import pytest
from types import SimpleNamespace

from scripts import postfill_publication as pp
from scripts import check_postfill_published as cpp


# ---------------------------------------------------------------------------
# Calendar: the fill date comes from the engine's own holiday-roll calendar
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("asof,nyse,xetr", [
    # normal week: Thu 12 Nov 2026 -> Mon 9 Nov fill on both venues
    (date(2026, 11, 12), date(2026, 11, 9), date(2026, 11, 9)),
    # holiday Monday, venues split: Labor Day 7 Sep 2026 rolls NYSE to Tue 8 Sep,
    # Xetra trades the 7th
    (date(2026, 9, 10), date(2026, 9, 8), date(2026, 9, 7)),
    # holiday Monday on both sides of the check: MLK 19 Jan 2026 -> Tue 20 Jan
    (date(2026, 1, 22), date(2026, 1, 20), date(2026, 1, 19)),
    # month boundary: Thu 3 Dec 2026 -> Mon 30 Nov fill
    (date(2026, 12, 3), date(2026, 11, 30), date(2026, 11, 30)),
    # year boundary: Thu 1 Jan 2026 (both venues shut) -> Mon 29 Dec 2025 fill
    (date(2026, 1, 1), date(2025, 12, 29), date(2025, 12, 29)),
    # year boundary, first week: Thu 7 Jan 2027 -> Mon 4 Jan 2027
    (date(2027, 1, 7), date(2027, 1, 4), date(2027, 1, 4)),
])
def test_latest_fill_uses_engine_calendar(asof, nyse, xetr):
    assert pp.latest_fill("NYSE", asof) == nyse
    assert pp.latest_fill("XETR", asof) == xetr


def test_weekdays_of_the_fixtures_are_what_the_cases_claim():
    assert date(2026, 9, 7).weekday() == 0 and date(2026, 9, 8).weekday() == 1
    assert date(2026, 1, 19).weekday() == 0 and date(2026, 1, 20).weekday() == 1
    assert date(2026, 11, 30).weekday() == 0 and date(2025, 12, 29).weekday() == 0
    assert date(2027, 1, 4).weekday() == 0


def test_a_fill_on_the_check_date_is_not_yet_due():
    # Tue 8 Sep 2026 is itself the rolled NYSE fill: it has not closed when
    # the 01:00 UTC refresh runs, so the latest DUE fill is the previous one.
    assert pp.latest_fill("NYSE", date(2026, 9, 8)) == date(2026, 8, 31)
    assert pp.latest_fill("NYSE", date(2026, 9, 9)) == date(2026, 9, 8)


def test_engine_frequency_is_pinned_to_the_engines():
    from scripts import run_topk_robustness as tk
    assert pp.ENGINE_FREQ == tk.HEADLINE_FREQ


# ---------------------------------------------------------------------------
# Verdict (pure)
# ---------------------------------------------------------------------------
FILL = date(2026, 11, 9)
FILLS = {"NYSE": FILL, "XETR": FILL}


def _book(**status):
    return {"as_of": "2026-11-06", "sleeves": [
        {"sleeve": s, "venue": v, "fill_date": FILL.isoformat(),
         "status": status.get(s, "READY"),
         "reason": "late BTC-USD bar" if status.get(s) == "HOLD" else None}
        for s, v in pp.DEFAULT_SLEEVE_VENUE.items()]}


def _track(anchor, end):
    return {"readable": True, "anchor_date": anchor, "deployed_series_end": end}


ALL_AT_FILL = {s: FILL for s in "ABCD"}
ALL_PRIOR = {s: date(2026, 11, 2) for s in "ABCD"}


def test_normal_week_is_ok():
    a = pp.assess(FILLS, ALL_AT_FILL, _track(FILL, date(2026, 11, 11)), _book())
    assert a.verdict == pp.OK and a.updated == list("ABCD") and not a.behind


def test_missed_week_is_missed():
    a = pp.assess(FILLS, ALL_PRIOR, _track(date(2026, 11, 6), date(2026, 11, 6)), _book())
    assert a.verdict == pp.MISSED and a.behind == list("ABCD")


def test_ci_advance_of_deployed_series_end_alone_does_not_pass():
    # The 29 Oct 2026 shape on origin/main: the daily CI track had moved
    # deployed_series_end to the 27th, past the 26 Oct fill, while the
    # anchor and every sleeve record still sat on the previous week.
    a = pp.assess(FILLS, ALL_PRIOR, _track(date(2026, 11, 6), date(2026, 11, 11)), _book())
    assert a.verdict == pp.MISSED


def test_records_at_fill_but_track_not_reanchored_is_missed():
    a = pp.assess(FILLS, ALL_AT_FILL, _track(date(2026, 11, 6), date(2026, 11, 11)), _book())
    assert a.verdict == pp.MISSED


def test_authorised_hold_is_held_not_missed():
    recs = dict(ALL_AT_FILL, C=date(2026, 11, 2))
    a = pp.assess(FILLS, recs, _track(FILL, FILL), _book(C="HOLD"),
                  hold_ok=lambda s: s["sleeve"] == "C")
    assert a.verdict == pp.HELD and a.behind == ["C"] and a.held_authorised == ["C"]


def test_unauthorised_hold_is_missed():
    recs = dict(ALL_AT_FILL, C=date(2026, 11, 2))
    a = pp.assess(FILLS, recs, _track(FILL, FILL), _book(C="HOLD"))
    assert a.verdict == pp.MISSED


def test_hold_cannot_excuse_a_track_that_never_reanchored():
    recs = dict(ALL_PRIOR)
    a = pp.assess(FILLS, recs, _track(date(2026, 11, 6), date(2026, 11, 6)),
                  _book(A="HOLD", B="HOLD", C="HOLD", D="HOLD"), hold_ok=lambda s: True)
    assert a.verdict == pp.MISSED


def test_unreadable_record_is_unknown_never_ok():
    a = pp.assess(FILLS, dict(ALL_AT_FILL, D=None), _track(FILL, FILL), _book())
    assert a.verdict == pp.UNKNOWN
    a = pp.assess(FILLS, ALL_AT_FILL, {"readable": False}, _book())
    assert a.verdict == pp.UNKNOWN
    a = pp.assess({"NYSE": None, "XETR": FILL}, ALL_AT_FILL, _track(FILL, FILL), _book())
    assert a.verdict == pp.UNKNOWN


def test_split_venue_week_checks_each_sleeve_against_its_own_venue():
    fills = {"NYSE": date(2026, 9, 8), "XETR": date(2026, 9, 7)}
    recs = {"A": date(2026, 9, 8), "B": date(2026, 9, 8), "C": date(2026, 9, 8),
            "D": date(2026, 9, 7)}
    a = pp.assess(fills, recs, _track(date(2026, 9, 8), date(2026, 9, 9)), None)
    assert a.verdict == pp.OK
    assert "NYSE 2026-09-08" in pp.fills_text(fills)


def test_book_for_fill_reads_history_not_the_rolled_current_file():
    rolled = {"sleeves": [{"sleeve": "A", "venue": "NYSE", "fill_date": "2026-11-16"}]}
    ranked = _book()
    assert pp.book_for_fill([rolled, ranked], FILLS) is ranked
    assert pp.book_for_fill([rolled], FILLS) is None


def test_miss_report_mails_and_held_does_not():
    missed = pp.assess(FILLS, ALL_PRIOR, _track(date(2026, 11, 6), date(2026, 11, 6)), _book())
    mail, subject, body = cpp.report(missed, None, "origin/main", date(2026, 11, 12))
    assert mail and subject.startswith("[MISS]") and "2026-11-09" in subject
    held = pp.assess(FILLS, dict(ALL_AT_FILL, C=date(2026, 11, 2)), _track(FILL, FILL),
                     _book(C="HOLD"), hold_ok=lambda s: True)
    mail, subject, body = cpp.report(held, "abc1234", "origin/main", date(2026, 11, 12))
    assert not mail and subject.startswith("[HELD]") and "late BTC-USD bar" in body
    ok = pp.assess(FILLS, ALL_AT_FILL, _track(FILL, FILL), _book())
    assert cpp.report(ok, "abc1234", "origin/main", date(2026, 11, 12))[0] is False


def test_notice_body_carries_the_required_fields():
    a = pp.assess(FILLS, ALL_AT_FILL, _track(FILL, date(2026, 11, 9)), _book())
    subject, body = pp.build_notice(a, "b764d01")
    assert subject == "[OK] Post-fill published - fill 2026-11-09"
    for needle in ("2026-11-09", "deployed_series_end: 2026-11-09", "b764d01",
                   "Sleeves updated to the fill: A, B, C, D", "HOLD: none"):
        assert needle in body


# ---------------------------------------------------------------------------
# The success notice end to end, in a throwaway repo with an origin
# ---------------------------------------------------------------------------
def _write_record(repo, rebalance: date, anchor: date, end: date):
    for name in pp.SLEEVE_FILES.values():
        (repo / "data" / name).write_text(json.dumps(
            {"headline": {"latest_rebalance": {"date": rebalance.isoformat()}}}),
            encoding="utf-8")
    (repo / "data" / "live_track.json").write_text(json.dumps(
        {"anchor_date": anchor.isoformat(), "deployed_series_end": end.isoformat()}),
        encoding="utf-8")
    (repo / "data" / "live_targets.json").write_text(json.dumps(_book()), encoding="utf-8")


@pytest.fixture
def clone(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t.test\n[core]\n\tlongpaths = true\n"
                   "\tautocrlf = false\n[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "o.git"
    repo = tmp_path / "c"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    (repo / "data").mkdir(parents=True)
    _write_record(repo, date(2026, 11, 2), date(2026, 11, 6), date(2026, 11, 6))

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "Local weekly refresh 2026-11-08 (scheduled)")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    return SimpleNamespace(path=repo, git=git)


def _post_fill_run(repo, state, sent, outcome="SENT", day=date(2026, 11, 10),
                   change=True, push=True):
    if change:
        end = day
        _write_record(repo.path, FILL, day if day > FILL else FILL, end)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", f"Local post-fill refresh {day.isoformat()} (scheduled)")
    if push:
        repo.git("push", "-q", "origin", "main")

    def send(subject, body):
        sent.append((subject, body))
        return outcome
    return pp.postfill_success_notice(
        repo.path, created_commit=change,
        now_utc=datetime(day.year, day.month, day.day, 1, 30, tzinfo=timezone.utc),
        send=send, sent_status="SENT", state_path=state, log=io.StringIO(),
        extra="Factsheet anchor status line", hold_ok=lambda s: False)


def test_tuesday_and_wednesday_both_succeed_one_email(clone, tmp_path):
    state, sent = tmp_path / "state.json", []
    assert _post_fill_run(clone, state, sent, day=date(2026, 11, 10)) == "sent"
    assert _post_fill_run(clone, state, sent, day=date(2026, 11, 11)) == "duplicate"
    assert len(sent) == 1
    subject, body = sent[0]
    assert "2026-11-09" in subject and "Factsheet anchor status line" in body
    assert json.loads(state.read_text())["sent"].keys() == {"NYSE:2026-11-09|XETR:2026-11-09"}


def test_a_run_that_pushed_nothing_sends_nothing(clone, tmp_path):
    state, sent = tmp_path / "state.json", []
    assert _post_fill_run(clone, state, sent, change=False) == "no-commit"
    assert sent == [] and not state.exists()


def test_a_commit_not_on_origin_sends_nothing(clone, tmp_path):
    state, sent = tmp_path / "state.json", []
    assert _post_fill_run(clone, state, sent, push=False) == "not-on-origin"
    assert sent == []


def test_a_failed_send_does_not_consume_the_fill(clone, tmp_path):
    state, sent = tmp_path / "state.json", []
    assert _post_fill_run(clone, state, sent, outcome="UNCONFIGURED",
                          day=date(2026, 11, 10)) == "send-failed:UNCONFIGURED"
    assert not state.exists()
    assert _post_fill_run(clone, state, sent, day=date(2026, 11, 11)) == "sent"
    assert len(sent) == 2


def test_the_next_weeks_fill_gets_its_own_notice(clone, tmp_path):
    state, sent = tmp_path / "state.json", []
    assert _post_fill_run(clone, state, sent, day=date(2026, 11, 10)) == "sent"
    # A week later the record has not moved to the 16 Nov fill: an
    # unconfirmed notice goes out and leaves that fill's key open.
    clone.git("commit", "-q", "--allow-empty", "-m", "Local post-fill refresh 2026-11-17")
    clone.git("push", "-q", "origin", "main")
    out = pp.postfill_success_notice(
        clone.path, created_commit=True, now_utc=datetime(2026, 11, 17, 1, 30, tzinfo=timezone.utc),
        send=lambda s, b: sent.append((s, b)) or "SENT", sent_status="SENT",
        state_path=state, log=io.StringIO(), hold_ok=lambda s: False)
    assert out == "sent-unconfirmed" and sent[-1][0].startswith("[CHECK]")
    assert "NYSE:2026-11-16|XETR:2026-11-16" not in json.loads(state.read_text())["sent"]


def test_notice_state_prunes_old_keys(tmp_path):
    state = tmp_path / "s.json"
    pp.record_notified(state, "old", "a", datetime(2026, 1, 5, tzinfo=timezone.utc))
    pp.record_notified(state, "new", "b", datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert set(json.loads(state.read_text())["sent"]) == {"new"}


def test_corrupt_notice_state_reads_as_nothing_sent(tmp_path):
    state = tmp_path / "s.json"
    state.write_text("[]")
    assert pp.already_notified(state, "x") is False


# ---------------------------------------------------------------------------
# Wiring: the refresh routes post-fill pushes through the new notice
# ---------------------------------------------------------------------------
def test_scheduled_refresh_routes_post_fill_through_the_deduped_notice():
    import inspect
    from scripts import scheduled_refresh as sr
    src = inspect.getsource(sr.main)
    assert "postfill_publication.postfill_success_notice" in src
    assert "created_commit=created_commit" in src
