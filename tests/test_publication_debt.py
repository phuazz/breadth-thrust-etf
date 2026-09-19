"""The publication obligation must survive the thing it polices.

Every test below either pins a property the first version of the module
lacked, or pins a date-arithmetic boundary per the house rules. Python
datetime months are 1-indexed (January = 1).

PROVENANCE OF THE LABELS BELOW. Tests marked "DEFECT (predecessor)" pin a
defect of the UNCOMMITTED draft that preceded commit 47d1b3a. All three
modules are absent at 47d1b3a^, so those cases cannot be demonstrated as
behavioural failures against any commit: the draft existed only as untracked
working-tree files, and the reproductions were run against it in session on
2026-09-16 before it was overwritten. Tests marked "REPRODUCED 2026-09-16"
are different - they were reproduced against 47d1b3a itself, which is in the
history, and they fail there.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
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
    # logs/ is gitignored in the real repo, and the fixture must match: with
    # no .gitignore, `git add -A` sweeps the obligation ledger into a commit
    # and `git reset --hard` then destroys it, which looks exactly like the
    # module losing its own state.
    (work / ".gitignore").write_text("logs/\n", encoding="utf-8")
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
    """DEFECT (predecessor). next_fill=2026-09-21, published 2026-09-08,
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
    """DEFECT (predecessor). Evidence read from HEAD meant an unpushed
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
    state, reason, missed = pd_.classify(obs[0], {}, date(2026, 9, 16))
    assert state == pd_.UNKNOWN and missed is False


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


# ---------------------------------------------------------------------------
# 9. A MISS must be evidenced, not inferred (finding 2, 2026-09-16)
#
# ``latest_rebalance`` names only the LAST rebalance, so the current record
# cannot distinguish a fill that was published and then overtaken from one
# that was never published at all. Both of the benign paths below were
# reported as a missed publication before the history walk existed.
# ---------------------------------------------------------------------------
def _publish(repo, dates: dict[str, str], message: str) -> None:
    _sleeve_docs(repo, dates)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    _git(repo, "push", "origin", "main")


def test_an_engine_rerun_that_did_publish_the_fill_is_discharged(repo):
    """BENIGN PATH 1. A rerun through 22 September reconstructs 8, 14 and 21
    September and publishes a record naming the 21st. The 14th WAS recorded
    at the time, and the history says so."""
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _publish(repo, {k: "2026-09-14" for k in "ABCD"}, "post-fill 14 Sep")
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "engine rerun to 22 Sep")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.DISCHARGED
    assert rec.get("missed") is not True
    assert rep.escalate is False


def test_a_publication_made_while_unobserved_is_not_a_miss(repo):
    """BENIGN PATH 2. Both publications happened before anything watched.
    The 14 September obligation was never recorded as owed, so nothing can
    say it was missed."""
    _publish(repo, {k: "2026-09-14" for k in "ABCD"}, "post-fill 14 Sep")
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "post-fill 21 Sep")
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21", "XETR": "2026-09-21"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 22))          # first ever observation
    assert rep.escalate is False
    assert all(r.get("missed") is not True for r in rep.obligations)


def test_a_miss_is_asserted_only_when_it_was_observed_and_the_history_is_conclusive(repo):
    """THE GENUINE CASE. Observed owed after the fill, and no publication in
    the covered history ever carried it."""
    assert _debt(repo, date(2026, 9, 16)).owed is True      # observed owed
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "straight to 21 Sep")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.SUPERSEDED
    assert rec["missed"] is True
    assert rep.escalate is True
    assert all(h["conclusive"] for h in rec["history"].values())


def test_an_inconclusive_history_does_not_assert_a_miss(repo, monkeypatch):
    """Absence of an observation is not evidence of a miss. A truncated walk
    reports SUPERSEDED with missed false, and does not escalate."""
    assert _debt(repo, date(2026, 9, 16)).owed is True
    # Several publications that each MOVE the record, so the sleeve-file
    # walk really has more commits than the limit allows.
    for d in ("2026-09-18", "2026-09-19", "2026-09-21"):
        _publish(repo, {k: d for k in "ABCD"}, f"straight to {d}")
    real = pd_.publication_history

    def truncated(*a, **kw):
        kw["limit"] = 1
        return real(*a, **kw)
    monkeypatch.setattr(pd_, "publication_history", truncated)
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.SUPERSEDED
    assert rec["missed"] is False
    assert rep.escalate is False
    assert any("NOT established" in r for r in rep.problems)


def test_the_history_walk_reads_the_blob_as_published_at_the_time(repo):
    anchor = _git(repo, "rev-parse", "origin/main").stdout.strip()
    _publish(repo, {k: "2026-09-14" for k in "ABCD"}, "post-fill 14 Sep")
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "later")
    hist = pd_.publication_history(repo, "origin/main", "A", date(2026, 9, 14),
                                   since_sha=anchor)
    # Every value the record has CARRIED, not just the one it carries now.
    assert {"2026-09-14", "2026-09-21"} <= hist["dates"]
    assert hist["conclusive"] is True


def test_a_walk_without_an_anchor_can_never_establish_a_negative(repo):
    """A date-bounded walk can confirm a publication; it cannot prove that
    none exists, because a commit timestamp can put one outside the window."""
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "later")
    hist = pd_.publication_history(repo, "origin/main", "A", date(2026, 9, 14))
    assert hist["conclusive"] is False
    assert "no anchor" in hist["note"]


# ---------------------------------------------------------------------------
# 10. An authorised HOLD is not a missed fill (2026-09-16)
#
# component_release.verify admits a HOLD on any sleeve the sealed release
# names among held_sleeves, only with a reason, and only when it is
# risk-only. Such a sleeve is not going to trade
# the fill, so its rebalance record is not obliged to advance and counting it
# as unpublished would redefine a sanctioned decision as an operational miss.
# ---------------------------------------------------------------------------
ANCHOR = "2026-09-11"


def _targets_with_status(fills, venues, statuses, *, final=True,
                         anchor=ANCHOR):
    doc = _targets(fills, venues, {v: anchor for v in fills})
    doc["targets_final"] = final
    doc["as_of"] = anchor
    for sl in doc["sleeves"]:
        sl["status"] = statuses.get(sl["sleeve"], "READY")
        sl["decision_session"] = anchor
        sl["decision_session_for_fill"] = anchor
        if sl["status"] == "HOLD":
            sl["reason"] = "risk-only hold"
    return doc


def _write_marker(repo, *, anchor=ANCHOR, d_ready=False):
    """A component_release.json with the right FIELDS and no seal behind it -
    which is precisely what must NOT authorise anything."""
    (repo / "data" / "component_release.json").write_text(
        json.dumps({"anchor": anchor, "d_ready": d_ready, "schema": 1}),
        encoding="utf-8")


def _seal_release(monkeypatch, *, anchor=ANCHOR, d_ready=False, verified=True,
                  error="", held=("D",)):
    """Stand in for a release that component_release.verify ACCEPTS.

    The debt module's job is to require that verification and to read its
    verdict; re-running the release contract itself belongs with
    component_release. What is pinned here is that nothing short of a
    verified release authorises a HOLD.
    """
    monkeypatch.setattr(pd_, "release_authorisation",
                        lambda repo_root, now=None: {
                            "verified": verified, "anchor": anchor,
                            "d_ready": d_ready, "error": error,
                            # Which sleeves the seal AUTHORISES (2026-09-19).
                            # d_ready was a proxy for "D is held" and cannot
                            # express a held C, which is the common case.
                            "held_sleeves": list(held)})


def test_a_venue_whose_only_sleeve_is_on_hold_carries_no_debt(repo, monkeypatch):
    _seal_release(monkeypatch)
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"NYSE": "2026-09-14", "XETR": "2026-09-14"},
                             {"A": "NYSE", "B": "NYSE", "C": "NYSE",
                              "D": "XETR"}, {"D": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    states = {r["key"]: r["state"] for r in rep.obligations}
    assert states["XETR|2026-09-14"] == pd_.NOT_OBLIGED
    assert states["NYSE|2026-09-14"] == pd_.OWED
    xetr = [r for r in rep.obligations if r["key"] == "XETR|2026-09-14"][0]
    assert "authorised HOLD" in xetr["reason"]


def test_a_sleeve_ever_observed_ready_stays_obliged(repo, monkeypatch):
    """One-way. An authorised HOLD arriving after the sleeve was FINAL READY
    for this fill does not retire an obligation that was already real."""
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"}, {})),
        encoding="utf-8")
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _seal_release(monkeypatch)
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True, "an authorised HOLD retired a live obligation"


def test_a_sleeve_with_no_status_is_treated_as_obliged(repo):
    """Older books carry no status field. Obliged is the conservative read."""
    rep = _debt(repo, date(2026, 9, 16))          # fixture has no statuses
    assert rep.owed is True
    assert any("no READY/HOLD status" in p for p in rep.problems)


# ---------------------------------------------------------------------------
# 11. A failed ledger write is not a healthy empty history (finding 6)
# ---------------------------------------------------------------------------
def test_a_failed_ledger_write_is_unknown_not_clean(repo, monkeypatch):
    """REPRODUCED 2026-09-16 against 47d1b3a: with the write failing, the obligation lived
    only in a file that was never written, so advancing next_fill returned
    owed:false, unknown:false, problems:[] with publication still behind."""
    monkeypatch.setattr(pd_, "save_ledger", lambda *a, **kw: False)
    first = _debt(repo, date(2026, 9, 16))
    assert first.owed is True and first.unknown is True
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21", "XETR": "2026-09-21"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"})),
        encoding="utf-8")
    later = _debt(repo, date(2026, 9, 16))
    assert later.owed is False, "the fixture no longer loses the obligation"
    assert later.unknown is True, "a lost history was reported as health"
    assert later.should_run is True
    assert any("could not be written" in p for p in later.problems)
    assert later.evidence["ledger_written"] is False


def test_a_successful_write_records_that_it_succeeded(repo):
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.evidence["ledger_written"] is True
    assert rep.unknown is False


# ---------------------------------------------------------------------------
# 12. A skipped rebalance is owed, but says WHY retrying will not help
#
# run_portfolio drops a rebalance date whose decision session sits beyond the
# panel's validated_through, so a refresh can publish while the record stays
# behind the fill.
# ---------------------------------------------------------------------------
def test_a_publication_that_did_not_carry_the_fill_is_named_as_such(repo):
    assert _debt(repo, date(2026, 9, 16)).owed is True
    # The refresh publishes repeatedly, and the record never advances.
    for i in range(2):
        (repo / "data" / "panel.json").write_text(f'{{"run": {i}}}',
                                                  encoding="utf-8")
        _publish(repo, {"A": "2026-09-08", "B": "2026-09-08",
                        "C": "2026-09-08", "D": "2026-09-07"},
                 f"Local post-fill refresh publication {i}")
    rep = _debt(repo, date(2026, 9, 20))          # past the 3-day grace
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.OWED
    assert rec.get("published_without_recording") is True
    assert "did not rebalance" in rec["reason"]
    assert "retrying will not move it" in rec["reason"]


# ---------------------------------------------------------------------------
# 13. A conclusive NEGATIVE must be positively established (finding 1)
#
# All four reproduced against the second pass on 2026-09-16: each returned
# conclusive:true from evidence it did not have, and asserted a MISS.
# ---------------------------------------------------------------------------
def _anchor(repo) -> str:
    return _git(repo, "rev-parse", "origin/main").stdout.strip()


def test_a_carrying_publication_committed_before_the_fill_is_still_found(repo):
    """REPRODUCED 2026-09-16: the walk was bounded by COMMIT DATE, so a
    publication that did carry the fill but was committed earlier - clock
    skew, or a rebase rewriting committer dates - fell outside it and the
    obligation was reported missed."""
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _sleeve_docs(repo, {k: "2026-09-14" for k in "ABCD"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "post-fill 14 Sep",
         env={**__import__("os").environ,
              "GIT_COMMITTER_DATE": "2026-09-05T10:00:00+00:00",
              "GIT_AUTHOR_DATE": "2026-09-05T10:00:00+00:00"})
    _git(repo, "push", "origin", "main")
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "later book")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.DISCHARGED
    assert rep.escalate is False


def test_a_shallow_clone_can_never_establish_a_negative(repo, tmp_path):
    """REPRODUCED 2026-09-16: a shallow clone answered with the commits it
    happened to hold and the verdict called that conclusive."""
    anchor = _anchor(repo)
    _publish(repo, {k: "2026-09-14" for k in "ABCD"}, "post-fill 14 Sep")
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "later")
    shallow = tmp_path / "shallow"
    origin = str(tmp_path / "origin.git").replace("\\", "/")
    _git(tmp_path, "clone", "--depth=1", f"file:///{origin}", str(shallow))
    hist = pd_.publication_history(shallow, "origin/main", "A",
                                   date(2026, 9, 14), since_sha=anchor)
    assert hist["shallow"] is True
    assert hist["conclusive"] is False
    assert "shallow" in hist["note"]


def test_unreadable_blobs_can_never_establish_a_negative(repo, monkeypatch):
    """REPRODUCED 2026-09-16: every blob read failing produced an empty date
    set, which then read as 'the fill was never published'."""
    anchor = _anchor(repo)
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "later")
    monkeypatch.setattr(pd_, "_read_blobs",
                        lambda repo_root, specs: {s: None for s in specs})
    hist = pd_.publication_history(repo, "origin/main", "A", date(2026, 9, 14),
                                   since_sha=anchor)
    assert hist["unreadable"] > 0
    assert hist["conclusive"] is False
    assert "could not be read" in hist["note"]


def test_a_rewritten_ref_can_never_establish_a_negative(repo):
    """REPRODUCED 2026-09-16: a force-push that dropped the publication
    carrying the fill was invisible, and the obligation was reported missed
    with escalation."""
    assert _debt(repo, date(2026, 9, 16)).owed is True
    keep = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _publish(repo, {k: "2026-09-14" for k in "ABCD"}, "post-fill 14 Sep")
    _git(repo, "fetch", "origin")            # this clone SAW that tip
    _git(repo, "reset", "--hard", keep)
    _sleeve_docs(repo, {k: "2026-09-21" for k in "ABCD"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "rewritten later book")
    _git(repo, "push", "--force", "origin", "main")
    _git(repo, "fetch", "origin")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["state"] == pd_.SUPERSEDED
    assert rec["missed"] is False, "a rewritten history established a miss"
    assert rep.escalate is False
    assert any(h["rewritten"] for h in rec["history"].values())


def test_a_healthy_history_still_establishes_a_negative(repo):
    """The repairs must not make every negative inconclusive."""
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "straight to 21 Sep")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert rec["missed"] is True
    assert all(h["conclusive"] for h in rec["history"].values())


def test_the_cache_is_invalid_when_the_SLEEVE_SET_changes(repo, monkeypatch):
    """REPRODUCED 2026-09-16: the key was (anchor, tip) alone, so a sleeve
    added to an obligation without either sha moving was served a cached
    history that did not contain it. The verdict then went inconclusive on
    stale evidence - safe here, but the claim that anchor and tip determine
    validity was simply false."""
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"NYSE": "2026-09-14"}, {"A": "NYSE"}, {})),
        encoding="utf-8")
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "straight past it")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert sorted(rec["history"]) == ["A"]
    assert rec["missed"] is True

    # A second sleeve joins the obligation. Neither sha moves.
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"NYSE": "2026-09-14"},
                             {"A": "NYSE", "B": "NYSE"}, {})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"][0]
    assert sorted(rec["history"]) == ["A", "B"], \
        "a stale cache answered for a sleeve it had never walked"
    assert rec["history_cache"]["sleeves"] == ["A", "B"]


def test_the_rewrite_check_runs_only_when_a_negative_is_at_stake(repo,
                                                                 monkeypatch):
    """The reflog walk costs about a second on the real repository. It
    decides nothing when the fill WAS found: positive evidence stands
    whatever else the ref has done."""
    calls = []
    real = pd_.ref_was_rewritten
    monkeypatch.setattr(pd_, "ref_was_rewritten",
                        lambda *a, **kw: calls.append(1) or real(*a, **kw))
    anchor = _anchor(repo)
    _publish(repo, {k: "2026-09-12" for k in "ABCD"}, "a publication after it")

    found = pd_.publication_history(repo, "origin/main", "A",
                                    date(2026, 9, 14), since_sha=anchor,
                                    confirm_absent="2026-09-12")
    assert "2026-09-12" in found["dates"]
    assert calls == [], "the reflog was walked although the date was found"

    pd_.publication_history(repo, "origin/main", "A", date(2026, 9, 14),
                            since_sha=anchor, confirm_absent="2026-09-14")
    assert len(calls) == 1, "a negative was asserted without the rewrite check"


def test_the_history_walk_is_memoised_against_the_tip_it_rests_on(repo,
                                                                  monkeypatch):
    """A retained obligation repeats this walk on every firing, before the
    retry budget is even consulted. The result is cached on (anchor, tip)
    and invalidated by any change to either."""
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "straight to 21 Sep")
    calls = []
    real = pd_.publication_history

    def counted(*a, **kw):
        calls.append(a[2])
        return real(*a, **kw)
    monkeypatch.setattr(pd_, "publication_history", counted)

    _debt(repo, date(2026, 9, 22))
    first = len(calls)
    assert first > 0
    _debt(repo, date(2026, 9, 22))
    assert len(calls) == first, "the walk was repeated on an unchanged tip"

    _publish(repo, {k: "2026-09-22" for k in "ABCD"}, "the tip moves")
    _debt(repo, date(2026, 9, 23))
    assert len(calls) > first, "a moved tip did not invalidate the cache"


# ---------------------------------------------------------------------------
# 14. Ledger continuity across a failure AND its recovery (finding 3)
# ---------------------------------------------------------------------------
def test_a_recovered_ledger_still_reports_the_gap(repo, monkeypatch):
    """REPRODUCED 2026-09-16: the second pass caught a write that was
    failing, but once writes recovered the ledger was a clean empty file and
    the verdict went back to owed:false, unknown:false, problems:[] with the
    publication still behind."""
    led = repo / "logs" / "ob.json"
    monkeypatch.setattr(pd_, "save_ledger", lambda *a, **kw: False)
    assert _debt(repo, date(2026, 9, 16)).owed is True
    monkeypatch.undo()

    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21", "XETR": "2026-09-21"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.unknown is True, "a lost history recovered into a clean one"
    assert rep.should_run is True
    assert any("lost writes" in p for p in rep.problems)
    assert pd_.continuity_path(led).exists()


def test_a_gap_forbids_asserting_a_miss_over_the_fills_it_covers(repo,
                                                                 monkeypatch):
    """The lost evidence is exactly ``observed_owed_after_fill``, so a miss
    over that window cannot be established either way."""
    monkeypatch.setattr(pd_, "save_ledger", lambda *a, **kw: False)
    _debt(repo, date(2026, 9, 16))
    monkeypatch.undo()
    _publish(repo, {k: "2026-09-21" for k in "ABCD"}, "straight to 21 Sep")
    rep = _debt(repo, date(2026, 9, 22))
    rec = [r for r in rep.obligations if r["key"] == "NYSE|2026-09-14"]
    if rec:                                   # re-observed after the gap
        assert rec[0].get("missed") is not True
    assert rep.escalate is False


def test_a_gap_ages_out_of_the_verdict_but_not_out_of_the_record(repo):
    led = repo / "logs" / "ob.json"
    pd_.record_write_outcome(led, False,
                             datetime(2026, 7, 1, tzinfo=timezone.utc))
    pd_.record_write_outcome(led, True,
                             datetime(2026, 7, 1, 1, tzinfo=timezone.utc))
    state = pd_.continuity_state(led)
    assert state["gaps"] and state["gaps"][0]["recovered_utc"]
    assert pd_.relevant_gaps(state, date(2026, 7, 10)), "too soon to forget"
    assert not pd_.relevant_gaps(state, date(2026, 9, 16)), "should have aged out"
    assert pd_.continuity_state(led)["gaps"], "the record must survive"


def test_a_total_write_failure_does_not_recover_into_a_clean_history(
        repo, monkeypatch):
    """REPRODUCED 2026-09-16: the continuity sidecar shares ``_atomic_write``
    and its directory with the ledger, so a real disk or permission failure
    takes BOTH. Once writes recovered, ledger and sidecar were empty and the
    verdict read owed:false, unknown:false, gaps:[] with a publication still
    behind. The surviving signal is written by other code, to other files."""
    (repo / "logs").mkdir(exist_ok=True)
    (repo / "logs" / "run_outcomes.jsonl").write_text(
        json.dumps({"asof": "2026-09-13T01:00:00", "cadence": "post-fill",
                    "exit_code": 0, "outcome": "green"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(pd_, "_atomic_write", lambda *a, **kw: False)
    monkeypatch.setattr(Path, "write_text",
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            OSError("disk full")))
    assert _debt(repo, date(2026, 9, 16)).unknown is True
    monkeypatch.undo()

    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21", "XETR": "2026-09-21"},
        {"A": "NYSE", "B": "NYSE", "C": "NYSE", "D": "XETR"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.unknown is True, "a total write failure recovered into health"
    assert rep.should_run is True
    assert any("history was lost" in p for p in rep.problems)


# ---------------------------------------------------------------------------
# 17. The HOLD authorisation must survive the post-fill clock (finding 1)
# ---------------------------------------------------------------------------
def _release_verified_at(monkeypatch, sealed_at, *, anchor=ANCHOR):
    """Stand in for component_release.verify, recording the CLOCK it is
    handed. The defect was entirely about which clock that is."""
    seen = []

    def fake_verify(root, now, committed=False):
        seen.append(now)
        from nyse_sessions import week_final_anchor
        if week_final_anchor(now).isoformat() != anchor:
            raise ValueError("wrong week or executed book")
        return {"anchor": anchor, "d_ready": False, "held_sleeves": ["D"],
                "identity": "x", "sealed_at": sealed_at}

    import component_release
    monkeypatch.setattr(component_release, "verify", fake_verify)
    return seen


def test_the_release_is_verified_at_the_clock_it_was_SEALED_at(repo,
                                                               monkeypatch):
    """REPRODUCED 2026-09-16 on the live repository: verify validates the
    book against the clock it is handed, so the release verified through
    Monday 14 September and was REFUSED from Tuesday the 15th - exactly
    when the post-fill pair runs. Sleeve D would have carried a false debt
    on every post-fill firing, undischargeable by any publication."""
    sealed = "2026-09-13T01:22:23+00:00"
    (repo / "data" / "component_release.json").write_text(
        json.dumps({"anchor": ANCHOR, "d_ready": False, "sealed_at": sealed}),
        encoding="utf-8")
    seen = _release_verified_at(monkeypatch, sealed)

    # Tuesday after the Monday close: the old code refused here.
    out = pd_.release_authorisation(repo, datetime(2026, 9, 15, 1, 0,
                                                   tzinfo=timezone.utc))
    assert out["verified"] is True
    assert out["verified_at"].startswith("2026-09-13")
    assert seen[-1].isoformat().startswith("2026-09-13")


@pytest.mark.parametrize("when", [
    datetime(2026, 9, 14, 21, 30, tzinfo=timezone.utc),   # Monday after close
    datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc),     # post-fill Tuesday
    datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc),     # post-fill Wednesday
    datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),    # the week turns
    datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc),     # the week after
])
def test_the_authorisation_holds_across_the_week_turn(repo, monkeypatch, when):
    sealed = "2026-09-13T01:22:23+00:00"
    (repo / "data" / "component_release.json").write_text(
        json.dumps({"anchor": ANCHOR, "d_ready": False, "sealed_at": sealed}),
        encoding="utf-8")
    _release_verified_at(monkeypatch, sealed)
    assert pd_.release_authorisation(repo, when)["verified"] is True


def test_a_seal_claiming_the_FUTURE_is_refused(repo, monkeypatch):
    """The timestamp is inside the body the identity hash covers, so a lie
    fails the hash - but a clock far ahead is refused before that, cheaply
    and without reading anything else."""
    (repo / "data" / "component_release.json").write_text(
        json.dumps({"anchor": ANCHOR, "d_ready": False,
                    "sealed_at": "2027-01-01T00:00:00+00:00"}),
        encoding="utf-8")
    seen = _release_verified_at(monkeypatch, "2027-01-01T00:00:00+00:00")
    out = pd_.release_authorisation(repo, datetime(2026, 9, 16, 1, 0,
                                                   tzinfo=timezone.utc))
    assert out["verified"] is False
    assert "in the future" in out["error"]
    assert seen == [], "a future seal was still handed to verify"


def test_an_OLD_release_stops_exempting_even_when_the_book_agrees(repo,
                                                                  monkeypatch):
    """REPRODUCED 2026-09-17: verifying at ``sealed_at`` made an old release
    valid for ever, and the anchor comparison could not catch it because
    live_targets.json had frozen on the SAME anchor - two stale files
    agreeing perfectly. A release sealed on 13 September still exempted
    sleeve D in March."""
    _seal_release(monkeypatch, anchor="2026-09-11")
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")

    # A clone meeting the stale pair for the first time grants nothing.
    stale = _debt(repo, date(2026, 10, 15))      # anchor + 34 days
    rec = [r for r in stale.obligations if r["key"] == "XETR|2026-09-14"][0]
    assert rec["state"] != pd_.NOT_OBLIGED, \
        "a month-old release still exempted the sleeve"
    assert any("frozen book, not a current decision" in p
               for p in stale.problems)
    assert any("stopped moving" in p for p in stale.problems)
    assert stale.unknown is True


def test_an_exemption_GRANTED_while_fresh_belongs_to_its_fill(repo,
                                                              monkeypatch):
    """The deliberate other side. An authorised HOLD is a decision about ONE
    fill: sleeve D was never going to trade it, so it owes no publication
    for it, and that fact does not decay with the clock. What the freshness
    bound stops is granting a NEW exemption from an old release; it does not
    revoke one already granted. The frozen book is still reported, so the
    state is not silently clean."""
    _seal_release(monkeypatch, anchor="2026-09-11")
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    fresh = _debt(repo, date(2026, 9, 16))       # anchor + 5 days
    assert [r["state"] for r in fresh.obligations] == [pd_.NOT_OBLIGED]

    later = _debt(repo, date(2026, 10, 15))
    rec = [r for r in later.obligations if r["key"] == "XETR|2026-09-14"][0]
    assert rec["state"] == pd_.NOT_OBLIGED, "the granted exemption was revoked"
    assert later.unknown is True, "a frozen book read as clean"
    assert any("stopped moving" in p for p in later.problems)


@pytest.mark.parametrize("asof,exempt", [
    (date(2026, 9, 11), True),      # the decision Friday itself
    (date(2026, 9, 14), True),      # the Monday fill
    (date(2026, 9, 16), True),      # the post-fill pair
    (date(2026, 9, 18), True),      # still inside the window
    (date(2026, 9, 21), True),      # exactly at the bound
    (date(2026, 9, 22), False),     # the next decision week has been and gone
    (date(2026, 9, 25), False),
])
def test_the_exemption_expires_across_the_week_turn(repo, monkeypatch, asof,
                                                    exempt):
    """An exemption is granted for ONE decision week. Past the bound it has
    to be granted again, from a release for the current week."""
    _seal_release(monkeypatch, anchor="2026-09-11")
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    ok, why = pd_.hold_is_authorised(
        {"sleeve": "D", "status": "HOLD", "reason": "risk-only"},
        "2026-09-11",
        {"verified": True, "anchor": "2026-09-11", "d_ready": False,
         "held_sleeves": ["D"]}, asof)
    assert ok is exempt, why
    if not exempt:
        assert "frozen book" in why


def test_an_unreadable_release_anchor_cannot_be_aged_and_is_refused():
    ok, why = pd_.hold_is_authorised(
        {"sleeve": "D", "status": "HOLD", "reason": "risk-only"}, None,
        {"verified": True, "anchor": "not-a-date", "d_ready": False,
         "held_sleeves": ["D"]}, date(2026, 9, 16))
    assert ok is False and "age cannot be established" in why


def test_staleness_is_measured_PER_VENUE(repo):
    """REPRODUCED 2026-09-17: the detector took the maximum fill across
    venues, so a NYSE fill that kept advancing hid an XETR fill that had
    stopped a month earlier - exactly the case the repair exists for, since
    sleeve D is the venue that holds and its exemption is retained for its
    original fill."""
    mixed = {"next_fill": {"by_venue": {"NYSE": "2026-10-12",
                                        "XETR": "2026-09-14"}}}
    frozen, why = pd_.book_looks_frozen(mixed, date(2026, 10, 15))
    assert frozen is True
    assert "XETR" in why and "2026-09-14" in why
    assert "NYSE" not in why, "a venue that is advancing was named as stale"

    stale = pd_.frozen_venues(mixed, date(2026, 10, 15))
    assert [s["venue"] for s in stale] == ["XETR"]
    assert stale[0]["age_days"] == 31

    both = {"next_fill": {"by_venue": {"NYSE": "2026-10-12",
                                       "XETR": "2026-10-12"}}}
    assert pd_.frozen_venues(both, date(2026, 10, 15)) == []


def test_a_frozen_venue_is_unknown_even_while_the_other_advances(repo):
    """End to end: a fresh, granted D hold, then NYSE advances and XETR
    stops. The exemption is retained for its own fill - that is deliberate -
    and the frozen venue is what makes the state visible."""
    def write(nyse_fill, nyse_decision, statuses=("READY",) * 4):
        (repo / "data" / "live_targets.json").write_text(json.dumps({
            "next_fill": {"by_venue": {"NYSE": nyse_fill, "XETR": "2026-09-14"},
                          "decision_by_venue": {"NYSE": nyse_decision,
                                                "XETR": "2026-09-11"}},
            "targets_final": True, "as_of": nyse_decision,
            "sleeves": [
                {"sleeve": s, "venue": "NYSE", "status": "READY",
                 "decision_session": nyse_decision,
                 "decision_session_for_fill": nyse_decision}
                for s in "ABC"]
            + [{"sleeve": "D", "venue": "XETR", "status": statuses[3],
                "reason": "risk-only", "decision_session": "2026-09-11",
                "decision_session_for_fill": "2026-09-11"}]}), encoding="utf-8")

    # Week one: D is held under a release that verifies, and is exempt.
    import unittest.mock as _mock
    with _mock.patch.object(pd_, "release_authorisation",
                            lambda root, now=None: {
                                "verified": True, "anchor": "2026-09-11",
                                "d_ready": False, "held_sleeves": ["D"]}):
        write("2026-09-14", "2026-09-11", ("READY",) * 3 + ("HOLD",))
        granted = _debt(repo, date(2026, 9, 16))
    states = {r["key"]: r["state"] for r in granted.obligations}
    assert states["XETR|2026-09-14"] == pd_.NOT_OBLIGED
    assert granted.unknown is False

    # Weeks later: NYSE keeps moving, XETR does not.
    write("2026-10-12", "2026-10-09", ("READY",) * 3 + ("HOLD",))
    later = _debt(repo, date(2026, 10, 15))
    states = {r["key"]: r["state"] for r in later.obligations}
    assert states["XETR|2026-09-14"] == pd_.NOT_OBLIGED, \
        "the granted exemption was revoked"
    assert later.unknown is True, "a frozen XETR read as clean behind NYSE"
    assert any("has not advanced for XETR" in p for p in later.problems)
    assert [n["venue"] for n in later.notices] == ["XETR"]


def test_a_frozen_venue_raises_a_bounded_deduplicated_notice(repo):
    """A frozen venue owes nothing, so it can never escalate. Before this it
    produced catch-up work and a log line that nothing read.

    The fill here is DISCHARGED, so nothing is overdue and the only thing
    wrong with the state is that the book stopped moving - which is exactly
    the condition that used to reach no one."""
    _publish(repo, {k: "2026-09-14" for k in "ABCD"}, "post-fill 14 Sep")
    (repo / "data" / "live_targets.json").write_text(json.dumps({
        "next_fill": {"by_venue": {"XETR": "2026-09-14"},
                      "decision_by_venue": {"XETR": "2026-09-11"}},
        "targets_final": True, "as_of": "2026-09-11",
        "sleeves": [{"sleeve": "D", "venue": "XETR", "status": "READY",
                     "decision_session": "2026-09-11",
                     "decision_session_for_fill": "2026-09-11"}]}),
        encoding="utf-8")
    led = repo / "logs" / "ob.json"
    rep = _debt(repo, date(2026, 10, 15))
    assert rep.escalate is False, "a frozen book is not an overdue publication"
    assert [n["kind"] for n in rep.notices] == ["frozen_book"]

    on = date(2026, 10, 15)
    due = pd_.notices_due(rep, led, on)
    assert [n["key"] for n in due] == ["frozen_book|XETR|2026-09-14"]
    pd_.mark_notified(led, [n["key"] for n in due], on)
    assert pd_.notices_due(rep, led, on) == [], "it mailed twice in one day"
    assert pd_.notices_due(rep, led, date(2026, 10, 16)), "it never mails again"

    for i in range(pd_.MAX_NOTICES + 2):
        d = date(2026, 10, 16 + i)
        pd_.mark_notified(led, [n["key"] for n in pd_.notices_due(rep, led, d)], d)
    assert pd_.notices_due(rep, led, date(2026, 11, 20)) == [], \
        "an unfixed frozen book mails for ever"
    assert _debt(repo, date(2026, 11, 20)).notices, \
        "the condition must stay in every verdict even once it stops mailing"


def _NOTICE_REPORT(key: str, venue: str = "XETR", fill: str = "2026-09-14"):
    """A DebtReport carrying exactly one frozen-book notice.

    The notice bounds are properties of the ledger, not of a git fixture, so
    the tests below drive them directly rather than freezing a repository.
    """
    return pd_.DebtReport(
        owed=False, unknown=True, escalate=False, cadence="post-fill",
        asof="2026-10-15", oldest_owed_fill=None, age_days=None, grace_days=2,
        notices=({"kind": "frozen_book", "key": key, "venue": venue,
                  "fill": fill, "age_days": 31,
                  "detail": f"{venue} has stopped advancing"},))


def test_only_a_delivered_notice_spends_the_delivered_budget(tmp_path):
    """MAX_NOTICES caps how often an OPERATOR was told, not how often the
    mailer was invoked (2026-09-17, eighth review). An unconfigured channel -
    the incident's own state - spent the budget in silence."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-09-14"
    for day in range(1, pd_.MAX_NOTICES + 3):
        d = date(2026, 10, day)
        assert pd_.record_notice_attempt(led, [key], d) is True
        pd_.record_notice_delivery(led, [key], d, pd_.NOTICE_UNCONFIRMED)
    rec = pd_.load_ledger(led)[0]["notices"][key]
    assert rec["count"] == 0, "a channel that told nobody spent the budget"
    assert rec["attempts"] == pd_.MAX_NOTICES + 2
    assert "notified_on" not in rec, "it claimed an operator was told"
    # And a delivery still spends it.
    d = date(2026, 10, 20)
    pd_.record_notice_attempt(led, [key], d)
    pd_.record_notice_delivery(led, [key], d, pd_.NOTICE_DELIVERED)
    assert pd_.load_ledger(led)[0]["notices"][key]["count"] == 1


