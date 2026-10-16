"""What earns an email, and — the harder half — what must not.

Carried from the 2026-09-20 alert-policy refinement and adapted for
vendor-resilience Phase 1 (2026-10-15):

  * the consequence table is pinned under ``routing="consequence"``, which is
    implemented but NOT the deployed default (it suppresses more than the
    tripwire did and needs owner approval);
  * a recovery needs the newest probe row's explicit evidence for the
    withdrawn session (schema-2 ``checked_days``), never a later last_bar;
  * deduplication counts CONFIRMED delivery (``delivery: confirmed``);
  * the factsheet-wording tests (D_HOLD_SENTENCE and friends) are not carried:
    that wording change was not brought into Phase 1.

Dates are ISO strings parsed with date.fromisoformat; Python months are
1-indexed (January = 1).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import alert_policy as ap  # noqa: E402
import check_vendor_probe as cvp  # noqa: E402
from component_publication import Snapshot, email_decision, review_window  # noqa: E402
from nyse_sessions import week_final_anchor  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------
def book(decision="2026-09-18", fill="2026-09-21", as_of=None):
    """The live instruction. D ranks the Xetra lines, B ranks SPY."""
    def sleeve(name, signals):
        return {"sleeve": name, "status": "READY", "reason": None,
                "decision_session": decision, "last_completed_session": decision,
                "decision_session_for_fill": decision, "fill_date": fill,
                "signals": signals, "weights": {}}
    return {"as_of": as_of or decision,
            "computed_at_utc": f"{decision}T21:00:00+00:00", "targets_final": True,
            "sleeves": [
                sleeve("A", {"CSP1": 0.1, "SOXX": 0.2}),
                sleeve("B", {"SPY": 0.07, "QQQ": 0.09}),
                sleeve("C", {"ARKG": 0.5}),
                sleeve("D", {"EXV1": 0.91, "EXH1": 0.75, "EXV3": 0.62}),
            ]}


def delivered(anchor="2026-09-18", regular="all_ready", at="2026-09-20T01:44:49+00:00"):
    state = {"anchor": anchor, "last_confirmed_at": at}
    if regular:
        state["regular"] = regular
    return {"schema": 1, "anchors": {anchor: state}}


def undelivered(anchor="2026-09-18"):
    return {"schema": 1, "anchors": {anchor: {"anchor": anchor,
                                              "preview": "abc"}}}


def probe_row(stamp, entries):
    """entries: (ticker, last_bar, last_completed_session[, venue[, proof]]).

    ``proof`` is a (checked_days, missing_completed_days) pair making the row
    schema-2 evidence for those days; without it the row is schema 1."""
    rows = []
    schema2 = False
    for e in entries:
        t, lb, lcs = e[:3]
        row = {"ticker": t, "venue": e[3] if len(e) > 3 else "NYSE",
               "last_bar": lb, "last_completed_session": lcs}
        if len(e) > 4:
            checked, missing = e[4]
            row.update({"checked_days": list(checked),
                        "missing_completed_days": list(missing),
                        "required_day_bar_present": lcs not in missing,
                        "evidence": "batch"})
            schema2 = True
        rows.append(row)
    out = {"probed_at_utc": stamp, "rows": rows}
    if schema2:
        out["schema"] = 2
    return out


def run(history, ops_rows=(), *, now, book_=None, delivery=None, data_dir=None,
        tmp_path=None, routing=ap.ROUTING_CONSEQUENCE):
    """Route one probe. The data directory is empty unless a test supplies one,
    so constituent resolution cannot reach the repository's real rosters."""
    return ap.evaluate(
        vendor_rows=history,
        retractions=cvp.detect_retractions(history),
        ops_rows=list(ops_rows),
        book=book() if book_ is None else book_,
        delivery=delivered() if delivery is None else delivery,
        now=now,
        data_dir=Path(data_dir or tmp_path or Path(__file__).parent / "fixtures"),
        routing=routing)


def at(stamp):
    return datetime.fromisoformat(stamp)


def sent(events):
    """What the record step writes after a CONFIRMED send."""
    return ap.stamp_delivery(events, "success")


