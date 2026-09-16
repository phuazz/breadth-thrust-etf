"""The publication obligation must survive the thing it polices.

Every test below either pins a property the first version of the module
lacked, or pins a date-arithmetic boundary per the house rules. Python
datetime months are 1-indexed (January = 1).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publication_debt as pd_          # noqa: E402
import scheduled_refresh as sr          # noqa: E402

UTC = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures: a real git repository, because the evidence is a remote ref
# ---------------------------------------------------------------------------
def _git(repo: Path, *args, **kw):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True, **kw)


def _sleeve_docs(repo: Path, dates: dict[str, str]) -> None:
    (repo / "data").mkdir(parents=True, exist_ok=True)
    for sleeve, name in pd_.SLEEVE_FILES.items():
        d = dates.get(sleeve)
        doc = {"headline": {"latest_rebalance": {"date": d,
                                                 "decision_date": d}}} if d else {}
        (repo / "data" / name).write_text(json.dumps(doc), encoding="utf-8")


def _targets(fills: dict[str, str], sleeve_venues: dict[str, str],
             decisions: dict[str, str] | None = None) -> dict:
    distinct = sorted(set(fills.values()))
    return {"next_fill": {"by_venue": dict(fills),
                          "date": distinct[0] if len(distinct) == 1 else None,
                          "venues_agree": len(distinct) == 1,
                          "decision_by_venue": dict(decisions or {})},
            "sleeves": [{"sleeve": s, "venue": v}
                        for s, v in sorted(sleeve_venues.items())]}


@pytest.fixture
def repo(tmp_path):
    """A clone with an origin, so origin/main means what it means live."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    # Long temp paths break bare-repo pushes on Windows unless longpaths is on.
    _git(work, "config", "core.longpaths", "true")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _sleeve_docs(work, {"A": "2026-09-08", "B": "2026-09-08",
                        "C": "2026-09-08", "D": "2026-09-07"})
    (work / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-14", "XETR": "2026-09-14"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"},
        {"NYSE": "2026-09-11", "XETR": "2026-09-11"})), encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m",
         "Local post-fill refresh 2026-09-09 (scheduled): panels current to "
         "2026-09-08, all steps OK")
    _git(work, "push", "origin", "main")
    return work


def _debt(repo: Path, asof: date, **kw):
    return pd_.current_debt(repo, asof, ledger=repo / "logs" / "ob.json", **kw)


# ---------------------------------------------------------------------------
# 1. The obligation is durable
# ---------------------------------------------------------------------------
def test_advancing_next_fill_does_not_discharge_the_older_obligation(repo):
    """THE REPRODUCED DEFECT. next_fill=2026-09-21, published 2026-09-08,
    asof 2026-09-16 returned owed:false and the 14 September obligation
    simply vanished."""
    first = _debt(repo, date(2026, 9, 16))
    assert first.owed is True
    assert first.oldest_owed_fill == "2026-09-14"

    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21", "XETR": "2026-09-21"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"})),
        encoding="utf-8")
    later = _debt(repo, date(2026, 9, 16))
    assert later.owed is True, "advancing the target discharged the old debt"
    assert later.oldest_owed_fill == "2026-09-14"
    keys = {r["key"] for r in later.obligations}
    assert {"NYSE|2026-09-14", "XETR|2026-09-14",
            "NYSE|2026-09-21", "XETR|2026-09-21"} <= keys
    # The future fill is recorded but not yet due.
    future = [r for r in later.obligations if r["key"] == "NYSE|2026-09-21"][0]
    assert future["state"] == pd_.PENDING