def test_a_channel_that_never_works_stops_being_retried(tmp_path):
    """The attempt budget is looser than the delivered one, but it exists: a
    mailer that refuses for ever must not be retried for ever."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-09-14"
    rep = _NOTICE_REPORT(key)
    for day in range(pd_.MAX_NOTICE_ATTEMPTS + 4):
        d = date(2026, 10, 1) + timedelta(days=day)
        for n in pd_.notices_due(rep, led, d):
            pd_.record_notice_attempt(led, [n["key"]], d)
            pd_.record_notice_delivery(led, [n["key"]], d,
                                       pd_.NOTICE_UNCONFIRMED)
    rec = pd_.load_ledger(led)[0]["notices"][key]
    assert rec["attempts"] == pd_.MAX_NOTICE_ATTEMPTS
    assert pd_.notices_due(rep, led, date(2027, 1, 1)) == []


def test_one_attempt_a_day_whatever_the_attempt_achieved(tmp_path):
    """What bounds an HOURLY schedule is the attempt stamp, not the delivery
    count: twelve firings against a dead channel used to send twelve mails."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-09-14"
    rep = _NOTICE_REPORT(key)
    d = date(2026, 10, 15)
    sends = 0
    for _ in range(12):
        for n in pd_.notices_due(rep, led, d):
            sends += 1
            pd_.record_notice_attempt(led, [n["key"]], d)
            pd_.record_notice_delivery(led, [n["key"]], d,
                                       pd_.NOTICE_UNCONFIRMED)
    assert sends == 1, "an hourly schedule mailed hourly"