# ---------------------------------------------------------------------------
# 1. A transient retraction, and its recovery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("routing", ap.ROUTING_MODES)
def test_routine_overnight_withdrawal_is_recorded_and_never_mailed(tmp_path, routing):
    history = [
        probe_row("2026-09-17T20:12:00+00:00",
                  [("EXV1.DE", "2026-09-17", "2026-09-17", "XETR")]),
        probe_row("2026-09-18T02:43:00+00:00",
                  [("EXV1.DE", "2026-09-16", "2026-09-17", "XETR")]),
    ]
    verdict = run(history, now=at("2026-09-18T02:50:00+00:00"), tmp_path=tmp_path,
                  routing=routing)
    assert verdict["action_required"] is False
    assert len(verdict["events"]) == 1
    event = verdict["events"][0]
    assert event["kind"] == ap.RETRACTION and event["class"] == "routine"
    assert event["disposition"] == ap.QUIET
    assert event["severity"] == ap.LOG_ONLY
    assert event["consequence"] == ap.ROUTINE_CYCLE


def test_a_quiet_withdrawal_recovers_quietly(tmp_path):
    opened = {"schema": 1, "recorded_at_utc": "2026-09-18T02:43:00+00:00",
              "kind": ap.RETRACTION, "incident": "EXV1.DE@2026-09-17",
              "ticker": "EXV1.DE", "venue": "XETR", "session": "2026-09-17",
              "consequence": ap.NO_DEPENDENCY, "severity": ap.LOG_ONLY,
              "disposition": ap.QUIET}
    proof = (["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"], [])
    history = [probe_row("2026-09-19T02:43:00+00:00",
                         [("EXV1.DE", "2026-09-18", "2026-09-18", "XETR", proof)])]
    verdict = run(history, [opened], now=at("2026-09-19T02:50:00+00:00"),
                  tmp_path=tmp_path)
    assert [e["kind"] for e in verdict["events"]] == [ap.RECOVERY]
    assert verdict["events"][0]["disposition"] == ap.RECOVERY_QUIET
    assert verdict["action_required"] is False
    assert ap.incidents_from([opened] + verdict["events"])["EXV1.DE@2026-09-17"].open is False


def test_a_line_that_feeds_nothing_cannot_raise_an_alert_under_consequence(tmp_path):
    """A genuinely unrelated proxy: in no universe, no roster, no book."""
    history = [
        probe_row("2026-09-18T21:41:00+00:00", [("ZZZQ", "2026-09-18", "2026-09-18")]),
        probe_row("2026-09-19T05:25:00+00:00", [("ZZZQ", "2026-09-17", "2026-09-18")]),
    ]
    verdict = run(history, now=at("2026-09-19T05:30:00+00:00"), tmp_path=tmp_path)
    assert verdict["action_required"] is False
    assert verdict["events"][0]["consequence"] == ap.NO_DEPENDENCY
    assert verdict["events"][0]["dependency"]["known"] is False


def test_xlf_is_sleeve_a_price_line_not_a_line_that_feeds_nothing(tmp_path):
    """The 2026-09-20 refinement treated XLF as feeding nothing. etf_registry
    prices the IUFS panel off XLF, so a withdrawal is a sleeve A dependency."""
    dep = ap.resolve_dependency("XLF", "NYSE", book(), tmp_path)
    assert dep.known and dep.sleeves == ("A",) and "IUFS" in dep.panels
    assert "trading_proxy" in dep.roles


def test_a_withdrawal_after_the_fill_does_not_reopen_an_archived_release(tmp_path):
    history = [
        probe_row("2026-09-18T21:41:00+00:00", [("SPY", "2026-09-18", "2026-09-18")]),
        probe_row("2026-09-23T05:25:00+00:00", [("SPY", "2026-09-17", "2026-09-22")]),
    ]
    verdict = run(history, now=at("2026-09-23T05:30:00+00:00"), tmp_path=tmp_path)
    assert verdict["action_required"] is False
    assert verdict["events"][0]["consequence"] == ap.ARCHIVED_RELEASE


