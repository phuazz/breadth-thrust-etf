"""Two-stage factsheet sender with a durable pre-send reservation.

The plan command defaults to a no-send rehearsal. --reserve requires the
workflow to commit and push the reservation BEFORE send. Any interrupted,
partial or uncertain send then requires operator reconciliation, not a retry.
Python datetime months are 1-indexed; exchange calendars define the anchor.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, formataddr
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess

from component_publication import SGT, Snapshot, email_decision, email_wording, review_window
from component_release import ROOT, MANIFEST, read, write, digest, verify
from nyse_sessions import week_final_anchor

LEDGER = "docs/component_delivery.json"
OUT = ".component-mail"
SEND_ACTIONS = {"preview", "regular", "d_update"}
# A presentation revision is not one of the ordinary stages. It never enters
# SEND_ACTIONS: the scheduled sender must not be able to reach this path.
REVISION_ACTION = "revision"
REVISION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def ledger_at(root):
    path = root / LEDGER
    if not path.exists():
        return {"schema": 1, "anchors": {}}
    value = read(path)
    if value.get("schema") != 1 or not isinstance(value.get("anchors"), dict):
        raise ValueError("delivery ledger is invalid; reconcile before sending")
    return value


def plan(root=ROOT, now=None, committed=False):
    now = now or datetime.now(timezone.utc)
    anchor = week_final_anchor(now).isoformat()
    ledger = ledger_at(root)
    # A prior unresolved reservation blocks new weeks too: delivery is uncertain.
    if any(s.get("pending") for s in ledger["anchors"].values()):
        return {"action": "alert", "reason": "Unconfirmed email attempt; reconcile delivery before retrying."}, None
    if (root / "docs/factsheet_hold.json").exists():
        return {"action": "wait", "reason": "Operator hold is in place."}, None
    sent = ledger["anchors"].get(anchor, {})
    # Respect an old-format confirmed send during migration, never double-send.
    old = root / "docs/factsheet_published.json"
    if not sent and old.exists() and read(old).get("anchor") == anchor:
        return {"action": "wait", "reason": "This anchor was already sent by the legacy workflow."}, None
    if not (root / MANIFEST).exists() or read(root / MANIFEST).get("anchor") != anchor:
        decision = email_decision(now, anchor=anchor, core=Snapshot("", False, "", anchor),
            europe=Snapshot("", False, "", anchor), sent=sent)
        return decision, None
    release = verify(root, now, committed=committed)
    europe = next(s for s in release["book"]["sleeves"] if s["sleeve"] == "D")
    decision = email_decision(now, anchor=anchor,
        core=Snapshot(release["core_identity"], True, anchor, anchor),
        europe=Snapshot(release["europe_identity"], release["d_ready"],
                        europe["last_completed_session"], europe["decision_session_for_fill"]), sent=sent)
    return decision, release


def render(decision, release, include_unchanged=False):
    from component_factsheet_view import render_html
    return render_html(decision, release, include_unchanged)


def prepare(root=ROOT, now=None, reserve=False, committed=False):
    now = now or datetime.now(timezone.utc)
    decision, release = plan(root, now, committed=committed)
    if decision["action"] not in SEND_ACTIONS:
        return decision
    from component_factsheet_view import verified_context, render_pdf, render_text
    release = {**release, "presentation": verified_context(root, release, committed)}
    html = render(decision, release)
    wording = email_wording(decision)
    candidate = {"decision": decision, "release_identity": release["identity"],
        "subject": f"{wording['subject']} · USD Multi-Strategy ETF Portfolio · {release['anchor']}", "html": html,
        "book_html": render(decision, release, include_unchanged=True), "book": release["book"]}
    candidate["text"] = render_text(decision, release)
    candidate["pdf_base64"] = base64.b64encode(render_pdf(decision, release)).decode("ascii")
    candidate["pdf_filename"] = f"factsheet_{release['anchor']}_{decision['action']}.pdf"
    candidate["id"] = digest(candidate)
    write(root / OUT / "candidate.json", candidate)
    (root / OUT / "preview.html").write_text(html, encoding="utf-8")
    (root / OUT / candidate["pdf_filename"]).write_bytes(base64.b64decode(candidate["pdf_base64"]))
    if reserve:
        ledger = ledger_at(root)
        anchor = release["anchor"]
        state = ledger["anchors"].setdefault(anchor, {"anchor": anchor})
        state["pending"] = {"id": candidate["id"], "action": decision["action"], "reserved_at": now.isoformat()}
        write(root / LEDGER, ledger)
    return decision


def smtp_send(candidate, env):
    sender = env["GMAIL_USER"]
    recipients = [addr for _, addr in getaddresses([env["RECIPIENT_EMAIL"]]) if addr]
    if not recipients or any("@" not in a or "\n" in a or "\r" in a for a in recipients):
        raise ValueError("recipient configuration is empty or invalid")
    msg = EmailMessage()
    msg["From"] = formataddr(("USD Multi-Strategy ETF Factsheet", sender))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = candidate["subject"]
    msg["Message-ID"] = f"<component-{candidate['id']}@breadth-thrust-etf.invalid>"
    wording = email_wording(candidate["decision"])
    msg.set_content(candidate.get("text") or "\n\n".join(wording.values()) + "\n\nThe complete proposed model book is attached.")
    msg.add_alternative(candidate["html"], subtype="html")
    if candidate.get("pdf_base64"):
        msg.add_attachment(base64.b64decode(candidate["pdf_base64"], validate=True),
                           maintype="application", subtype="pdf", filename=candidate["pdf_filename"])
    msg.add_attachment(candidate["book_html"], subtype="html", filename="complete-proposed-book.html")
    from component_release import canonical
    msg.add_attachment(canonical(candidate["book"]), maintype="application", subtype="json",
                       filename="proposed-model-book.json")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60, context=ssl.create_default_context()) as smtp:
        smtp.login(sender, env["GMAIL_APP_PASSWORD"])
        refused = smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
        if refused:
            raise RuntimeError("Some recipients were refused; reconcile the reserved attempt. Do not resend automatically.")


def send(root=ROOT, now=None, transport=smtp_send, env=None, committed=False):
    now = now or datetime.now(timezone.utc)
    if (root / "docs/factsheet_hold.json").exists():
        raise ValueError("operator hold is in place")
    candidate = read(root / OUT / "candidate.json")
    if candidate["id"] != digest({k: v for k, v in candidate.items() if k != "id"}):
        raise ValueError("mail payload changed after reservation")
    if candidate["decision"].get("action") not in SEND_ACTIONS:
        raise ValueError("the prepared payload is not an ordinary factsheet stage")
    release = verify(root, now, committed=committed)
    if release["identity"] != candidate["release_identity"] or release["book"] != candidate["book"]:
        raise ValueError("release changed after reservation")
    ledger = ledger_at(root)
    state = ledger["anchors"][release["anchor"]]
    if state.get("pending", {}).get("id") != candidate["id"]:
        raise ValueError("no matching durable reservation")
    if committed:
        # The workflow must have pushed the reservation, not merely written it
        # in an ephemeral runner. A crash after SMTP then cannot lose the lock.
        import json
        remote = subprocess.run(["git", "show", f"origin/main:{LEDGER}"], cwd=root,
                                capture_output=True, check=True).stdout
        remote_state = json.loads(remote)["anchors"][release["anchor"]]
        if remote_state.get("pending", {}).get("id") != candidate["id"]:
            raise ValueError("reservation is not present on the remote tracking branch")
    # Recheck the actual send clock, not just the workflow start clock.
    decision = candidate["decision"]
    europe = next(s for s in release["book"]["sleeves"] if s["sleeve"] == "D")
    rechecked = email_decision(now, anchor=release["anchor"],
        core=Snapshot(release["core_identity"], True, release["anchor"], release["anchor"]),
        europe=Snapshot(release["europe_identity"], release["d_ready"],
                        europe["last_completed_session"], europe["decision_session_for_fill"]), sent=state)
    if rechecked["action"] != decision["action"]:
        raise ValueError("send eligibility changed after reservation; reconcile without sending")
    transport(candidate, os.environ if env is None else env)
    state.pop("pending")
    state["core"] = release["core_identity"]
    state["europe"] = release["europe_identity"]
    state[decision["action"]] = ("d_hold" if decision["d_hold"] else "all_ready") if decision["action"] == "regular" else release["identity"]
    state["last_confirmed_at"] = now.isoformat()
    write(root / LEDGER, ledger)
    if decision["action"] in ("regular", "d_update"):
        write(root / "docs/factsheet_published.json", {"anchor": release["anchor"], "published_at_utc": now.isoformat()})


def blocked(reason):
    return {"action": "blocked", "reason": reason}, None


def plan_revision(root=ROOT, now=None, revision=None, committed=False, allow_pending=None,
                  late_authority=None):
    """Authorise a relabelled restatement of an already-confirmed anchor.

    Every branch refuses by default. This path cannot change an instruction,
    a recipient list, a schedule or an existing receipt: it re-sends the same
    sealed book under an explicit revision subject and records that fact
    beside the untouched original confirmation.

    late_authority is a named owner waiver, recorded in the receipt, of TWO
    guards and no others: the one-revision-per-anchor limit and the weekend
    review checkpoint. It is a reason string, not a boolean, because a
    receipt that cannot say WHY a guard was stood down is not a receipt. It
    is not a force-send: unchanged core and Europe identities, a verifying
    release, a settled D, no operator hold, no outstanding attempt, the
    current anchor and an unexpired fill date all still refuse, and the
    waiver raises the ceiling to two revisions, not to any number.
    """
    now = now or datetime.now(timezone.utc)
    revision = str(revision or "").strip()
    if not REVISION_ID.match(revision):
        return blocked("a revision identifier of [A-Za-z0-9._-] is required")
    if (root / "docs/factsheet_hold.json").exists():
        return blocked("operator hold is in place")
    anchor = week_final_anchor(now).isoformat()
    ledger = ledger_at(root)
    outstanding = [s["pending"] for s in ledger["anchors"].values() if s.get("pending")]
    # Only this revision's own durable reservation may be outstanding; any
    # other unresolved attempt means delivery is uncertain somewhere.
    if any(not (allow_pending and p.get("id") == allow_pending
                and p.get("action") == REVISION_ACTION and p.get("revision") == revision)
           for p in outstanding):
        return blocked("an unconfirmed delivery attempt is outstanding; reconcile before revising")
    state = ledger["anchors"].get(anchor)
    if not state or state.get("anchor") != anchor:
        return blocked("no confirmed delivery exists for the current anchor")
    if not state.get("regular"):
        return blocked("the ordinary weekly delivery has not completed for this anchor")
    if state["regular"] == "d_hold" and not state.get("d_update"):
        return blocked("the D follow-up is still outstanding; revise only a settled week")
    already = sorted(state.get("revisions") or {})
    if revision in already:
        return blocked(f"revision {revision!r} was already delivered for this anchor")
    if already and not late_authority:
        return blocked(f"a presentation revision was already delivered for this anchor: {already}")
    if len(already) >= 2:
        return blocked(f"two presentation revisions already stand for this anchor: {already}")
    if not (root / MANIFEST).exists() or read(root / MANIFEST).get("anchor") != anchor:
        return blocked("the sealed release is absent or belongs to another week")
    release = verify(root, now, committed=committed)
    if not release["d_ready"]:
        return blocked("D is not verified; a revision must restate a settled book")
    if (state.get("core") != release["core_identity"]
            or state.get("europe") != release["europe_identity"]):
        return blocked("the sealed instruction identities differ from what was delivered")
    local = now.astimezone(SGT)
    if local > review_window(now)[1] and not late_authority:
        return blocked("the weekend review checkpoint has passed")
    if any(date.fromisoformat(s["fill_date"]) < local.date() for s in release["book"]["sleeves"]):
        return blocked("a proposed fill date has passed; the instruction would be stale")
    return {"action": REVISION_ACTION, "audience": "distribution", "anchor": anchor,
            "revision": revision, "d_hold": False,
            "core_identity": release["core_identity"],
            "europe_identity": release["europe_identity"],
            "late_authority": late_authority or None,
            "reason": ("presentation revision of the confirmed all-ready book"
                       + (f"; late second revision on owner authority: {late_authority}"
                          if late_authority else ""))}, release


def prepare_revision(root=ROOT, now=None, revision=None, reserve=False, committed=False,
                     late_authority=None):
    now = now or datetime.now(timezone.utc)
    decision, release = plan_revision(root, now, revision, committed=committed,
                                      late_authority=late_authority)
    if decision["action"] != REVISION_ACTION:
        return decision
    from component_factsheet_view import verified_context, render_pdf, render_text
    release = {**release, "presentation": verified_context(root, release, committed)}
    html = render(decision, release)
    wording = email_wording(decision)
    candidate = {"decision": decision, "release_identity": release["identity"],
        "subject": f"{wording['subject']} · USD Multi-Strategy ETF Portfolio · {release['anchor']}",
        "html": html, "book_html": render(decision, release, include_unchanged=True),
        "book": release["book"]}
    candidate["text"] = render_text(decision, release)
    candidate["pdf_base64"] = base64.b64encode(render_pdf(decision, release)).decode("ascii")
    candidate["pdf_filename"] = f"factsheet_{release['anchor']}_revision-{decision['revision']}.pdf"
    candidate["id"] = digest(candidate)
    write(root / OUT / "candidate.json", candidate)
    (root / OUT / "preview.html").write_text(html, encoding="utf-8")
    (root / OUT / candidate["pdf_filename"]).write_bytes(base64.b64decode(candidate["pdf_base64"]))
    if reserve:
        ledger = ledger_at(root)
        state = ledger["anchors"][release["anchor"]]
        # The same durable reservation the ordinary stages use, so an
        # interrupted revision blocks every later send until reconciled.
        state["pending"] = {"id": candidate["id"], "action": REVISION_ACTION,
                            "revision": decision["revision"], "reserved_at": now.isoformat()}
        write(root / LEDGER, ledger)
    return decision


def send_revision(root=ROOT, now=None, revision=None, transport=smtp_send, env=None,
                  committed=False, late_authority=None):
    now = now or datetime.now(timezone.utc)
    candidate = read(root / OUT / "candidate.json")
    if candidate["id"] != digest({k: v for k, v in candidate.items() if k != "id"}):
        raise ValueError("mail payload changed after reservation")
    decision = candidate["decision"]
    if decision.get("action") != REVISION_ACTION:
        raise ValueError("the prepared payload is not a presentation revision")
    revision = str(revision or "").strip() or decision.get("revision")
    if revision != decision.get("revision"):
        raise ValueError("revision identifier does not match the reserved payload")
    # The waiver must match what was reserved: a payload prepared without
    # authority cannot acquire it at send time, and vice versa.
    late_authority = late_authority or decision.get("late_authority")
    if (late_authority or None) != (decision.get("late_authority") or None):
        raise ValueError("late authority does not match the reserved payload")
    rechecked, release = plan_revision(root, now, revision, committed=committed,
                                       allow_pending=candidate["id"],
                                       late_authority=late_authority)
    if rechecked["action"] != REVISION_ACTION:
        raise ValueError(f"revision eligibility changed after reservation: {rechecked['reason']}")
    if release["identity"] != candidate["release_identity"] or release["book"] != candidate["book"]:
        raise ValueError("release changed after reservation")
    ledger = ledger_at(root)
    state = ledger["anchors"][release["anchor"]]
    pending = state.get("pending") or {}
    if pending.get("id") != candidate["id"] or pending.get("revision") != revision:
        raise ValueError("no matching durable reservation")
    if committed:
        # The reservation must already be on the remote, exactly as the
        # ordinary stages require, so a crash after SMTP cannot lose the lock.
        import json
        remote = subprocess.run(["git", "show", f"origin/main:{LEDGER}"], cwd=root,
                                capture_output=True, check=True).stdout
        remote_pending = json.loads(remote)["anchors"][release["anchor"]].get("pending", {})
        if remote_pending.get("id") != candidate["id"] or remote_pending.get("revision") != revision:
            raise ValueError("reservation is not present on the remote tracking branch")
    transport(candidate, os.environ if env is None else env)
    # The original confirmation is evidence of a delivered instruction and is
    # left exactly as it stands; the revision is recorded beside it.
    state.pop("pending")
    receipt = {"candidate": candidate["id"], "release": release["identity"],
               "subject": candidate["subject"], "confirmed_at": now.isoformat()}
    if late_authority:
        # A stood-down guard that leaves no trace is indistinguishable from a
        # guard that never ran. The reason travels with the receipt.
        receipt["late_authority"] = late_authority
    state.setdefault("revisions", {})[revision] = receipt
    write(root / LEDGER, ledger)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "send", "revise", "revise-send"))
    parser.add_argument("--reserve", action="store_true")
    parser.add_argument("--revision", default="")
    parser.add_argument("--authorised-late", dest="late_authority", default="",
                        help="Owner reason for standing down the one-revision limit and the "
                             "review checkpoint. Recorded in the delivery receipt.")
    args = parser.parse_args()
    if args.operation == "send":
        send(committed=True)
        print("SMTP accepted the factsheet for all configured recipients; delivery ledger updated.")
        return
    if args.operation == "revise-send":
        send_revision(revision=args.revision, committed=True,
                      late_authority=args.late_authority.strip() or None)
        print("SMTP accepted the revised presentation for all configured recipients; revision recorded.")
        return
    if args.operation == "revise":
        decision = prepare_revision(revision=args.revision, reserve=args.reserve, committed=True,
                                    late_authority=args.late_authority.strip() or None)
        print(f"{decision['action']}: {decision['reason']}")
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write(f"send={'true' if args.reserve and decision['action'] == REVISION_ACTION else 'false'}\n")
        if decision["action"] != REVISION_ACTION:
            raise SystemExit(1)
        return
    decision = prepare(reserve=args.reserve, committed=True)
    print(f"{decision['action']}: {decision['reason']}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"send={'true' if args.reserve and decision['action'] in SEND_ACTIONS else 'false'}\n")
            handle.write(f"alert={'true' if decision['action'] == 'alert' else 'false'}\n")


if __name__ == "__main__":
    main()