def test_a_notice_whose_dedupe_cannot_be_written_is_refused_not_sent(tmp_path,
                                                                    monkeypatch):
    """``record_notice_attempt`` reports the failure BEFORE anything is sent,
    so the caller can withhold. Without it there was no bound at all."""
    led = tmp_path / "ob.json"
    monkeypatch.setattr(pd_, "_atomic_write", lambda *a, **kw: False)
    assert pd_.record_notice_attempt(led, ["k"], date(2026, 10, 15)) is False


def test_the_read_back_covers_the_notices_not_only_the_obligations(tmp_path,
                                                                   monkeypatch):
    """The fifth review made the read-back compare CONTENT; the seventh pass
    then added a second semantic map and left it unverified, so a write that
    silently did nothing returned True with the alert budget unchanged."""
    led = tmp_path / "ob.json"
    obs = {"NYSE|2026-09-14": {"state": "owed"}}
    assert pd_.save_ledger(led, {"obligations": obs,
                                 "notices": {"k": {"count": 3}}}) is True
    monkeypatch.setattr(pd_, "_atomic_write", lambda *a, **kw: True)  # no-op
    assert pd_.save_ledger(led, {"obligations": obs,
                                 "notices": {"k": {"count": 4}}}) is False, \
        "the count the cap depends on was not written and the writer said " \
        "it was"
    # updated_utc is a stamp, not something the next run reasons from, and a
    # comparison that included it could never succeed.
    monkeypatch.undo()
    assert pd_.save_ledger(led, {"obligations": obs,
                                 "notices": {"k": {"count": 4}}}) is True