def test_the_cache_is_never_consulted_to_excuse_an_alert():
    """Preserved cached data does not prove a released instruction still
    holds, and it never proves a recovery: nothing here may read a cache."""
    source = Path(ap.__file__).read_text(encoding="utf-8")
    for forbidden in ("_ohlc_cache", "prices_cache", "read_parquet"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# 2. Readiness routing (module function only; not wired by Phase 1)
# ---------------------------------------------------------------------------
def test_d_alone_unverified_at_the_deadline_does_not_mail():
    consequence, severity, reason = ap.assess_readiness("not_ready", {"D"}, "deadline")
    assert consequence == ap.D_UNVERIFIED
    assert severity == ap.LOG_ONLY
    assert "factsheet" in reason


def test_an_authorised_hold_is_not_a_readiness_failure():
    for scopes in ({"D"}, {"C"}, {"A", "B", "C"}):
        consequence, severity, _ = ap.assess_readiness("hold", scopes, "deadline")
        assert (consequence, severity) == (ap.AUTHORISED_HOLD, ap.LOG_ONLY)


def test_d_unverified_reaches_the_distribution_rather_than_the_operator():
    now = at("2026-09-20T10:30:00+00:00")           # Sunday 18:30 SGT
    decision = email_decision(
        now, anchor="2026-09-18",
        core=Snapshot("core-id", True, "2026-09-18", "2026-09-18"),
        europe=Snapshot("europe-id", False, "2026-09-17", "2026-09-18"),
        sent={})
    assert decision["action"] == "regular"
    assert decision["audience"] == "distribution"
    assert decision["d_hold"] is True


@pytest.mark.parametrize("scopes", [{"A"}, {"B"}, {"C"}, {"book"}, {"overlay"},
                                    {"D", "overlay"}, {"C", "D"}])
def test_core_unverified_at_the_deadline_requires_intervention(scopes):
    consequence, severity, _ = ap.assess_readiness("not_ready", scopes, "deadline")
    assert (consequence, severity) == (ap.CORE_READINESS_FAILURE, ap.ACTION_REQUIRED)


def test_invalid_data_alerts_in_either_phase():
    for phase in ("review", "deadline"):
        assert ap.assess_readiness("error", {"D"}, phase)[1] == ap.ACTION_REQUIRED


def test_the_review_checkpoint_is_informational_and_the_deadline_is_not():
    assert ap.assess_readiness("not_ready", {"A"}, "review")[0] == ap.REVIEW_CHECKPOINT
    assert ap.assess_readiness("not_ready", {"A"}, "deadline")[1] == ap.ACTION_REQUIRED


def test_a_ready_book_says_nothing():
    assert ap.assess_readiness("ready", set(), "deadline") == (
        ap.READY, ap.LOG_ONLY, "every sleeve is verified for the next fill")


# ---------------------------------------------------------------------------
# 4. An invalidated released instruction (consequence routing)
# ---------------------------------------------------------------------------
def _ranked_withdrawal(stamp="2026-09-20T03:08:00+00:00"):
    return [
        probe_row("2026-09-18T20:12:00+00:00",
                  [("EXV1.DE", "2026-09-18", "2026-09-18", "XETR")]),
        probe_row(stamp, [("EXV1.DE", "2026-09-16", "2026-09-18", "XETR")]),
    ]


def test_a_withdrawn_ranked_input_of_a_distributed_instruction_mails(tmp_path):
    verdict = run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                  tmp_path=tmp_path)
    assert verdict["action_required"] is True
    event = verdict["events"][0]
    assert event["consequence"] == ap.RANKED_INPUT_WITHDRAWN
    assert event["disposition"] == ap.NOTIFY
    assert event["dependency"]["sleeves"] == ["D"]
    assert event["provenance"]["distributed"] is True


def test_the_same_withdrawal_is_quiet_before_the_factsheet_goes_out(tmp_path):
    verdict = run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                  delivery=undelivered(), tmp_path=tmp_path)
    assert verdict["action_required"] is False
    assert verdict["events"][0]["consequence"] == ap.PRE_RELEASE_INPUT


def test_last_week_s_receipt_does_not_make_this_week_s_book_distributed(tmp_path):
    fresh = book(decision="2026-09-25", fill="2026-09-28")
    history = [
        probe_row("2026-09-25T20:12:00+00:00",
                  [("EXV1.DE", "2026-09-25", "2026-09-25", "XETR")]),
        probe_row("2026-09-26T03:08:00+00:00",
                  [("EXV1.DE", "2026-09-23", "2026-09-25", "XETR")]),
    ]
    verdict = run(history, now=at("2026-09-26T03:10:00+00:00"), book_=fresh,
                  delivery=delivered(anchor="2026-09-18"), tmp_path=tmp_path)
    assert verdict["action_required"] is False
    assert verdict["events"][0]["consequence"] == ap.PRE_RELEASE_INPUT
    assert verdict["events"][0]["provenance"]["anchor"] == "2026-09-25"
    once_sent = run(history, now=at("2026-09-26T09:10:00+00:00"), book_=fresh,
                    delivery=delivered(anchor="2026-09-25"), tmp_path=tmp_path)
    assert once_sent["action_required"] is True