def test_an_obligation_the_book_moved_past_is_missed_not_discharged(repo):
    _debt(repo, date(2026, 9, 16))                       # record 09-14
    _sleeve_docs(repo, {"A": "2026-09-21", "B": "2026-09-21",
                        "C": "2026-09-21", "D": "2026-09-21"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "later book")
    _git(repo, "push", "origin", "main")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.SUPERSEDED
    assert rec["missed"] is True
    assert rep.escalate is True, "a fill that was never recorded must escalate"


def test_a_discharged_obligation_stays_discharged(repo):
    _sleeve_docs(repo, {"A": "2026-09-14", "B": "2026-09-14",
                        "C": "2026-09-14", "D": "2026-09-14"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "post-fill book")
    _git(repo, "push", "origin", "main")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is False and rep.unknown is False
    assert all(r["state"] == pd_.DISCHARGED for r in rep.obligations)
    assert rep.evidence["sha"]


# ---------------------------------------------------------------------------
# 2. Evidence is remote, and specific
# ---------------------------------------------------------------------------
def test_a_local_commit_does_not_discharge_the_debt(repo):
    """THE REPRODUCED DEFECT. Evidence read from HEAD meant an unpushed
    commit - which is what a failed push leaves behind - counted as a
    publication and suppressed the retry that would have recovered it."""
    _sleeve_docs(repo, {"A": "2026-09-14", "B": "2026-09-14",
                        "C": "2026-09-14", "D": "2026-09-14"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m",
         "Local post-fill refresh 2026-09-16 (scheduled): panels current to "
         "2026-09-14, all steps OK")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True, "an unpushed commit discharged the debt"
    assert rep.evidence["unpushed_commits"] == 1
    _git(repo, "push", "origin", "main")
    assert _debt(repo, date(2026, 9, 16)).owed is False


def test_a_fresh_panel_on_an_unrelated_commit_is_not_publication(repo):
    """The subject names the CSP1 panel date. That is not the sleeve
    rebalance record, and it must not discharge anything on its own."""
    _git(repo, "commit", "--allow-empty", "-m",
         "Local post-fill refresh 2026-09-16 (scheduled): panels current to "
         "2026-09-15, all steps OK")
    _git(repo, "push", "origin", "main")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True
    # The subject IS reported, as context, and it is ahead of the fill.
    assert rep.evidence["latest_publication_subject"] == "2026-09-15"


def test_an_unresolvable_remote_ref_is_unknown_not_nothing_owed(repo):
    _git(repo, "remote", "remove", "origin")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.unknown is True
    assert rep.owed is False
    assert rep.should_run is True, "unknown must still do the work"
    assert any("origin/main" in p for p in rep.problems)


def test_an_unreadable_sleeve_record_is_unknown(repo):
    (repo / "data" / pd_.SLEEVE_FILES["C"]).write_text("{not json",
                                                       encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "broken record")
    _git(repo, "push", "origin", "main")
    rep = _debt(repo, date(2026, 9, 16))
    nyse = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert nyse["state"] == pd_.UNKNOWN
    assert rep.should_run is True


# ---------------------------------------------------------------------------
# 3. Venue awareness
# ---------------------------------------------------------------------------
def test_divergent_venue_fill_dates_are_two_obligations(repo):
    """Live shape from the Labor Day week: XETR filled Monday 7 September,
    NYSE Tuesday the 8th. ``next_fill.date`` is None across a divergence,
    and reading only it returned 'nothing owed' for both venues."""
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-08", "XETR": "2026-09-07"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"})),
        encoding="utf-8")
    obs, blocking, _ = pd_.observe_obligations(
        json.loads((repo / "data" / "live_targets.json").read_text()))
    assert not blocking
    assert {(o.venue, o.fill_date) for o in obs} == {
        ("NYSE", "2026-09-08"), ("XETR", "2026-09-07")}
    rep = _debt(repo, date(2026, 9, 9))
    # Both venues are published exactly to their own fill: nothing owed.
    assert rep.owed is False
    assert {r["key"] for r in rep.obligations} == {"NYSE|2026-09-08",
                                                   "XETR|2026-09-07"}