def test_a_malformed_notice_record_is_named_and_suppressed_not_restarted(
        tmp_path):
    """Dropping it quietly handed the key a fresh budget of MAX_NOTICES, so a
    corrupt record was a way to re-arm the mailer."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-09-14"
    led.write_text(json.dumps({"schema": pd_.LEDGER_SCHEMA, "obligations": {},
                               "notices": {key: "sent five times"}}),
                   encoding="utf-8")
    book, obligation_problems = pd_.load_ledger(led)
    assert obligation_problems == [], \
        "an alert-dedupe record must not turn the publication verdict UNKNOWN"
    assert set(book["notices_unreadable"]) == {key}
    assert any(key in p and "suppressed rather than restarted" in p
               for p in book["notice_problems"])
    assert pd_.notices_due(_NOTICE_REPORT(key), led, date(2026, 10, 15)) == [], \
        "an unreadable send history was read as a fresh budget"
    # The evidence has to survive a save, or the next load reports nothing.
    assert pd_.save_ledger(led, book) is True
    assert set(pd_.load_ledger(led)[0]["notices_unreadable"]) == {key}


def test_the_malformed_notice_reaches_the_verdict_without_blocking_it(repo):
    """Reported, not escalated: it says nothing about whether a publication
    is owed, and blocking on it would put every firing through a refresh."""
    led = repo / "logs" / "ob.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({"schema": pd_.LEDGER_SCHEMA, "obligations": {},
                               "notices": {"frozen_book|XETR|2026-09-14": 7}}),
                   encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert any("cannot be read" in p for p in rep.problems), \
        "the damaged send history reached nobody"
    assert rep.unknown is False, "an alert record made the publication " \
                                 "verdict unreadable"


def test_the_notices_map_is_bounded_by_when_the_condition_was_last_seen(tmp_path):
    """MAX_NOTICES bounds sends per key; this bounds the keys, which grow as
    venues and fills accumulate.

    The eighth pass evicted by BUDGET and the ninth corrected it to evict by
    CONDITION: what goes is what has not been reported for
    NOTICE_RETENTION_DAYS, spent or not, because a spent record is the
    tombstone that keeps its condition quiet."""
    led = tmp_path / "ob.json"
    newest = date(2027, 1, 5)
    # Well beyond the window: these conditions stopped being reported.
    old = {f"frozen_book|XETR|{(newest - timedelta(days=200 + i)).isoformat()}":
           {"count": pd_.MAX_NOTICES,
            "last_seen": (newest - timedelta(days=200 + i)).isoformat()}
           for i in range(60)}
    # Inside the window: still being reported, spent budget or not.
    recent = {f"frozen_book|NYSE|{(newest - timedelta(days=i)).isoformat()}":
              {"count": pd_.MAX_NOTICES, "attempts": 1,
               "last_seen": (newest - timedelta(days=i)).isoformat()}
              for i in range(5)}
    assert pd_.save_ledger(led, {"obligations": {},
                                 "notices": {**old, **recent},
                                 "notices_observed_on": newest.isoformat()
                                 }) is True
    back = pd_.load_ledger(led)[0]["notices"]
    assert set(recent) <= set(back), "a condition seen this week was evicted"
    # Evicted down to the bound, oldest first, and nothing beyond that.
    assert len(back) == pd_.MAX_NOTICE_KEYS
    oldest = f"frozen_book|XETR|{(newest - timedelta(days=259)).isoformat()}"
    assert oldest not in back, "the oldest resolved condition was retained"

    # And when everything is recent the map stays oversized and SAYS so,
    # rather than dropping a record that is still silencing its condition.
    crowd = {f"frozen_book|NYSE|2027-02-{d:02d}":
             {"count": pd_.MAX_NOTICES, "last_seen": "2027-02-01"}
             for d in range(1, pd_.MAX_NOTICE_KEYS + 6)}
    led2 = tmp_path / "ob2.json"
    assert pd_.save_ledger(led2, {"obligations": {}, "notices": crowd,
                                  "notices_observed_on": "2027-02-01"}) is True
    book2, _ = pd_.load_ledger(led2)
    assert len(book2["notices"]) == len(crowd)
    assert any("exceed the retention bound" in p
               for p in book2["notice_problems"])


def test_an_evicted_exhausted_notice_is_not_due_again(tmp_path):
    """The eighth pass evicted EXHAUSTED records first, on the reasoning that
    a spent record can no longer alert. The spent count IS what stops it: the
    next firing found no record, read the still-frozen venue as new, and
    re-armed the whole budget (2026-09-17, ninth review)."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-08-14"
    spent = {key: {"count": pd_.MAX_NOTICES, "attempts": pd_.MAX_NOTICES,
                   "last_seen": "2026-10-15"}}
    filler = {f"frozen_book|NYSE|2026-{m:02d}-{d:02d}":
              {"count": pd_.MAX_NOTICES, "last_seen": f"2026-{m:02d}-{d:02d}"}
              for m in range(1, 13) for d in range(1, 29)}
    assert pd_.save_ledger(led, {"obligations": {},
                                 "notices": {**filler, **spent}}) is True
    # The condition is still live, so the scheduler stamps it every firing.
    assert pd_.record_notice_conditions(led, [key], date(2026, 10, 15)) is True
    back = pd_.load_ledger(led)[0]["notices"]
    assert key in back, "the tombstone that silences the condition was evicted"
    assert back[key]["count"] == pd_.MAX_NOTICES, "the count was reset"
    assert pd_.notices_due(_NOTICE_REPORT(key), led, date(2026, 10, 16)) == [], \
        "an exhausted notice became due again"