def test_a_session_the_instruction_never_ranked_on_is_not_an_invalidation(tmp_path):
    older = [
        probe_row("2026-09-18T20:12:00+00:00",
                  [("EXV1.DE", "2026-09-17", "2026-09-18", "XETR")]),
        probe_row("2026-09-20T03:08:00+00:00",
                  [("EXV1.DE", "2026-09-15", "2026-09-18", "XETR")]),
    ]
    verdict = run(older, now=at("2026-09-20T03:10:00+00:00"), tmp_path=tmp_path)
    assert verdict["events"][0]["consequence"] == ap.HISTORICAL_INPUT
    assert verdict["action_required"] is False


def test_an_unreadable_date_may_not_buy_silence(tmp_path):
    verdict = run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                  book_=book(decision="not-a-date", as_of="2026-09-18"),
                  tmp_path=tmp_path)
    assert verdict["action_required"] is True
    assert verdict["events"][0]["consequence"] == ap.PANEL_INPUT_WITHDRAWN


def test_the_message_says_what_is_affected_supersession_and_what_to_do(tmp_path):
    verdict = run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                  tmp_path=tmp_path)
    subject, text = ap.subject(verdict["events"]), ap.body(verdict["events"])
    assert subject.startswith("[ACTION REQUIRED]")
    assert "EXV1.DE 2026-09-18" in subject
    assert "WHAT IS AFFECTED" in text
    assert "Strategy D" in text and "EXV1.DE" in text
    assert "IS A PREVIOUS INSTRUCTION SUPERSEDED" in text
    assert "No. Nothing here recomputes a ranking" in text
    assert "WHAT TO DO" in text
    assert "Re-verify" in text
    assert "send_component_factsheet.py revise" in text
    assert "The fill this instruction names is 2026-09-21" in text
    assert f"Batch: {verdict['batch_id']}" in text


def test_a_constituent_of_a_ranked_panel_asks_for_verification(tmp_path):
    (tmp_path / "constituents_exv3.json").write_text(json.dumps(
        {"etf": "EXV3", "snapshots": {"2026-09-18": {"tickers": ["SAP.DE", "ASML.AS"]}}}),
        encoding="utf-8")
    history = [
        probe_row("2026-09-18T15:44:00+00:00",
                  [("SAP.DE", "2026-09-18", "2026-09-18", "XETR")]),
        probe_row("2026-09-20T03:08:00+00:00",
                  [("SAP.DE", "2026-09-16", "2026-09-18", "XETR")]),
    ]
    verdict = run(history, now=at("2026-09-20T03:10:00+00:00"), tmp_path=tmp_path)
    event = verdict["events"][0]
    assert event["consequence"] == ap.PANEL_INPUT_WITHDRAWN
    assert event["disposition"] == ap.NOTIFY
    assert event["dependency"]["sleeves"] == ["D"]
    assert event["dependency"]["panels"] == ["EXV3"]
    assert event["dependency"]["ranked"] is False
    assert "panel constituent" in ap.body(verdict["events"])


# ---------------------------------------------------------------------------
# 5. Duplicate suppression, and a recovery that means something
# ---------------------------------------------------------------------------
PROOF_0918 = (["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"], [])


def test_the_same_unresolved_incident_mails_once_after_confirmed_delivery(tmp_path):
    first = run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                tmp_path=tmp_path)
    assert first["events"][0]["disposition"] == ap.NOTIFY
    later = _ranked_withdrawal("2026-09-20T06:02:00+00:00")
    second = run(later, sent(first["events"]), now=at("2026-09-20T06:05:00+00:00"),
                 tmp_path=tmp_path)
    assert second["events"][0]["disposition"] == ap.REPEAT
    assert second["action_required"] is False
    assert second["recorded"] == 1