def test_one_venue_behind_owes_alone(repo):
    """Sleeve D's XETR fill unpublished while the NYSE sleeves are current."""
    _sleeve_docs(repo, {"A": "2026-09-14", "B": "2026-09-14",
                        "C": "2026-09-14", "D": "2026-09-07"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "us only")
    _git(repo, "push", "origin", "main")
    rep = _debt(repo, date(2026, 9, 16))
    states = {r["key"]: r["state"] for r in rep.obligations}
    assert states["NYSE|2026-09-14"] == pd_.DISCHARGED
    assert states["XETR|2026-09-14"] == pd_.OWED
    assert rep.owed is True


def test_a_sleeve_with_no_venue_map_is_unknown():
    obs, blocking, _ = pd_.observe_obligations(
        {"next_fill": {"by_venue": {"NYSE": "2026-09-14"}}})
    assert blocking and "sleeve/venue map" in blocking[0]
    state, reason = pd_.classify(obs[0], {}, date(2026, 9, 16))
    assert state == pd_.UNKNOWN


# ---------------------------------------------------------------------------
# 4. Malformed and missing state
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body", ["{not json", "[]", '"a string"', "null"])
def test_malformed_targets_are_unknown_not_nothing_owed(repo, body):
    (repo / "data" / "live_targets.json").write_text(body, encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.unknown is True
    assert rep.should_run is True


def test_a_missing_targets_file_still_carries_the_recorded_obligation(repo):
    _debt(repo, date(2026, 9, 16))                       # record it first
    (repo / "data" / "live_targets.json").unlink()
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True
    assert rep.unknown is True
    assert rep.oldest_owed_fill == "2026-09-14"


def test_a_corrupt_ledger_says_so_rather_than_forgetting_quietly(repo):
    led = repo / "logs" / "ob.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text("{{{", encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.unknown is True
    assert any("history was lost" in p for p in rep.problems)


def test_a_malformed_fill_date_is_blocking():
    obs, blocking, _ = pd_.observe_obligations(
        {"next_fill": {"by_venue": {"NYSE": "not-a-date"}},
         "sleeves": [{"sleeve": "A", "venue": "NYSE"}]})
    assert obs == []
    assert blocking and "not a calendar date" in blocking[0]


def test_the_ledger_write_is_atomic_and_bounded(tmp_path):
    p = tmp_path / "ob.json"
    book = {"schema": 1, "obligations": {
        f"NYSE|2026-{m:02d}-01": {"venue": "NYSE",
                                  "fill_date": f"2026-{m:02d}-01",
                                  "state": pd_.DISCHARGED, "sleeves": ["A"]}
        for m in range(1, 13)}}
    book["obligations"]["NYSE|2027-01-05"] = {"venue": "NYSE",
                                              "fill_date": "2027-01-05",
                                              "state": pd_.OWED,
                                              "sleeves": ["A"]}
    saved = pd_.save_ledger(p, book)
    assert saved is True
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"
    back, problems = pd_.load_ledger(p)
    assert not problems
    assert "NYSE|2027-01-05" in back["obligations"]


def test_eviction_drops_terminal_records_before_outstanding_ones(tmp_path):
    obligations = {f"NYSE|2026-01-{d:02d}": {"venue": "NYSE",
                                             "fill_date": f"2026-01-{d:02d}",
                                             "state": pd_.DISCHARGED,
                                             "sleeves": ["A"]}
                   for d in range(1, pd_.MAX_OBLIGATIONS + 5)}
    obligations["NYSE|2026-01-01"]["state"] = pd_.OWED
    p = tmp_path / "ob.json"
    pd_.save_ledger(p, {"schema": 1, "obligations": obligations})
    back, _ = pd_.load_ledger(p)
    assert len(back["obligations"]) == pd_.MAX_OBLIGATIONS
    assert "NYSE|2026-01-01" in back["obligations"], "evicted an owed record"


# ---------------------------------------------------------------------------
# 5. Date boundaries (house rule: one month, one year, minimum)
# ---------------------------------------------------------------------------
def test_month_boundary_fill_read_after_month_end(repo):
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-30"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 10, 2))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-30"][0]
    assert rec["state"] == pd_.OWED
    assert rec["age_days"] == 2                  # 30 Sep -> 2 Oct
    assert rep.escalate is False                 # grace is 3


def test_year_boundary_fill_read_in_the_new_year(repo):
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-12-29"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    rep = _debt(repo, date(2027, 1, 2))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-12-29"][0]
    assert rec["age_days"] == 4                  # 29 Dec -> 2 Jan
    assert rep.escalate is True                  # past a 3-day grace


def test_a_new_year_publication_discharges_an_old_year_fill(repo):
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-12-29"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    _sleeve_docs(repo, {"A": "2026-12-29", "B": "2026-12-29",
                        "C": "2026-12-29", "D": "2026-12-29"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "year end book")
    _git(repo, "push", "origin", "main")
    rep = _debt(repo, date(2027, 1, 2))
    assert rep.owed is False


def test_a_future_fill_is_pending_not_owed(repo):
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 19))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-21"][0]
    assert rec["state"] == pd_.PENDING
    assert rep.owed is False


# ---------------------------------------------------------------------------
# 6. Escalation is bounded
# ---------------------------------------------------------------------------
def test_escalation_is_once_per_obligation_per_day(repo):
    rep = _debt(repo, date(2026, 9, 20))
    assert rep.escalate is True
    due = pd_.escalation_due(rep, date(2026, 9, 20))
    assert set(due) == {"NYSE|2026-09-14", "XETR|2026-09-14"}
    pd_.mark_escalated(repo / "logs" / "ob.json", due, date(2026, 9, 20))
    again = _debt(repo, date(2026, 9, 20))
    assert pd_.escalation_due(again, date(2026, 9, 20)) == []
    assert pd_.escalation_due(again, date(2026, 9, 21)) == due


def test_escalation_stops_after_the_cap(repo):
    led = repo / "logs" / "ob.json"
    # Grace is 3 days from a 14 September fill, so the first
    # escalation day is the 18th.
    for day in range(18, 18 + pd_.MAX_ESCALATIONS):
        rep = _debt(repo, date(2026, 9, day))
        pd_.mark_escalated(led, pd_.escalation_due(rep, date(2026, 9, day)),
                           date(2026, 9, day))
    rep = _debt(repo, date(2026, 9, 25))
    assert rep.escalate is True, "the verdict must stay visible"
    assert pd_.escalation_due(rep, date(2026, 9, 25)) == [], \
        "an undischargeable obligation must stop mailing"


# ---------------------------------------------------------------------------
# 7. The subject contract with scheduled_refresh (kept: context, not evidence)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cadence,kind", [("post-fill", "post-fill"),
                                          ("weekend", "weekly")])