def test_a_future_dated_record_cannot_evict_valid_tombstones(tmp_path):
    """Retention must not be a function of the records being retained
    (2026-09-17, tenth review). The ninth pass anchored the floor on the
    MAXIMUM last_seen in the map, so one dict-shaped record carrying
    "2099-01-01" - which passes every validity check this module has - moved
    the floor to 2098, made every genuine record look resolved, evicted 17
    live tombstones, and re-armed each with a fresh budget."""
    led = tmp_path / "ob.json"
    live = {f"frozen_book|XETR|2026-10-{d:02d}":
            {"count": pd_.MAX_NOTICES, "last_seen": "2026-10-15"}
            for d in range(1, 29)}
    live.update({f"frozen_book|NYSE|2026-10-{d:02d}":
                 {"count": pd_.MAX_NOTICES, "last_seen": "2026-10-15"}
                 for d in range(1, 29)})
    poison = "frozen_book|XETR|poison"
    live[poison] = {"count": 0, "last_seen": "2099-01-01"}
    assert pd_.save_ledger(led, {"obligations": {}, "notices": live,
                                 "notices_observed_on": "2026-10-15"}) is True
    back = pd_.load_ledger(led)[0]["notices"]
    survivors = {k for k in back if k != poison}
    assert len(survivors) == len(live) - 1, \
        f"{len(live) - 1 - len(survivors)} live tombstones were evicted by a " \
        f"date nobody verified"
    # And the spent notices they were silencing stay silent.
    for key in list(survivors)[:3]:
        assert pd_.notices_due(_NOTICE_REPORT(key), led,
                               date(2026, 10, 16)) == [], \
            f"{key} was re-armed"
    # The future-dated record itself is not evictable either: a date after
    # the anchor is evidence the date is wrong, not that it resolved.
    assert poison in back


def test_a_stale_ledger_is_not_pruned_merely_because_it_was_opened(tmp_path):
    """Without a trusted observation from THIS run, nothing is pruned. That
    is the case the ninth pass's relative floor existed to protect, and it is
    protected now without trusting the records."""
    led = tmp_path / "ob.json"
    old = {f"frozen_book|XETR|2020-{m:02d}-{d:02d}":
           {"count": pd_.MAX_NOTICES, "last_seen": f"2020-{m:02d}-{d:02d}"}
           for m in range(1, 13) for d in range(1, 29)}
    assert pd_.save_ledger(led, {"obligations": {}, "notices": old}) is True
    back = pd_.load_ledger(led)[0]
    assert len(back["notices"]) == len(old), "a stale ledger pruned itself"
    assert back["notices_observed_on"] == ""
    # Once a run records its observation, retention applies from THAT date.
    assert pd_.record_notice_conditions(
        led, ["frozen_book|XETR|2026-10-15"], date(2026, 10, 15)) is True
    after = pd_.load_ledger(led)[0]
    assert after["notices_observed_on"] == "2026-10-15"
    assert len(after["notices"]) == pd_.MAX_NOTICE_KEYS
    assert "frozen_book|XETR|2026-10-15" in after["notices"], \
        "the condition this run observed was pruned"


def test_a_record_is_evicted_only_once_its_condition_stops_being_reported(
        tmp_path):
    """Retention keys on the CONDITION, not on the budget. A record that has
    fallen NOTICE_RETENTION_DAYS behind the freshest one describes something
    that resolved; nothing else is evictable."""
    led = tmp_path / "ob.json"
    resolved = "frozen_book|XETR|2025-01-02"
    live = "frozen_book|XETR|2026-08-14"
    notices = {resolved: {"count": pd_.MAX_NOTICES, "last_seen": "2025-01-02"},
               live: {"count": pd_.MAX_NOTICES, "last_seen": "2026-10-15"}}
    notices.update({f"frozen_book|NYSE|2026-{m:02d}-{d:02d}":
                    {"count": 0, "attempts": 0, "last_seen": "2026-10-15"}
                    for m in range(1, 13) for d in range(1, 29)})
    assert pd_.save_ledger(led, {"obligations": {}, "notices": notices,
                                 "notices_observed_on": "2026-10-15"}) is True
    back = pd_.load_ledger(led)[0]["notices"]
    assert resolved not in back, "a resolved condition was retained for ever"
    assert live in back, "a live condition was evicted"


def test_only_one_of_two_concurrent_workers_may_send(tmp_path):
    """``notices_due`` is a read, so two overlapping firings both select the
    same notice. The eighth pass then had both call record_notice_attempt,
    whose "already recorded today" branch returned True - the same value as
    "you may send" - so both mailed (2026-09-17, ninth review)."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-08-14"
    on = date(2026, 10, 15)
    first = pd_.claim_notice(led, key, on)
    second = pd_.claim_notice(led, key, on)
    assert first == pd_.NOTICE_CLAIMED
    assert second == pd_.NOTICE_ALREADY_CLAIMED
    assert second != pd_.NOTICE_CLAIMED, "two workers were authorised to send"
    # A different notice, and the same notice tomorrow, are separate claims.
    assert pd_.claim_notice(led, "frozen_book|NYSE|2026-08-14", on) == \
        pd_.NOTICE_CLAIMED
    assert pd_.claim_notice(led, key, date(2026, 10, 16)) == pd_.NOTICE_CLAIMED


def test_the_claim_is_atomic_under_a_real_interleaving(tmp_path):
    """Not a simulation of the race: N threads contend for one claim through
    the same O_EXCL create that two processes would."""
    import threading
    led = tmp_path / "ob.json"
    key, on = "frozen_book|XETR|2026-08-14", date(2026, 10, 15)
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(pd_.claim_notice(led, key, on))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(pd_.NOTICE_CLAIMED) == 1, \
        f"{results.count(pd_.NOTICE_CLAIMED)} workers were authorised: {results}"
    assert results.count(pd_.NOTICE_ALREADY_CLAIMED) == 7


def test_an_unclaimable_notice_is_not_sent(tmp_path, monkeypatch):
    """No durable claim means nothing bounds the repetition, so nothing may
    be sent. The three answers are distinct on purpose."""
    led = tmp_path / "ob.json"

    def _boom(*a, **kw):
        raise OSError("disk")
    monkeypatch.setattr(pd_.os, "open", _boom)
    assert pd_.claim_notice(led, "k", date(2026, 10, 15)) == \
        pd_.NOTICE_UNCLAIMABLE
    assert pd_.NOTICE_UNCLAIMABLE != pd_.NOTICE_CLAIMED


def test_claim_files_are_swept(tmp_path):
    led = tmp_path / "ob.json"
    old = date(2026, 1, 1)
    pd_.claim_notice(led, "k", old)
    assert list(pd_.notice_claim_dir(led).glob("*.claim"))
    pd_.claim_notice(led, "k", old + timedelta(days=pd_.NOTICE_CLAIM_TTL_DAYS + 1))
    names = [p.name for p in pd_.notice_claim_dir(led).glob("*.claim")]
    assert not any(n.endswith("2026-01-01.claim") for n in names), names


def test_smtp_success_with_a_failed_ledger_write_is_at_least_once(tmp_path,
                                                                  monkeypatch):
    """The unavoidable case, stated rather than hidden. The mail is gone the
    moment the server takes it; if the delivery write then fails the ledger
    does not know, and the notice is due again on a later day. The claim
    still bounds it to once a day."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-08-14"
    rep = _NOTICE_REPORT(key)
    day_one = date(2026, 10, 15)
    assert pd_.claim_notice(led, key, day_one) == pd_.NOTICE_CLAIMED
    pd_.record_notice_attempt(led, [key], day_one)
    # SMTP accepted the message. The delivery write now fails.
    monkeypatch.setattr(pd_, "_atomic_write", lambda *a, **kw: False)
    assert pd_.record_notice_delivery(led, [key], day_one,
                                      pd_.NOTICE_DELIVERED) is False
    monkeypatch.undo()
    rec = pd_.load_ledger(led)[0]["notices"][key]
    assert rec["count"] == 0, "the count survived a write that failed"
    # CROSS-DAY RETRY: it goes out again later while the condition is still
    # reported, so the operator may receive it twice rather than zero times.
    assert pd_.notices_due(rep, led, date(2026, 10, 16)), \
        "a notice whose bookkeeping failed was lost, not repeated"
    # AND NOT hourly: the same day is still bounded by the attempt stamp and
    # by the claim.
    assert pd_.notices_due(rep, led, day_one) == []
    assert pd_.claim_notice(led, key, day_one) == pd_.NOTICE_ALREADY_CLAIMED