def test_a_consequence_that_grows_is_not_a_repeat(tmp_path):
    quiet = run(_ranked_withdrawal("2026-09-19T20:00:00+00:00"),
                now=at("2026-09-19T20:05:00+00:00"), delivery=undelivered(),
                tmp_path=tmp_path)
    assert quiet["events"][0]["disposition"] == ap.QUIET
    escalated = run(_ranked_withdrawal("2026-09-20T03:08:00+00:00"),
                    sent(quiet["events"]), now=at("2026-09-20T03:10:00+00:00"),
                    tmp_path=tmp_path)
    assert escalated["events"][0]["disposition"] == ap.NOTIFY
    assert escalated["action_required"] is True


def test_a_recovery_that_closes_an_alert_is_notified(tmp_path):
    opened = sent(run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                      tmp_path=tmp_path)["events"])
    restored = [probe_row("2026-09-20T12:04:00+00:00",
                          [("EXV1.DE", "2026-09-18", "2026-09-18", "XETR", PROOF_0918)])]
    verdict = run(restored, opened, now=at("2026-09-20T12:06:00+00:00"),
                  tmp_path=tmp_path)
    event = verdict["events"][0]
    assert event["kind"] == ap.RECOVERY
    assert event["disposition"] == ap.RECOVERY_NOTIFY
    assert verdict["recovery"] is True
    assert ap.subject(verdict["events"]).startswith("[RESOLVED]")
    assert "closes the ACTION REQUIRED alert" in ap.body(verdict["events"])


def test_a_recovery_after_the_fill_has_passed_is_not_announced(tmp_path):
    opened = sent(run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                      tmp_path=tmp_path)["events"])
    proof = (["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"], [])
    restored = [probe_row("2026-09-23T12:04:00+00:00",
                          [("EXV1.DE", "2026-09-22", "2026-09-22", "XETR", proof)])]
    verdict = run(restored, opened, now=at("2026-09-23T12:06:00+00:00"),
                  tmp_path=tmp_path)
    assert verdict["events"][0]["disposition"] == ap.RECOVERY_QUIET
    assert verdict["recovery"] is False


def test_an_incident_nobody_observes_again_is_closed_not_left_to_suppress(tmp_path):
    opened = sent(run(_ranked_withdrawal(), now=at("2026-09-20T03:10:00+00:00"),
                      tmp_path=tmp_path)["events"])
    much_later = at("2026-09-20T03:10:00+00:00") + timedelta(
        days=ap.INCIDENT_MAX_OPEN_DAYS + 1)
    verdict = run([probe_row(much_later.isoformat(), [("SPY", "2026-10-05", "2026-10-05")])],
                  opened, now=much_later, tmp_path=tmp_path)
    assert verdict["events"][0]["kind"] == ap.STALE
    assert verdict["action_required"] is False


def test_the_log_is_trimmed_without_losing_an_open_incident(tmp_path):
    path = tmp_path / "ops.jsonl"
    filler = [{"schema": 1, "recorded_at_utc": "2026-08-01T00:00:00+00:00",
               "kind": ap.RECOVERY, "incident": f"OLD{i}@2026-08-01",
               "ticker": f"OLD{i}", "session": "2026-08-01",
               "disposition": ap.RECOVERY_QUIET} for i in range(ap.MAX_OPS_ROWS + 50)]
    ap.record(path, filler)
    open_row = {"schema": 1, "recorded_at_utc": "2026-08-02T00:00:00+00:00",
                "kind": ap.RETRACTION, "incident": "EXV1.DE@2026-09-18",
                "ticker": "EXV1.DE", "session": "2026-09-18",
                "consequence": ap.RANKED_INPUT_WITHDRAWN, "disposition": ap.NOTIFY}
    ap.record(path, [open_row])
    rows = ap.read_ops_log(path)
    assert len(rows) <= ap.MAX_OPS_ROWS
    incidents = ap.incidents_from(rows)
    assert incidents["EXV1.DE@2026-09-18"].open is True
    assert incidents["EXV1.DE@2026-09-18"].notified is True


def test_an_unreadable_row_does_not_cost_the_rest_of_the_history(tmp_path):
    path = tmp_path / "ops.jsonl"
    path.write_text('{"incident": "A@2026-09-18", "kind": "retraction", '
                    '"disposition": "notify"}\n{ this is not json\n',
                    encoding="utf-8")
    assert len(ap.read_ops_log(path)) == 1


