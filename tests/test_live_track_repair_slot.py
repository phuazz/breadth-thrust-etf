"""Tests for scripts/live_track_repair_slot.py and the two workflow files it
binds together.

The decision is exercised against synthetic live_track files with a fixed
clock, so no network is involved; the calendar facts come from
nyse_sessions (covered by tests/test_nyse_sessions.py). Python datetime
months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.live_track_repair_slot import (
    FORCE_ENV,
    decide,
    live_verdict,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Thu 2 Jul 2026 at 22:00 UTC: the 2 Jul close (20:00 UTC) is complete, so
# the expected session is 2026-07-02 and Wed 1 Jul is the one-behind case.
# Fri 3 Jul 2026 is the Independence Day observance (test_nyse_sessions), so
# the fixture sits on the Thursday that ends that week.
NOW = datetime(2026, 7, 2, 22, 0, tzinfo=timezone.utc)


def _write_live_track(tmp_path: Path, anchor: str, dates: list[str],
                      equity: list[float]) -> Path:
    blob = {"anchor_date": anchor, "anchor_equity": 2.9,
            "live_dates": dates, "live_equity": equity,
            "deployed_series_end": max([anchor, *dates])}
    (tmp_path / "live_track.json").write_text(json.dumps(blob),
                                              encoding="utf-8")
    return tmp_path


def _outputs(path: Path) -> dict[str, str]:
    """Parse the key=value lines of a GITHUB_OUTPUT file (heredoc skipped)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and "<<" not in line and not line.startswith("detail"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------

def test_a_current_series_is_not_dispatched():
    dispatch, reason = decide({"status": "ok", "label": "Live track",
                               "evidence": "ends 2026-07-02"})
    assert dispatch is False
    assert "already reaches" in reason


def test_one_session_behind_dispatches():
    dispatch, reason = decide({
        "status": "warn", "label": "Live track",
        "evidence": "ends 2026-07-01, 1 session(s) behind expected 2026-07-02",
    })
    assert dispatch is True
    assert "warn" in reason


def test_an_unreadable_or_corrupt_file_dispatches_so_the_daily_job_rewrites_it():
    dispatch, _ = decide({"status": "fail", "label": "Live track",
                          "evidence": "unreadable (live_track.json): boom"})
    assert dispatch is True


def test_force_dispatches_a_current_series():
    dispatch, reason = decide({"status": "ok", "label": "Live track",
                               "evidence": ""}, force=True)
    assert dispatch is True
    assert "forced" in reason


# ---------------------------------------------------------------------------
# main, end to end against a synthetic committed file and a fixed clock
# ---------------------------------------------------------------------------

def test_main_skips_when_the_committed_series_reaches_the_expected_session(
        tmp_path, monkeypatch):
    data_dir = _write_live_track(tmp_path, "2026-07-02", [], [])
    out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv(FORCE_ENV, raising=False)
    assert main([], data_dir=data_dir, now_utc=NOW) == 0
    assert _outputs(out)["dispatch"] == "false"


def test_main_dispatches_when_the_committed_series_is_one_session_behind(
        tmp_path, monkeypatch):
    # The withheld-bar case: splice to Wed 1 Jul while Thu 2 Jul has closed.
    data_dir = _write_live_track(tmp_path, "2026-06-26",
                                 ["2026-06-29", "2026-06-30", "2026-07-01"],
                                 [1.00, 1.01, 1.02])
    out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv(FORCE_ENV, raising=False)
    assert main([], data_dir=data_dir, now_utc=NOW) == 0
    assert _outputs(out)["dispatch"] == "true"


def test_main_honours_the_force_environment_variable(tmp_path, monkeypatch):
    data_dir = _write_live_track(tmp_path, "2026-07-02", [], [])
    out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv(FORCE_ENV, "true")
    assert main([], data_dir=data_dir, now_utc=NOW) == 0
    assert _outputs(out)["dispatch"] == "true"


def test_main_dispatches_when_the_committed_file_is_missing(
        tmp_path, monkeypatch):
    out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv(FORCE_ENV, raising=False)
    assert main([], data_dir=tmp_path, now_utc=NOW) == 0
    assert _outputs(out)["dispatch"] == "true"


def test_live_verdict_judges_an_empty_splice_on_its_anchor(tmp_path):
    """The state after a local re-anchor: no live points, anchor current."""
    v = live_verdict(NOW.date(),
                     _write_live_track(tmp_path, "2026-07-02", [], []))
    assert v["status"] == "ok"


# ---------------------------------------------------------------------------
# The two workflow files, pinned as text (test_check_pretrade_ready style)
# ---------------------------------------------------------------------------

def test_repair_workflow_runs_after_the_vendor_window_and_outside_the_local_clone_window():
    wf = (WORKFLOWS / "daily_live_track_repair.yml").read_text(encoding="utf-8")
    assert "cron: '17 8 * * 2-5'" in wf      # Tue-Fri 08:17 UTC, never Saturday
    assert "actions: write" in wf             # gh workflow run needs it
    assert ("gh workflow run daily_live_track.yml --ref main "
            "-f reason=repair-slot") in wf
    assert "steps.decide.outputs.dispatch == 'true'" in wf
    assert "LIVE_TRACK_REPAIR_FORCE" in wf


def test_daily_workflow_keeps_its_post_close_slot_and_withholds_the_warning_there():
    wf = (WORKFLOWS / "daily_live_track.yml").read_text(encoding="utf-8")
    assert "cron: '30 21 * * 1-5'" in wf
    assert wf.count("cron:") == 1             # the repair slot is its own workflow
    # The warning email is reserved for a run after the vendor window; the
    # 21:30 slot prints a notice instead.
    assert "github.event.schedule != '30 21 * * 1-5'" in wf
    assert "github.event.schedule == '30 21 * * 1-5'" in wf
    assert "reason:" in wf                    # the dispatch input the slot fills
    assert "group: daily-live-track" in wf
    # A rejected push asks whether a local refresh already carried the
    # session before it rebases (2026-10-01).
    commit_step = wf[wf.index("Commit refreshed outputs"):]
    assert "live_track_superseded.py --ref origin/main" in commit_step
    assert commit_step.index("live_track_superseded.py") < commit_step.index(
        "git pull --rebase --autostash")


# ---------------------------------------------------------------------------
# live_track_superseded: the CI side of the same collision
# ---------------------------------------------------------------------------

from scripts.live_track_superseded import main as superseded_main  # noqa: E402


def _committed_live_track(tmp_path: Path, monkeypatch, blob: dict | None) -> Path:
    """A throwaway repository whose HEAD commits ``blob`` as the live track
    (or no live track at all when ``blob`` is None)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t.test\n"
                   "[core]\n\tlongpaths = true\n\tautocrlf = false\n"
                   "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    if blob is None:
        (repo / "data" / "other.json").write_text("{}", encoding="utf-8")
    else:
        (repo / "data" / "live_track.json").write_text(json.dumps(blob),
                                                       encoding="utf-8")

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True,
                       text=True, check=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "state on origin")
    return repo


def test_a_local_re_anchor_on_origin_supersedes_this_run(tmp_path, monkeypatch):
    repo = _committed_live_track(tmp_path, monkeypatch, {
        "anchor_date": "2026-07-02", "live_dates": [], "live_equity": [],
        "deployed_series_end": "2026-07-02"})
    assert superseded_main(["--ref", "HEAD"], repo_root=repo, now_utc=NOW) == 0


def test_an_origin_still_behind_does_not_supersede(tmp_path, monkeypatch):
    repo = _committed_live_track(tmp_path, monkeypatch, {
        "anchor_date": "2026-06-26", "live_equity": [1.0, 1.01, 1.02],
        "live_dates": ["2026-06-29", "2026-06-30", "2026-07-01"]})
    assert superseded_main(["--ref", "HEAD"], repo_root=repo, now_utc=NOW) == 1


def test_an_unreadable_origin_file_is_not_superseded(tmp_path, monkeypatch):
    repo = _committed_live_track(tmp_path, monkeypatch, None)
    assert superseded_main(["--ref", "HEAD"], repo_root=repo, now_utc=NOW) == 2