def test_an_explicit_repair_clears_the_notice_suppression(tmp_path):
    """The eighth pass unioned the persisted keys back in unconditionally, so
    the suppression could not be lifted by ANY action - and both the code
    comment and the handoff claimed removing the record cleared it. Verified
    false on 2026-09-17."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-08-14"
    led.write_text(json.dumps({"schema": pd_.LEDGER_SCHEMA, "obligations": {},
                               "notices": {key: "rubbish"}}), encoding="utf-8")
    book, _ = pd_.load_ledger(led)
    assert set(book["notices_unreadable"]) == {key}
    assert pd_.save_ledger(led, book) is True
    assert set(pd_.load_ledger(led)[0]["notices_unreadable"]) == {key}, \
        "the evidence did not survive a save"

    # Repair one: write a well-formed record for the key.
    doc = json.loads(led.read_text(encoding="utf-8"))
    doc["notices"] = {key: {"count": 0, "attempts": 0,
                            "last_seen": "2026-10-15"}}
    led.write_text(json.dumps(doc), encoding="utf-8")
    after = pd_.load_ledger(led)[0]
    assert after["notices_unreadable"] == {}, "an explicit repair was ignored"
    assert pd_.notices_due(_NOTICE_REPORT(key), led, date(2026, 10, 16)), \
        "the key stayed suppressed after it was repaired"


def test_clear_notice_suppression_is_the_route_for_a_deleted_record(tmp_path):
    """Deleting the garbage outright is the more obvious operator move, and
    absence cannot clear the suppression on its own - save_ledger writes the
    cleaned map, so the malformed value is absent from the very next read."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-08-14"
    led.write_text(json.dumps({"schema": pd_.LEDGER_SCHEMA, "obligations": {},
                               "notices": {key: 7}}), encoding="utf-8")
    pd_.save_ledger(led, pd_.load_ledger(led)[0])
    assert set(pd_.load_ledger(led)[0]["notices_unreadable"]) == {key}
    assert pd_.clear_notice_suppression(led, key) is True
    assert pd_.load_ledger(led)[0]["notices_unreadable"] == {}
    assert pd_.clear_notice_suppression(led, key) is False, \
        "clearing a key that is not suppressed reported success"
    assert pd_.notices_due(_NOTICE_REPORT(key), led, date(2026, 10, 16))


def test_the_retained_unreadable_evidence_is_bounded(tmp_path):
    """Bounded, and NOT silently: dropping an entry lets the key it
    suppressed alert again, so the bound says so when it bites."""
    led = tmp_path / "ob.json"
    bad = {f"frozen_book|XETR|2026-09-{d:02d}": "rubbish"
           for d in range(1, pd_.MAX_UNREADABLE_NOTICES + 6)}
    led.write_text(json.dumps({"schema": pd_.LEDGER_SCHEMA, "obligations": {},
                               "notices": bad}), encoding="utf-8")
    book, _ = pd_.load_ledger(led)
    assert len(book["notices_unreadable"]) == pd_.MAX_UNREADABLE_NOTICES
    assert any("at its bound" in p for p in book["notice_problems"])
    one = next(iter(book["notices_unreadable"].values()))
    assert len(one["evidence"]) <= pd_.NOTICE_EVIDENCE_CHARS
    assert "rubbish" in one["evidence"], "the evidence names nothing"


def test_an_eighth_pass_ledger_still_suppresses_what_it_suppressed(tmp_path):
    """The eighth pass wrote notices_unreadable as a LIST. Reading one with
    this version must not read as repaired."""
    led = tmp_path / "ob.json"
    key = "frozen_book|XETR|2026-08-14"
    led.write_text(json.dumps({"schema": pd_.LEDGER_SCHEMA, "obligations": {},
                               "notices": {},
                               "notices_unreadable": [key]}), encoding="utf-8")
    book, _ = pd_.load_ledger(led)
    assert set(book["notices_unreadable"]) == {key}
    assert pd_.notices_due(_NOTICE_REPORT(key), led, date(2026, 10, 16)) == []


def test_a_live_set_larger_than_the_bound_is_reported_not_truncated(tmp_path):
    """Dropping one would silently forget a condition nobody has been told
    about, which is the defect this module exists to remove."""
    led = tmp_path / "ob.json"
    live = {f"frozen_book|NYSE|2027-01-{d:02d}": {"count": 0, "attempts": 1,
                                                  "last_seen": "2027-01-01"}
            for d in range(1, pd_.MAX_NOTICE_KEYS + 6)}
    assert pd_.save_ledger(led, {"obligations": {}, "notices": live}) is True
    book, _ = pd_.load_ledger(led)
    assert len(book["notices"]) == len(live), "a live notice was evicted"
    assert any("exceed the retention bound" in p
               for p in book["notice_problems"])


def test_a_frozen_book_is_reported_as_unknown(repo, monkeypatch):
    """The broader statement: a book that has stopped advancing cannot say
    which fills have happened since, so the verdict is UNKNOWN rather than
    clean. The live book on 2026-09-17 carries a 14 September fill and is
    NOT frozen."""
    frozen, why = pd_.book_looks_frozen(
        {"next_fill": {"by_venue": {"XETR": "2026-09-14"}}},
        date(2026, 10, 15))
    assert frozen is True and "stopped moving" in why
    near, _ = pd_.book_looks_frozen(
        {"next_fill": {"by_venue": {"XETR": "2026-09-14"}}},
        date(2026, 9, 17))
    assert near is False, "a three-day-old fill is an ordinary week"


def test_a_marker_with_no_sealed_at_authorises_nothing(repo):
    """The forged-marker protection is unchanged: a hand-written file has no
    sealed_at, and without one there is no clock to verify at."""
    _write_marker(repo)
    out = pd_.release_authorisation(repo, datetime(2026, 9, 16, 1, 0,
                                                   tzinfo=timezone.utc))
    assert out["verified"] is False and out["error"]


# ---------------------------------------------------------------------------
# 18. The ledger read-back checks CONTENT (finding 2)
# ---------------------------------------------------------------------------
def test_a_same_key_stale_file_fails_the_read_back(repo, monkeypatch):
    """REPRODUCED 2026-09-16: the read-back compared KEY SETS, so a write
    that silently did nothing over an older ledger passed - every key in
    place and every value wrong, including the states and the observed-owed
    flags the next run reasons from."""
    led = repo / "logs" / "ob.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({
        "schema": 1, "updated_utc": "2026-09-01T00:00:00",
        "obligations": {"NYSE|2026-09-14": {
            "venue": "NYSE", "fill_date": "2026-09-14", "state": "pending",
            "observed_owed_after_fill": False}}}), encoding="utf-8")
    monkeypatch.setattr(pd_, "_atomic_write", lambda *a, **kw: True)
    assert pd_.save_ledger(led, {"schema": 1, "obligations": {
        "NYSE|2026-09-14": {"venue": "NYSE", "fill_date": "2026-09-14",
                            "state": "owed",
                            "observed_owed_after_fill": True}}}) is False


def test_a_real_write_passes_the_read_back(repo):
    led = repo / "logs" / "ob.json"
    assert pd_.save_ledger(led, {"schema": 1, "obligations": {
        "NYSE|2026-09-14": {"venue": "NYSE", "fill_date": "2026-09-14",
                            "state": "owed"}}}) is True


# ---------------------------------------------------------------------------
# 19. Lost history needs QUALIFYING evidence (finding 3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,body", [
    ("run_outcomes.jsonl", ""),                       # blank
    ("run_outcomes.jsonl", "{not json\n"),            # malformed
    ("run_outcomes.jsonl", '{"asof": "x"}\n'),        # no cadence
    ("last_green_run.json", ""),                      # blank
    ("last_green_run.json", "{}"),                    # no cadence
    ("last_green_europe.json",
     '{"cadence": "weekend", "local_date": "2026-09-13"}'),   # collection only
    ("last_green_core.json",
     '{"cadence": "weekend", "local_date": "2026-09-13"}'),
    ("alert_delivery.json", '{"status": "sent"}'),    # says nothing about runs
])
def test_a_weak_artefact_does_not_prove_lost_history(repo, name, body):
    """REPRODUCED 2026-09-16: the mere EXISTENCE of any of these files was
    taken as proof that the clone had run before, so a blank, malformed or
    collection-only artefact put the verdict into UNKNOWN and disabled miss
    detection for every fill up to today."""
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    (repo / "logs" / name).write_text(body, encoding="utf-8")
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert not any("history was lost" in p for p in rep.problems)
    assert rep.unknown is False


def test_a_collection_only_clone_is_not_accused_of_losing_history(repo):
    """A Europe capture firing writes a component marker and publishes
    nothing. It is not evidence of a publication history."""
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    for name in ("last_green_europe.json", "last_green_core.json"):
        (repo / "logs" / name).write_text(
            json.dumps({"cadence": "weekend", "local_date": "2026-09-13"}),
            encoding="utf-8")
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    assert not any("history was lost" in p
                   for p in _debt(repo, date(2026, 9, 16)).problems)


def test_a_manual_ledger_deletion_is_reported_and_then_clears(repo):
    """Deleting the ledger by hand IS a lost history, and saying so is
    right. It must clear on the next firing, once the ledger repopulates."""
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    (repo / "logs" / "run_outcomes.jsonl").write_text(
        json.dumps({"asof": "2026-09-13T01:00:00", "cadence": "post-fill",
                    "exit_code": 0, "outcome": "green"}) + "\n",
        encoding="utf-8")
    _debt(repo, date(2026, 9, 16))
    (repo / "logs" / "ob.json").unlink()
    gone = _debt(repo, date(2026, 9, 16))
    assert any("history was lost" in p for p in gone.problems)
    back = _debt(repo, date(2026, 9, 16))
    assert not any("history was lost" in p for p in back.problems)
    assert back.owed is True, "the obligation did not come back"


