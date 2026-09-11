"""Workflow contract tests; all readiness/date cases exercise the live book path.

See test_pretrade_alert_states.py for month/year boundaries, holidays, delays,
invalid dates and operator recovery. No tests retain an orphaned report helper.
"""
from pathlib import Path


def test_workflow_is_pinned_to_the_review_and_deadline_slots():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
                / "pretrade_check.yml").read_text(encoding="utf-8")
    assert "cron: '0 6 * * 0'" in workflow
    assert "cron: '0 22 * * 0'" in workflow
    assert "cron: '0 4 * * 5'" not in workflow
    assert "Next fill" in workflow
    assert "--phase" in workflow
    assert "steps.pretrade.outputs.warn != 'false'" in workflow
    assert "last_green_run" not in workflow


def test_only_the_live_report_entrypoint_exists():
    from scripts import check_pretrade_ready as checker
    assert callable(checker.build_book_report)
    assert not hasattr(checker, "build_report")
