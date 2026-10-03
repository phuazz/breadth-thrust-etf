"""Explicit owner-reviewed corrections to a preview-origin book.

No acquisition or rebuild. Prepare writes an isolated candidate; reserve and send
are separate approval-bound operations. Live send commits/pushes an attempt lock
BEFORE SMTP. Python months are 1-indexed.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone, date
from email.utils import getaddresses
import json
import os
from pathlib import Path
import subprocess

import component_release as cr
import send_component_factsheet as sender
from component_publication import review_window, SGT, email_wording
from nyse_sessions import week_final_anchor

ACTION = "preview_supersession"
POLICIES = ("suppress_unchanged", "ordinary_unchanged")


def archived_preview(root, identity):
    """Resolve an exact delivered seal, not a newer equivalent book."""
    commits = subprocess.run(["git", "log", "--format=%H", "--", cr.MANIFEST],
                             cwd=root, check=True, capture_output=True, text=True).stdout.split()
    for commit in commits:
        shown = subprocess.run(["git", "show", f"{commit}:{cr.MANIFEST}"], cwd=root,
                               capture_output=True)
        if shown.returncode:
            continue
        value = json.loads(shown.stdout)
        if value.get("identity") == identity:
            return value
    raise ValueError("the exact delivered release cannot be reconstructed")


def recipients(values):
    parsed = [address for _, address in getaddresses(values)]
    if (not parsed or len(set(a.lower() for a in parsed)) != len(parsed)
            or any("@" not in a or "\n" in a or "\r" in a for a in parsed)):
        raise ValueError("explicit unique recipient addresses are required")
    return sorted(parsed, key=str.lower)


def plan(root, *, expected_preview, authority, supersession, allowed_holds,
         followup_policy, to, now=None, committed=True, prior=None, pending_id=None,
         expected_previous=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("an aware review clock is required")
    if not authority.strip() or not sender.REVISION_ID.fullmatch(supersession):
        raise ValueError("named owner authority and a unique identifier are required")
    if followup_policy not in POLICIES:
        raise ValueError("an explicit approved follow-up policy is required")
    allowed = sorted(set(allowed_holds))
    if any(s not in "ABCD" or len(s) != 1 for s in allowed):
        raise ValueError("allowed HOLDs must be explicit A/B/C/D sleeve identifiers")
    if (root / "docs/factsheet_hold.json").exists():
        raise ValueError("operator hold is in place")
    anchor = week_final_anchor(now).isoformat()
    ledger = sender.ledger_at(root)
    for ledger_anchor, state in ledger["anchors"].items():
        pending = state.get("pending")
        if pending and not (ledger_anchor == anchor and pending_id and pending.get("id") == pending_id
                            and pending.get("action") == ACTION
                            and pending.get("supersession") == supersession):
            raise ValueError("unconfirmed delivery attempt; reconcile before proceeding")
    state = ledger["anchors"].get(anchor, {})
    if not expected_preview or state.get("preview") != expected_preview:
        raise ValueError("expected preview does not match the current anchor receipt")
    if not state.get("last_confirmed_at") or state.get("anchor") != anchor:
        raise ValueError("preview delivery is not confirmed")
    receipts = state.get("preview_supersessions") or {}
    if supersession in receipts:
        raise ValueError("a correction with this identifier already exists; no duplicate send")
    subsequent = bool(receipts or state.get("regular") or state.get("d_update") or state.get("restatements"))
    if subsequent and not expected_previous:
        raise ValueError("the exact latest delivered release is required for a later correction")
    expected_previous = expected_previous or expected_preview
    recorded_previous = state.get("latest_delivery_release") or (expected_preview if not subsequent else None)
    if recorded_previous and expected_previous != recorded_previous:
        raise ValueError("expected previous release is not the latest delivered release")
    if now.astimezone(SGT).weekday() not in (5, 6, 0):
        raise ValueError("outside the current anchor correction window")
    # The automatic deadlines do not authorise late mail. This route always
    # requires fresh owner approval and an unexpired fill date, like the
    # existing settled-week restatement path.
    release = cr.verify(root, now, committed=committed)
    if release["anchor"] != anchor:
        raise ValueError("release belongs to another anchor")
    held = sorted(cr.held_sleeves_of(release))
    if held != allowed:
        raise ValueError(f"remaining HOLDs {held} do not exactly match owner-reviewed scope {allowed}")
    for sleeve in release["book"]["sleeves"]:
        if date.fromisoformat(sleeve["fill_date"]) < now.astimezone(SGT).date():
            raise ValueError("proposed fill date has passed")
        if (sleeve["status"] == "READY"
                and sleeve["decision_session"] != sleeve["decision_session_for_fill"]):
            raise ValueError("a READY sleeve does not use the required close")
    prior = prior if prior is not None else archived_preview(root, expected_previous)
    if (prior.get("identity") != expected_previous or not sender._reconstructs(
            prior, anchor, state.get("core"), state.get("europe"))):
        raise ValueError("latest delivered seal or delivered identities do not reconstruct")
    released, imposed, lines = sender.restatement_changes(prior, release)
    if not (released or imposed or lines):
        raise ValueError("instruction unchanged; no supersession is justified")
    original = {k: copy.deepcopy(v) for k, v in state.items() if k != "pending"}
    decision = {"action": "restatement", "delivery_mode": ACTION,
        "audience": "distribution", "anchor": anchor, "supersession": supersession,
        "authority": authority.strip(), "expected_preview": expected_preview,
        "expected_previous": expected_previous, "protocol_version": 2,
        "is_late_correction": subsequent,
        "after_review_checkpoint": now > review_window(now)[1],
        "followup_policy": followup_policy, "allowed_holds": allowed,
        "recipients": recipients(to), "d_hold": not release["d_ready"],
        "core_held": [s for s in held if s != "D"], "first_issue": [],
        "core_identity": release["core_identity"], "europe_identity": release["europe_identity"],
        "supersedes": {"core": state["core"], "europe": state["europe"]},
        "prior_release": prior["identity"], "original_delivery_state": original,
        "released_holds": released, "imposed_holds": imposed, "restated_lines": lines,
        "reason": "owner-reviewed correction of the latest confirmed instruction"}
    return decision, release


def prepare(root, **kwargs):
    """Render once; never reserve, commit, publish or send."""
    decision, release = plan(root, **kwargs)
    from component_factsheet_view import verified_context, render_pdf, render_text
    import base64
    context = {**release, "presentation": verified_context(root, release, kwargs.get("committed", True))}
    wording = email_wording(decision)
    candidate = {"decision": decision, "release_identity": release["identity"],
        "subject": f"{wording['subject']} · USD Multi-Strategy ETF Portfolio · {release['anchor']}",
        "html": sender.render(decision, context),
        "book_html": sender.render(decision, context, include_unchanged=True),
        "text": render_text(decision, context), "book": release["book"],
        "pdf_base64": base64.b64encode(render_pdf(decision, context)).decode("ascii"),
        "pdf_filename": f"factsheet_{release['anchor']}_preview-supersession-{decision['supersession']}.pdf"}
    candidate["id"] = cr.digest(candidate)
    directory = root / ".component-mail" / "preview-supersession" / candidate["id"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "candidate.json"
    if path.exists() and cr.read(path) != candidate:
        raise ValueError("immutable candidate path already contains different content")
    cr.write(path, candidate)
    (directory / "preview.html").write_text(candidate["html"], encoding="utf-8")
    (directory / "preview.txt").write_text(candidate["text"], encoding="utf-8")
    (directory / candidate["pdf_filename"]).write_bytes(base64.b64decode(candidate["pdf_base64"]))
    return path


def recheck(root, path, approved_candidate, *, now=None, committed=True, prior=None):
    candidate = cr.read(path)
    if (not approved_candidate or candidate.get("id") != approved_candidate
            or cr.digest({k: v for k, v in candidate.items() if k != "id"}) != approved_candidate):
        raise ValueError("candidate differs from the exact approved payload")
    d = candidate["decision"]
    if d.get("delivery_mode") != ACTION:
        raise ValueError("wrong delivery mode")
    decision, release = plan(root, expected_preview=d["expected_preview"],
        authority=d["authority"], supersession=d["supersession"], allowed_holds=d["allowed_holds"],
        expected_previous=d["expected_previous"],
        followup_policy=d["followup_policy"], to=d["recipients"], now=now,
        committed=committed, prior=prior, pending_id=approved_candidate)
    if cr.digest(decision) != cr.digest(d):
        raise ValueError("reviewed difference, recipients, policy or original receipt changed")
    if release["identity"] != candidate["release_identity"] or release["book"] != candidate["book"]:
        raise ValueError("sealed instruction changed after approval")
    return candidate


def reserve(root, path, approved_candidate, **kwargs):
    candidate = recheck(root, path, approved_candidate, **kwargs)
    d = candidate["decision"]
    ledger = sender.ledger_at(root)
    state = ledger["anchors"][d["anchor"]]
    if state.get("pending"):
        raise ValueError("reservation already exists; reconcile rather than retry")
    state["pending"] = {"id": approved_candidate, "action": ACTION,
        "supersession": d["supersession"],
        "reserved_at": (kwargs.get("now") or datetime.now(timezone.utc)).isoformat()}
    cr.write(root / sender.LEDGER, ledger)


def send(root, path, approved_candidate, *, now=None, committed=True, prior=None,
         transport=sender.smtp_send, env=None):
    candidate = recheck(root, path, approved_candidate, now=now, committed=committed, prior=prior)
    d = candidate["decision"]
    ledger = sender.ledger_at(root)
    state = ledger["anchors"][d["anchor"]]
    pending = state.get("pending", {})
    if pending.get("id") != approved_candidate or pending.get("action") != ACTION:
        raise ValueError("no matching durable reservation")
    if pending.get("attempted_at"):
        raise ValueError("delivery was already attempted; reconcile, never retry blindly")
    if committed:
        raw = subprocess.run(["git", "show", f"origin/main:{sender.LEDGER}"], cwd=root,
                             capture_output=True, check=True).stdout
        if json.loads(raw)["anchors"][d["anchor"]].get("pending") != pending:
            raise ValueError("reservation is not durably present on origin/main")
    env = os.environ if env is None else env
    if recipients([env.get("RECIPIENT_EMAIL", "")]) != d["recipients"]:
        raise ValueError("configured recipients differ from the approved audience")
    if committed:
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=root, capture_output=True, text=True, check=True).stdout
        if dirty.strip():
            raise ValueError("tracked tree must be clean before persisting the send-attempt lock")
    pending["attempted_at"] = (now or datetime.now(timezone.utc)).isoformat()
    cr.write(root / sender.LEDGER, ledger)
    if committed:
        # A fresh checkout sees the attempt too. A failed push stops before SMTP;
        # loss of a local process cannot turn an uncertain send into an automatic retry.
        for command in (["git", "add", "--", sender.LEDGER],
                        ["git", "commit", "-m", "Record preview supersession delivery attempt"],
                        ["git", "push", "origin", "HEAD:main"]):
            subprocess.run(command, cwd=root, check=True)
    # The pending lock deliberately survives transport uncertainty or receipt-write failure.
    transport(candidate, env)
    confirmed = (now or datetime.now(timezone.utc)).isoformat()
    state.pop("pending")
    state.setdefault("preview_supersessions", {})[d["supersession"]] = {
        "candidate": approved_candidate, "release": candidate["release_identity"],
        "subject": candidate["subject"], "confirmed_at": confirmed,
        # The reviewed payload stays private. The public receipt binds its
        # audience without publishing the recipients' addresses to the repo.
        "recipient_fingerprint": cr.digest(d["recipients"]),
        "decision": copy.deepcopy({k: v for k, v in d.items() if k != "recipients"})}
    state["core"], state["europe"] = d["core_identity"], d["europe_identity"]
    state["latest_delivery_release"] = candidate["release_identity"]
    # preview, last_confirmed_at, regular and all prior receipts remain untouched.
    state["preview_supersession"] = {"id": d["supersession"], "confirmed_at": confirmed,
        "protocol_version": 2, "d_hold": d["d_hold"],
        "followup_policy": d["followup_policy"], "core": d["core_identity"],
        "europe": d["europe_identity"]}
    cr.write(root / sender.LEDGER, ledger)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("prepare", "reserve", "send"))
    p.add_argument("--expected-preview")
    p.add_argument("--expected-previous", help="exact latest delivered release for a subsequent correction")
    p.add_argument("--authority", default="")
    p.add_argument("--supersession", default="")
    p.add_argument("--allowed-holds", default="")
    p.add_argument("--followup-policy", choices=POLICIES)
    p.add_argument("--recipient", action="append", default=[])
    p.add_argument("--candidate", type=Path)
    p.add_argument("--approved-candidate", default="")
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.operation == "prepare":
        print(prepare(root, expected_preview=args.expected_preview, expected_previous=args.expected_previous, authority=args.authority,
            supersession=args.supersession, allowed_holds=args.allowed_holds.split(",") if args.allowed_holds else [],
            followup_policy=args.followup_policy, to=args.recipient))
    else:
        if args.candidate is None:
            p.error("--candidate is required")
        {"reserve": reserve, "send": send}[args.operation](root, args.candidate, args.approved_candidate)


if __name__ == "__main__":
    main()