def test_a_genuine_first_run_is_not_accused_of_losing_history(repo):
    """The other side of the same test. A clone with no obligation history
    AND no sign of having run before has simply not run before."""
    (repo / "data" / "live_targets.json").write_text(json.dumps(_targets(
        {"NYSE": "2026-09-21"}, {"A": "NYSE", "B": "NYSE", "C": "NYSE"})),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.unknown is False
    assert not any("history was lost" in p for p in rep.problems)


def test_the_ledger_write_is_verified_by_reading_it_back(repo, monkeypatch):
    """A writer that returns True has reported on its own behaviour. The
    file is what the next run will actually read."""
    led = repo / "logs" / "ob.json"
    monkeypatch.setattr(pd_, "_atomic_write", lambda *a, **kw: True)
    assert pd_.save_ledger(led, {"schema": 1, "obligations": {
        "NYSE|2026-09-14": {"venue": "NYSE", "fill_date": "2026-09-14"}}}) \
        is False, "a write that did not persist was reported as success"


def test_an_unrecovered_gap_never_ages_out(repo):
    led = repo / "logs" / "ob.json"
    pd_.record_write_outcome(led, False,
                             datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert pd_.relevant_gaps(pd_.continuity_state(led), date(2026, 9, 16))


# ---------------------------------------------------------------------------
# 15. HOLD authorisation and provisional READY (finding 2)
# ---------------------------------------------------------------------------
def test_a_raw_hold_the_seal_does_not_name_is_not_an_exemption(repo, monkeypatch):
    """REPRODUCED 2026-09-16: any sleeve's raw ``status: HOLD`` was accepted
    as an authorised exemption, which is weaker than component_release.

    RE-PINNED 2026-09-19. The protection is unchanged and the rule it rests on
    has moved: what disqualifies this HOLD is that the sealed release does not
    NAME sleeve A, not that A is spelled differently from D. A book can still
    not exempt itself by writing HOLD in its own file.
    """
    _seal_release(monkeypatch, held=("D",))
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"NYSE": "2026-09-14"},
                             {"A": "NYSE", "B": "NYSE", "C": "NYSE"},
                             {"A": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True
    assert any("does not record sleeve A on an authorised HOLD" in p
               for p in rep.problems)


def test_a_held_sleeve_C_the_seal_names_is_exempt(repo, monkeypatch):
    """THE 2026-09-19 CASE. Sleeve C holds whenever the coverage floor refuses
    a partial decision row - a late BTC-USD bar is enough - and before this it
    could not be authorised at any price, so it acquired a publication debt and
    would have escalated a false missed fill from the Tuesday after."""
    _seal_release(monkeypatch, held=("C",))
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"NYSE": "2026-09-14"},
                             {"A": "NYSE", "B": "NYSE", "C": "NYSE"},
                             {"C": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert not any("authorised HOLD" in p for p in rep.problems), rep.problems


@pytest.mark.parametrize("release,why", [
    (None, "does not verify against its own contract"),
    ({"verified": True, "anchor": "2026-09-04", "d_ready": False,
      "held_sleeves": ["D"]},
     "not '2026-09-11'"),
    # RE-PINNED 2026-09-19: the refusal is now that the seal does not NAME
    # this sleeve, which is the direct question. Reading D's status off
    # d_ready could not express a held C at all.
    ({"verified": True, "anchor": "2026-09-11", "d_ready": True,
      "held_sleeves": []},
     "does not record sleeve D on an authorised HOLD"),
    ({"verified": True, "anchor": "2026-09-11", "d_ready": False,
      "held_sleeves": ["C"]},
     "does not record sleeve D on an authorised HOLD"),
    ({"verified": False, "error": "sealed source changed: data/x.json",
      "anchor": "2026-09-11", "d_ready": False, "held_sleeves": ["D"]},
     "sealed source changed"),
])
def test_a_d_hold_without_a_verified_release_is_not_authorised(
        repo, monkeypatch, release, why):
    monkeypatch.setattr(
        pd_, "release_authorisation",
        lambda repo_root, now=None: release or {"verified": False,
                                                "error": "no release"})
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True, "an unverified HOLD exempted the sleeve"
    assert any(why in p for p in rep.problems)


def test_a_HAND_WRITTEN_release_marker_authorises_nothing(repo):
    """REPRODUCED 2026-09-16: two lines of JSON with a matching anchor and
    d_ready:false exempted sleeve D from its publication obligation. The
    marker sits in the working tree where anything can write it; the
    authorisation is now component_release.verify, which checks an identity
    hash, the committed seal, every source digest, the book, the guards and
    the price evidence."""
    _write_marker(repo)
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True, "a forged release marker suppressed an obligation"
    assert any("does not verify against its own contract" in p
               for p in rep.problems)


def test_the_release_check_runs_only_when_a_hold_claims_it(repo, monkeypatch):
    """Verifying a release is real work - a git read of the sealed commit and
    a digest of every source - and a book with no HOLD in it never needs the
    answer."""
    calls = []
    monkeypatch.setattr(pd_, "release_authorisation",
                        lambda repo_root, now=None: calls.append(1) or
                        {"verified": False, "error": "x"})
    _debt(repo, date(2026, 9, 16))                 # the fixture holds nothing
    assert calls == []
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    _debt(repo, date(2026, 9, 16))
    assert len(calls) == 1, "the release was verified once, on the HOLD"


def test_release_authorisation_never_raises(tmp_path):
    out = pd_.release_authorisation(tmp_path)
    assert out["verified"] is False and out["error"]


def test_a_d_hold_with_no_reason_is_not_authorised(repo, monkeypatch):
    _seal_release(monkeypatch)
    doc = _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                               {"D": "HOLD"})
    for sl in doc["sleeves"]:
        sl.pop("reason", None)
    (repo / "data" / "live_targets.json").write_text(json.dumps(doc),
                                                     encoding="utf-8")
    assert _debt(repo, date(2026, 9, 16)).owed is True


def test_a_provisional_ready_does_not_bind_the_obligation(repo, monkeypatch):
    """REPRODUCED 2026-09-16: a midweek READY on a book that is not final,
    ranked on a session that is not the one its fill will be decided by,
    made the sleeve permanently obliged - even where the final release
    legitimately held it."""
    doc = _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"}, {},
                               final=False)
    for sl in doc["sleeves"]:
        sl["decision_session"] = "2026-09-09"      # ranked midweek
        sl["decision_session_for_fill"] = "2026-09-11"
    (repo / "data" / "live_targets.json").write_text(json.dumps(doc),
                                                     encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    assert rep.owed is True                        # still obliged for now
    assert any("provisional" in p for p in rep.problems)

    # ...and the final release, holding D, is then free to exempt it.
    _seal_release(monkeypatch)
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 16))
    states = {r["key"]: r["state"] for r in rep.obligations}
    assert states["XETR|2026-09-14"] == pd_.NOT_OBLIGED


def test_a_final_ready_binds_even_against_a_later_authorised_hold(
        repo, monkeypatch):
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"}, {},
                             final=True)), encoding="utf-8")
    assert _debt(repo, date(2026, 9, 16)).owed is True
    _seal_release(monkeypatch)
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": "2026-09-14"}, {"D": "XETR"},
                             {"D": "HOLD"})), encoding="utf-8")
    assert _debt(repo, date(2026, 9, 16)).owed is True, \
        "an authorised HOLD retired an obligation a final book had bound"


# ---------------------------------------------------------------------------
# 16. A verified schedule revision is not a missed publication (finding 7)
# ---------------------------------------------------------------------------
def _revise(repo, old_fill, new_fill):
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": old_fill}, {"D": "XETR"}, {})),
        encoding="utf-8")
    assert _debt(repo, date(2026, 9, 16)).owed is True
    (repo / "data" / "live_targets.json").write_text(json.dumps(
        _targets_with_status({"XETR": new_fill}, {"D": "XETR"}, {})),
        encoding="utf-8")
    _publish(repo, {k: new_fill for k in "ABCD"}, "published on the new date")


def test_a_calendar_confirmed_revision_is_not_a_miss(repo, monkeypatch):
    """REPRODUCED 2026-09-16: a controlled calendar change moving the fill
    from 14 to 15 September left the retained original obligation classified
    missed:true even though the revised fill published."""
    monkeypatch.setattr(pd_, "venue_sessions",
                        lambda v, a, b: {"2026-09-11", "2026-09-15"})
    _revise(repo, "2026-09-14", "2026-09-15")
    rep = _debt(repo, date(2026, 9, 16))
    states = {r["key"]: r["state"] for r in rep.obligations}
    assert states["XETR|2026-09-14"] == pd_.REVISED
    assert states["XETR|2026-09-15"] == pd_.DISCHARGED
    assert rep.escalate is False


def test_a_calendar_that_REFUSES_the_move_does_not_excuse_the_miss(repo):
    """CORRECTED 2026-09-16 (fifth review). The calendar still lists the old
    fill, so it is not evidence that the schedule moved - it is evidence
    that it did NOT. A sibling sharing the decision session no longer
    suppresses a miss on those terms; only a calendar that cannot be READ
    does, because that is absence of evidence rather than evidence of
    absence. Nothing is discharged either way."""
    _revise(repo, "2026-09-14", "2026-09-15")
    rep = _debt(repo, date(2026, 9, 16))
    rec = [r for r in rep.obligations if r["key"] == "XETR|2026-09-14"][0]
    assert rec["state"] == pd_.SUPERSEDED
    assert rec["missed"] is True
    assert "REFUSES the move" in rec["reason"]


def test_an_unreadable_calendar_still_suppresses_the_miss(repo, monkeypatch):
    """The other half of the same rule: with no calendar to consult, a
    schedule revision cannot be ruled out, so no miss is asserted."""
    _revise(repo, "2026-09-14", "2026-09-15")
    monkeypatch.setattr(pd_, "venue_sessions", lambda *a: None)
    rep = _debt(repo, date(2026, 9, 16))
    rec = [r for r in rep.obligations if r["key"] == "XETR|2026-09-14"][0]
    assert rec["missed"] is False
    assert "could not be read" in rec["reason"]
    assert rep.escalate is False


def test_fill_plus_one_is_not_accepted_on_its_own(repo, monkeypatch):
    """Both dates are real sessions and the new one ranks on a different
    close: that is the following rebalance, not a revision of this one."""
    monkeypatch.setattr(pd_, "venue_sessions",
                        lambda v, a, b: {"2026-09-11", "2026-09-14",
                                         "2026-09-15"})
    ok, status, why = pd_.fill_was_revised("XETR", "2026-09-14", "2026-09-15",
                                           "2026-09-11")
    assert ok is False
    assert status == pd_.REVISION_OLD_FILL_STILL_TRADES
    assert status not in pd_.REVISION_INCONCLUSIVE,         "a calendar that ANSWERED must not excuse anything"
    assert "still a XETR session" in why


def test_a_BACKWARD_revision_is_recognised_too(repo, monkeypatch):
    """REPRODUCED 2026-09-16: the caller searched only for a LATER sibling
    fill, so a calendar revision that moved the fill EARLIER left the
    original obligation OWED for ever - the published record can never reach
    a session the calendar has withdrawn. fill_was_revised always accepted
    either ordering; its caller did not."""
    monkeypatch.setattr(pd_, "venue_sessions",
                        lambda v, a, b: {"2026-09-11", "2026-09-14"})
    _revise(repo, "2026-09-15", "2026-09-14")
    rep = _debt(repo, date(2026, 9, 16))
    states = {r["key"]: r["state"] for r in rep.obligations}
    assert states["XETR|2026-09-15"] == pd_.REVISED
    assert states["XETR|2026-09-14"] == pd_.DISCHARGED
    assert rep.owed is False and rep.escalate is False