# ---------------------------------------------------------------------------
# 6. Sender routing facts the policy relies on (base sender, unchanged)
# ---------------------------------------------------------------------------
def test_a_preview_already_sent_does_not_resend(tmp_path):
    now = at("2026-09-19T10:30:00+00:00")           # Saturday 18:30 SGT
    decision = email_decision(
        now, anchor=week_final_anchor(now).isoformat(),
        core=Snapshot("core-id", True, week_final_anchor(now).isoformat(),
                      week_final_anchor(now).isoformat()),
        europe=Snapshot("europe-id", False, "2026-09-17", "2026-09-18"),
        sent={"anchor": week_final_anchor(now).isoformat(), "preview": "x"})
    assert decision["action"] == "wait"


# ---------------------------------------------------------------------------
# 7. Month boundaries, year boundaries and exchange holidays
# ---------------------------------------------------------------------------
def test_month_boundary_withdrawal_of_a_ranked_input(tmp_path):
    history = [
        probe_row("2026-07-31T20:12:00+00:00",
                  [("EXV1.DE", "2026-07-31", "2026-07-31", "XETR")]),
        probe_row("2026-08-02T03:08:00+00:00",
                  [("EXV1.DE", "2026-07-29", "2026-07-31", "XETR")]),
    ]
    verdict = run(history, now=at("2026-08-02T03:10:00+00:00"),
                  book_=book(decision="2026-07-31", fill="2026-08-03"),
                  delivery=delivered(anchor="2026-07-31"), tmp_path=tmp_path)
    assert verdict["events"][0]["consequence"] == ap.RANKED_INPUT_WITHDRAWN
    assert verdict["action_required"] is True
    assert verdict["events"][0]["incident"] == "EXV1.DE@2026-07-31"


def test_year_boundary_withdrawal_and_its_archived_counterpart(tmp_path):
    history = [
        probe_row("2026-12-31T20:12:00+00:00",
                  [("EXV1.DE", "2026-12-31", "2026-12-31", "XETR")]),
        probe_row("2027-01-02T03:08:00+00:00",
                  [("EXV1.DE", "2026-12-29", "2026-12-31", "XETR")]),
    ]
    live = run(history, now=at("2027-01-02T03:10:00+00:00"),
               book_=book(decision="2026-12-31", fill="2027-01-04"),
               delivery=delivered(anchor="2026-12-31"), tmp_path=tmp_path)
    assert live["events"][0]["consequence"] == ap.RANKED_INPUT_WITHDRAWN
    archived = run(history, now=at("2027-01-05T03:10:00+00:00"),
                   book_=book(decision="2026-12-31", fill="2027-01-04"),
                   delivery=delivered(anchor="2026-12-31"), tmp_path=tmp_path)
    assert archived["events"][0]["consequence"] == ap.ARCHIVED_RELEASE
    assert archived["action_required"] is False


def test_a_holiday_shortened_week_keeps_the_anchor_and_the_checkpoint_honest():
    saturday = at("2026-04-04T02:00:00+00:00")
    assert week_final_anchor(saturday) == date(2026, 4, 2)
    opens, final = review_window(saturday)
    assert (opens.isoformat(), final.isoformat()) == (
        "2026-04-05T18:00:00+08:00", "2026-04-06T06:00:00+08:00")


def test_a_holiday_delayed_fill_still_binds_the_alert(tmp_path):
    history = [
        probe_row("2026-09-04T20:12:00+00:00", [("SPY", "2026-09-04", "2026-09-04")]),
        probe_row("2026-09-07T03:08:00+00:00", [("SPY", "2026-09-02", "2026-09-04")]),
    ]
    kwargs = dict(book_=book(decision="2026-09-04", fill="2026-09-08"),
                  delivery=delivered(anchor="2026-09-04"), tmp_path=tmp_path)
    monday = run(history, now=at("2026-09-07T03:10:00+00:00"), **kwargs)
    assert monday["events"][0]["consequence"] == ap.RANKED_INPUT_WITHDRAWN
    # SPY is ranked by B and is also CSP1's price line for A.
    assert "B" in monday["events"][0]["dependency"]["sleeves"]
    after = run(history, now=at("2026-09-09T03:10:00+00:00"), **kwargs)
    assert after["events"][0]["consequence"] == ap.ARCHIVED_RELEASE