def test_subject_contract_matches_what_the_refresh_writes(cadence, kind):
    msg = sr.scheduled_commit_message(date(2026, 9, 16), date(2026, 9, 14),
                                      cadence)
    parsed = pd_.parse_publication(msg)
    assert parsed is not None, f"{msg!r} no longer parses"
    assert parsed == (cadence, date(2026, 9, 16), date(2026, 9, 14))
    assert f"Local {kind} refresh " in msg


@pytest.mark.parametrize("subject", [
    "Scanner daily build 2026-09-15",
    "Vendor availability probe 2026-09-15 03:10 UTC",
    "Local post-fill refresh 2026-09-16",
    "",
])
def test_unrelated_subjects_are_never_publications(subject):
    assert pd_.parse_publication(subject) is None


def test_the_real_incident_commit_parses():
    parsed = pd_.parse_publication(
        "Local weekly refresh 2026-09-13 (scheduled): panels current to "
        "2026-09-11, all steps OK")
    assert parsed == ("weekend", date(2026, 9, 13), date(2026, 9, 11))


# ---------------------------------------------------------------------------
# 8. Cadence
# ---------------------------------------------------------------------------
def test_the_weekend_cadence_carries_no_fill_debt_but_still_observes(repo):
    rep = _debt(repo, date(2026, 9, 16), cadence="weekend")
    assert rep.owed is False and rep.should_run is False
    assert {r["key"] for r in rep.obligations} == {"NYSE|2026-09-14",
                                                   "XETR|2026-09-14"}
    # and the post-fill pair sees what the weekend run recorded
    assert _debt(repo, date(2026, 9, 16)).owed is True


def test_cli_exit_codes(repo, capsys):
    assert pd_.main(["--repo", str(repo), "--asof", "2026-09-16",
                     "--no-persist"]) == 1
    assert pd_.main(["--repo", str(repo), "--asof", "2026-09-30",
                     "--no-persist"]) == 2
    _sleeve_docs(repo, {"A": "2026-09-14", "B": "2026-09-14",
                        "C": "2026-09-14", "D": "2026-09-14"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "book")
    _git(repo, "push", "origin", "main")
    assert pd_.main(["--repo", str(repo), "--asof", "2026-09-16",
                     "--no-persist"]) == 0
    _git(repo, "remote", "remove", "origin")
    assert pd_.main(["--repo", str(repo), "--asof", "2026-09-16",
                     "--no-persist"]) == 3, "unknown must not exit 0"


def test_current_debt_never_raises_on_a_hostile_tree(tmp_path):
    rep = pd_.current_debt(tmp_path, date(2026, 9, 16),
                           ledger=tmp_path / "logs" / "ob.json")
    assert rep.unknown is True and rep.owed is False and rep.should_run is True