def test_an_owed_sibling_is_excused_only_while_the_calendar_is_SILENT(
        repo, monkeypatch):
    """CORRECTED 2026-09-16 (fifth review). An OWED obligation with a
    sibling used to be dropped from escalation unconditionally, so a
    genuine miss could hide behind a fill the calendar had never confirmed
    as its replacement. The excuse now needs the calendar to be unreadable."""
    _revise(repo, "2026-09-15", "2026-09-14")

    monkeypatch.setattr(pd_, "venue_sessions", lambda *a: None)
    silent = _debt(repo, date(2026, 9, 25))       # well past grace
    rec = [r for r in silent.obligations if r["key"] == "XETR|2026-09-15"][0]
    assert rec["state"] == pd_.OWED
    assert rec.get("revision_sibling") == "2026-09-14"
    assert silent.escalate is False, "an unreadable calendar did not excuse it"

    monkeypatch.undo()                            # the calendar answers again
    speaking = _debt(repo, date(2026, 9, 25))
    assert speaking.escalate is True, \
        "a calendar that refuses the move still excused the obligation"


def test_the_suppression_rule_reads_a_STATUS_not_a_sentence(repo, monkeypatch):
    """REPRODUCED 2026-09-17: both escalation paths decided whether a sibling
    excuses a miss by searching the reason PROSE for "could not be read". A
    reworded message would have flipped a suppression into an assertion with
    no test failing. The branch is now a structured status, and the prose is
    free to say whatever reads best."""
    src = inspect.getsource(pd_.current_debt)
    assert '"could not be read"' not in src, \
        "a control-flow branch is still reading the message text"
    assert "REVISION_INCONCLUSIVE" in src

    # And the statuses themselves are exhaustive over fill_was_revised.
    calls = [
        (lambda v, a, b: None, pd_.REVISION_CALENDAR_UNREADABLE),
        (lambda v, a, b: {"2026-09-11", "2026-09-14", "2026-09-15"},
         pd_.REVISION_OLD_FILL_STILL_TRADES),
        (lambda v, a, b: {"2026-09-11"}, pd_.REVISION_NEW_FILL_NOT_A_SESSION),
        (lambda v, a, b: {"2026-09-09", "2026-09-15"},
         pd_.REVISION_WRONG_DECISION_SESSION),
        (lambda v, a, b: {"2026-09-11", "2026-09-15"},
         pd_.REVISION_CONFIRMED),
    ]
    for sessions, expected in calls:
        monkeypatch.setattr(pd_, "venue_sessions", sessions)
        ok, status, why = pd_.fill_was_revised("XETR", "2026-09-14",
                                               "2026-09-15", "2026-09-11")
        assert status == expected, why
        assert ok is (status == pd_.REVISION_CONFIRMED)
    monkeypatch.undo()
    ok, status, why = pd_.fill_was_revised("XETR", "nonsense", "2026-09-15",
                                           "2026-09-11")
    assert status == pd_.REVISION_UNREADABLE_DATES


def test_a_malformed_sibling_never_raises_out_of_current_debt(repo):
    """REPRODUCED 2026-09-17: _revision_candidates sorted siblings by
    distance, calling date.fromisoformat on every one of them BEFORE
    fill_was_revised could classify it. A single unreadable fill_date in the
    ledger raised ValueError straight out of current_debt - a function whose
    docstring promises it never does, and which the refresh calls from
    inside its own failure handler."""
    led = repo / "logs" / "ob.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({"schema": 1, "obligations": {
        "XETR|2026-09-14": {"venue": "XETR", "fill_date": "2026-09-14",
                            "decision_session": "2026-09-11",
                            "sleeves": ["D"], "state": "owed",
                            "observed_owed_after_fill": True},
        "XETR|not-a-date": {"venue": "XETR", "fill_date": "not-a-date",
                            "decision_session": "2026-09-11",
                            "sleeves": ["D"], "state": "owed"}}}),
        encoding="utf-8")

    rep = _debt(repo, date(2026, 9, 20))          # must not raise
    assert rep.unknown is True
    assert rep.should_run is True
    assert any("fill date is unreadable" in p and "not-a-date" in p
               for p in rep.problems)

    # Not parsed, not guessed at - and NOT deleted either.
    back, _ = pd_.load_ledger(led)
    assert "XETR|not-a-date" in back["obligations"], \
        "a record that could not be read was silently dropped"


def test_the_unreadable_sibling_is_named_on_the_obligation(repo):
    led = repo / "logs" / "ob.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({"schema": 1, "obligations": {
        "XETR|2026-09-14": {"venue": "XETR", "fill_date": "2026-09-14",
                            "decision_session": "2026-09-11",
                            "sleeves": ["D"], "state": "owed",
                            "observed_owed_after_fill": True},
        "XETR|rubbish": {"venue": "XETR", "fill_date": "rubbish",
                         "decision_session": "2026-09-11",
                         "sleeves": ["D"], "state": "owed"}}}),
        encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 20))
    rec = [r for r in rep.obligations if r["key"] == "XETR|2026-09-14"][0]
    assert rec["unreadable_siblings"] == ["rubbish"]


def test_one_malformed_sibling_raises_one_problem_not_one_per_neighbour(repo):
    """Every obligation sharing the decision session sees the same bad
    record, so a single fault produced a line per obligation and an operator
    read four copies of it (2026-09-17, eighth review). The evidence stays on
    each record; the problem is raised once, naming what it blocks."""
    led = repo / "logs" / "ob.json"
    led.parent.mkdir(parents=True, exist_ok=True)
    obs = {f"XETR|2026-09-{d:02d}": {"venue": "XETR",
                                     "fill_date": f"2026-09-{d:02d}",
                                     "decision_session": "2026-09-11",
                                     "sleeves": ["D"], "state": "owed",
                                     "observed_owed_after_fill": True}
           for d in (14, 15, 16, 17)}
    obs["XETR|rubbish"] = {"venue": "XETR", "fill_date": "rubbish",
                           "decision_session": "2026-09-11",
                           "sleeves": ["D"], "state": "owed"}
    led.write_text(json.dumps({"schema": 1, "obligations": obs}),
                   encoding="utf-8")
    rep = _debt(repo, date(2026, 9, 20))
    named = [p for p in rep.problems if "rubbish" in p and "unreadable" in p
             and "obligation ledger holds" in p]
    assert len(named) == 1, f"one fault, {len(named)} problems: {named}"
    # It still says which obligations it blocks, and each one still carries
    # the evidence.
    for d in (14, 15, 16, 17):
        assert f"XETR|2026-09-{d:02d}" in named[0]
        rec = [r for r in rep.obligations
               if r["key"] == f"XETR|2026-09-{d:02d}"][0]
        assert rec["unreadable_siblings"] == ["rubbish"]
    assert rep.unknown is True, "an unreadable sibling stopped blocking"


@pytest.mark.parametrize("fill", ["not-a-date", "", "2026-13-40", None])
def test_revision_candidates_separates_what_it_cannot_read(fill):
    ob = pd_.Obligation("XETR", "2026-09-14", ("D",), "2026-09-11")
    siblings = {("XETR", "2026-09-11"): ["2026-09-15", fill]}
    candidates, unreadable = pd_._revision_candidates(siblings, ob)
    assert candidates == ["2026-09-15"]
    assert unreadable == [str(fill)]


def test_only_an_inconclusive_status_excuses_an_obligation():
    """The rule in one line: a calendar that ANSWERED excuses nothing."""
    assert set(pd_.REVISION_INCONCLUSIVE) == {
        pd_.REVISION_CALENDAR_UNREADABLE, pd_.REVISION_UNREADABLE_DATES}
    for answered in (pd_.REVISION_OLD_FILL_STILL_TRADES,
                     pd_.REVISION_NEW_FILL_NOT_A_SESSION,
                     pd_.REVISION_WRONG_DECISION_SESSION,
                     pd_.REVISION_CONFIRMED):
        assert answered not in pd_.REVISION_INCONCLUSIVE


def test_stale_sibling_metadata_does_not_decide_a_later_verdict(repo,
                                                                monkeypatch):
    """The sibling and the calendar's answer are recomputed on every
    evaluation. A record written while the calendar was down must not keep
    excusing the obligation once it is back."""
    _revise(repo, "2026-09-15", "2026-09-14")
    monkeypatch.setattr(pd_, "venue_sessions", lambda *a: None)
    _debt(repo, date(2026, 9, 20))
    led = json.loads((repo / "logs" / "ob.json").read_text(encoding="utf-8"))
    assert "could not be read" in \
        led["obligations"]["XETR|2026-09-15"]["revision_checked"]

    monkeypatch.undo()
    _debt(repo, date(2026, 9, 21))
    led = json.loads((repo / "logs" / "ob.json").read_text(encoding="utf-8"))
    stored = led["obligations"]["XETR|2026-09-15"].get("revision_checked", "")
    assert "could not be read" not in stored, "stale metadata survived"


def test_repeated_firings_do_not_re_escalate_the_same_miss(repo):
    """The miss is asserted, and the escalation dedupe bounds the mail."""
    _revise(repo, "2026-09-14", "2026-09-15")
    first = _debt(repo, date(2026, 9, 20))
    due = pd_.escalation_due(first, date(2026, 9, 20))
    assert due, "an evidenced miss did not escalate at all"
    pd_.mark_escalated(repo / "logs" / "ob.json", due, date(2026, 9, 20))
    again = _debt(repo, date(2026, 9, 20))
    assert pd_.escalation_due(again, date(2026, 9, 20)) == []


def test_an_unreadable_calendar_never_confirms_a_revision(monkeypatch):
    monkeypatch.setattr(pd_, "venue_sessions", lambda *a: None)
    ok, status, why = pd_.fill_was_revised("XETR", "2026-09-14", "2026-09-15",
                                           "2026-09-11")
    assert ok is False
    assert status == pd_.REVISION_CALENDAR_UNREADABLE
    assert status in pd_.REVISION_INCONCLUSIVE
    assert "could not be read" in why


def test_an_owed_obligation_inside_grace_does_not_pay_for_the_history_walk(repo):
    calls = []
    real = pd_.publication_history

    def counted(*a, **kw):
        calls.append(a)
        return real(*a, **kw)
    import unittest.mock as _mock
    with _mock.patch.object(pd_, "publication_history", counted):
        rep = _debt(repo, date(2026, 9, 16))      # age 2, grace 3
    assert rep.owed is True
    assert calls == [], "the common owed path walked the history"