def test_the_earliest_fill_among_affected_sleeves_binds(tmp_path):
    b = book()
    b["sleeves"][3]["fill_date"] = "2026-09-21"      # D, Xetra
    b["sleeves"][1]["fill_date"] = "2026-09-22"      # B, NYSE
    provenance = ap.read_provenance(b, delivered(), ("B", "D"))
    assert provenance.fill_date == date(2026, 9, 21)
    assert provenance.fill_passed(date(2026, 9, 22)) is True
    assert provenance.fill_passed(date(2026, 9, 21)) is False


# ---------------------------------------------------------------------------
# The command line and the workflow contract
# ---------------------------------------------------------------------------
def _cli_files(tmp_path):
    vendor = tmp_path / "vendor.jsonl"
    vendor.write_text("\n".join(json.dumps(r) for r in _ranked_withdrawal()) + "\n",
                      encoding="utf-8")
    for name, payload in (("book.json", book()), ("delivery.json", delivered())):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    return ["--vendor-log", str(vendor), "--ops-log", str(tmp_path / "ops.jsonl"),
            "--book", str(tmp_path / "book.json"),
            "--delivery", str(tmp_path / "delivery.json"),
            "--data-dir", str(tmp_path), "--now", "2026-09-20T03:10:00Z"]


def test_the_cli_rehearses_without_writing(tmp_path, monkeypatch, capsys):
    args = _cli_files(tmp_path)
    out = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert ap.main(args) == 0
    assert "rehearsal: nothing written" in capsys.readouterr().out
    assert not (tmp_path / "ops.jsonl").exists()
    outputs = out.read_text(encoding="utf-8")
    assert "action_required=true" in outputs
    assert "mail=true" in outputs
    # Default routing is the tripwire's mailing set and subject.
    assert "subject=[WARN] Vendor RETRACTED a settled bar" in outputs


def test_the_cli_routes_a_batch_and_records_only_that_batch(tmp_path):
    args = _cli_files(tmp_path)[:-2]  # production route uses the wall clock
    batch_path = tmp_path / "batch.json"
    assert ap.main(args + ["--batch-out", str(batch_path)]) == 0
    assert not (tmp_path / "ops.jsonl").exists()      # routing never writes the log
    batch = ap.read_batch(batch_path)
    assert batch["mail"] is True and batch["batch_id"]
    ops = str(tmp_path / "ops.jsonl")
    assert ap.main(["--ops-log", ops, "--record-batch", str(batch_path),
                    "--mail-outcome", "failure"]) == 0
    rows = ap.read_ops_log(Path(ops))
    assert [r["delivery"] for r in rows] == [ap.DELIVERY_FAILED]
    assert rows[0]["event_id"] == batch["events"][0]["event_id"]


def test_the_cli_refuses_a_rehearsal_batch(tmp_path):
    args = _cli_files(tmp_path)
    batch_path = tmp_path / "batch.json"
    assert ap.main(args + ["--batch-out", str(batch_path)]) == 1
    assert not batch_path.exists()
    assert not (tmp_path / "ops.jsonl").exists()


def test_the_workflow_routes_mails_records_and_commits_in_that_order():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
                / "vendor_probe.yml").read_text(encoding="utf-8")
    assert "python scripts/alert_policy.py --batch-out" in workflow
    assert "--record-batch" in workflow and "--mail-outcome" in workflow
    assert "steps.mail.outcome" in workflow
    assert "steps.policy.outputs.mail == 'true'" in workflow
    assert "data/ops_alert_log.jsonl" in workflow
    assert (workflow.index("id: policy") < workflow.index("id: mail")
            < workflow.index("--record-batch")
            < workflow.index("Commit the observation"))
    # Detection stays, and the tripwire still mails if routing fails.
    assert "python scripts/check_vendor_probe.py" in workflow
    assert "steps.policy.outcome != 'success' && steps.guard.outputs.retracted == 'true'" in workflow
    # The record step never re-evaluates: it is given the frozen batch only.
    record_step = workflow[workflow.index("--record-batch") - 200:workflow.index("--mail-outcome") + 60]
    assert "--now" not in record_step and "--routing" not in record_step
    # Consequence routing is not enabled by the workflow.
    assert "--routing consequence" not in workflow


def test_the_operations_log_is_a_committed_artefact():
    root = Path(__file__).resolve().parents[1]
    assert (root / "data" / "ops_alert_log.jsonl").exists()
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "data/ops_alert_log.jsonl" not in ignore
    assert ap.OPS_LOG.parent.name == "data"
