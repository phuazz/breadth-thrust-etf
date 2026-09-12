"""Fail-closed policy for independent core/Europe publication.

This module does not fetch, send, schedule or waive existing checks. Callers
must supply independently verified component snapshots. Python datetime months
are 1-indexed; all time arithmetic uses datetime and the exchange calendars.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
import math

SGT = ZoneInfo("Asia/Singapore")


@dataclass(frozen=True)
class Snapshot:
    """Verification receipt, not merely a component's latest observation date.

    identity must cover the exact approved inputs and outputs. A changed
    snapshot requires revalidation; the sender must verify its hashes again.
    """
    identity: str
    verified: bool
    decision_session: str
    required_session: str

    @property
    def ready(self) -> bool:
        try:
            decision = date.fromisoformat(self.decision_session)
            required = date.fromisoformat(self.required_session)
        except (TypeError, ValueError):
            return False
        return self.verified is True and bool(self.identity) and decision == required


def review_window(now: datetime) -> tuple[datetime, datetime]:
    """Sunday 18:00 to Monday 06:00 SGT for the upcoming/current fill week.

    The second instant is an email review checkpoint, NOT an order deadline.
    Weekdays use datetime's Monday=0 indexing, not month indexing.
    """
    if now.tzinfo is None:
        raise ValueError("an aware timestamp is required")
    local = now.astimezone(SGT)
    monday = local.date() - timedelta(days=local.weekday())
    if local.weekday() != 0:
        monday += timedelta(days=7)
    sunday = monday - timedelta(days=1)
    return (datetime.combine(sunday, time(18), SGT),
            datetime.combine(monday, time(6), SGT))


def email_decision(now: datetime, *, anchor: str, core: Snapshot,
                   europe: Snapshot, sent: dict[str, str],
                   operator_hold: bool = False) -> dict:
    """Choose one notification; no implicit permission to send or publish.

    Core includes A/B/C AND both overlay decisions. ``sent`` is the durable
    ledger for this anchor, written only after a confirmed successful send.
    Both preview and regular/follow-up factsheets go to the existing
    distribution, with explicit D HOLD labelling when necessary. Operational
    alerts go only to the operator.
    No automatic reissue of changed core orders after a regular factsheet:
    that requires review rather than an unnoticed version change.
    """
    regular_at, final_check = review_window(now)
    local = now.astimezone(SGT)
    result = {"action": "wait", "audience": None, "d_hold": not europe.ready,
              "anchor": anchor, "core_identity": core.identity,
              "europe_identity": europe.identity}
    if not anchor or operator_hold:
        return {**result, "reason": "missing anchor or operator hold"}
    from nyse_sessions import week_final_anchor
    if (anchor != week_final_anchor(now).isoformat()
            or core.required_session != anchor
            or (sent and sent.get("anchor") != anchor)):
        return {**result, "action": "alert", "audience": "operator",
                "reason": "wrong week or ledger anchor; no factsheet"}
    if local.weekday() not in (5, 6, 0):
        return {**result, "reason": "outside weekend review window"}
    if not core.ready:
        return {**result, "action": "alert" if local >= regular_at else "wait",
                "audience": "operator" if local >= regular_at else None,
                "reason": "core or overlay verification incomplete; no factsheet"}
    if sent.get("core") and sent["core"] != core.identity:
        return {**result, "action": "alert", "audience": "operator",
                "reason": "core changed after distribution; review required"}
    if sent.get("regular"):
        if (europe.ready and sent.get("europe") and sent["europe"] != europe.identity
                and (sent["regular"] == "all_ready" or sent.get("d_update"))):
            return {**result, "action": "alert", "audience": "operator",
                    "reason": "D changed after its confirmed instruction; review required"}
        if (sent["regular"] == "d_hold" and europe.ready
                and not sent.get("d_update") and local <= final_check):
            return {**result, "action": "d_update", "audience": "distribution",
                    "reason": "D now verified; same approved core snapshot"}
        if sent["regular"] == "d_hold" and europe.ready and not sent.get("d_update") and local > final_check:
            return {**result, "action": "alert", "audience": "operator",
                    "reason": "D became ready after the review checkpoint; operator review required"}
        return {**result, "reason": "already distributed or update window closed"}
    if local > final_check:
        return {**result, "action": "alert", "audience": "operator",
                "reason": "missed review window; no automatic late factsheet"}
    if local >= regular_at or europe.ready:
        return {**result, "action": "regular", "audience": "distribution",
                "reason": "all ready" if europe.ready else "bounded wait ended; D HOLD"}
    if not sent.get("preview"):
        return {**result, "action": "preview", "audience": "distribution",
                "reason": "core ready; Europe pending"}
    return {**result, "reason": "preview already sent; waiting for Europe"}


def email_wording(decision: dict) -> dict[str, str]:
    """Plain-language cover text for a verified snapshot, never a new signal.

    All factsheet recipients see the same status. The attachment and position
    lines must come from the sealed snapshot checked by the eventual sender.
    """
    action = decision.get("action")
    if action == "preview":
        return {
            "subject": "Initial factsheet — A–C ready; D pending",
            "heading": "A–C and portfolio overlays are ready for review",
            "summary": "Strategies A–C, the EM tilt and the breadth gate have been verified. "
                       "Strategy D is still waiting for complete data.",
            "difference": "This is the initial update. A later email will confirm D's status "
                          "and clearly identify any changes.",
            "d_instruction": "Keep D's existing selection. Any portfolio-level risk adjustment "
                             "is shown separately; no new D selection is proposed.",
        }
    if action in ("regular", "d_update"):
        if decision.get("d_hold") is True:
            return {
                "subject": "Weekly factsheet — D remains on HOLD",
                "heading": "A–C ready; D remains on HOLD",
                "summary": "Strategies A–C and the portfolio overlays are verified. "
                           "D's data is still incomplete, so no new D selection is proposed.",
                "difference": "If you received the initial update, the verified A–C and overlay "
                              "instructions are unchanged. This email confirms D remains on HOLD.",
                "d_instruction": "Keep D's existing selection. Any portfolio-level risk adjustment "
                                 "is shown separately.",
            }
        return {
            "subject": "Weekly factsheet — all strategies ready",
            "heading": "D is now ready; all strategies are verified",
            "summary": "Strategy D's required data is complete. Its proposed changes are now included.",
            "difference": "If you received an earlier update, the verified A–C and overlay "
                          "instructions are unchanged. Review the D changes in this email.",
            "d_instruction": "Review D's proposed changes alongside the previously verified instructions.",
        }
    raise ValueError("factsheet wording requires an approved send decision")


def resize_held_basket(held: dict[str, float], target_nav: float) -> dict[str, float]:
    """Preserve selection and relative weights; apply only an approved budget.

    No new member, stale re-ranking or redistribution to other sleeves. This
    does not certify execution prices or authorise a trade. A caller must keep
    an overlay adjustment distinct from a ranking HOLD in its instruction.
    """
    if (not math.isfinite(target_nav) or not 0 <= target_nav <= 1
            or any(not math.isfinite(v) or v < 0 for v in held.values())):
        raise ValueError("invalid NAV weights")
    total = sum(held.values())
    if total > 1 + 1e-9:
        raise ValueError("held basket exceeds NAV")
    if total == 0:
        if target_nav != 0:
            raise ValueError("cannot create an allocation without a held basket")
        return dict(held)
    return {name: value / total * target_nav for name, value in held.items()}
