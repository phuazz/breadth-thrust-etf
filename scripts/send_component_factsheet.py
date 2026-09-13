"""Two-stage factsheet sender with a durable pre-send reservation.

The plan command defaults to a no-send rehearsal. --reserve requires the
workflow to commit and push the reservation BEFORE send. Any interrupted,
partial or uncertain send then requires operator reconciliation, not a retry.
Python datetime months are 1-indexed; exchange calendars define the anchor.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from html import escape
import os
from pathlib import Path
import smtplib
import ssl
import subprocess

from component_publication import Snapshot, email_decision, email_wording
from component_release import ROOT, MANIFEST, read, write, digest, verify
from nyse_sessions import week_final_anchor

LEDGER = "docs/component_delivery.json"
OUT = ".component-mail"
SEND_ACTIONS = {"preview", "regular", "d_update"}


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
    """Decision-aid email: status, explicit changes, then historical performance."""
    wording = email_wording(decision)
    book = release["book"]
    e = escape
    anchor = e(release["anchor"])
    parts = [f"<h1>{e(wording['heading'])}</h1>", f"<p>Decision close: {anchor}. Proposed positions, not executed trades.</p>",
             f"<p>{e(wording['summary'])}</p><p>{e(wording['difference'])}</p>",
             f"<p><strong>Strategy D:</strong> {e(wording['d_instruction'])}</p>"]
    if release["performance"]["series"].startswith("synthetic"):
        parts.insert(0, "<p><strong>SYNTHETIC NO-SEND REHEARSAL — not a live instruction.</strong></p>")
    overlays = book["overlay_decision"]
    parts.append(f"<p>EM tilt: {'ON' if overlays['tilt_on'] else 'OFF'}. "
                 f"Breadth gate: {'RISK OFF' if overlays['gate_on'] else 'RISK ON'}. "
                 f"Both inputs verified to {anchor}.</p>")
    for s in book["sleeves"]:
        # datetime supplies the weekday; no manually inferred calendar labels.
        fill = datetime.fromisoformat(s["fill_date"]).strftime("%a %d %b %Y")
        parts.append(f"<p>Strategy {e(s['sleeve'])}: {e(s['status'])}. "
                     f"{e(s['venue'])} closing-auction fill: {e(fill)}.</p>")
    parts.append("<p>Confirm broker submission times in the dashboard’s Execution Timing tab. "
                 "An email review checkpoint is not an order cutoff.</p>"
                 + ("<h2>Complete proposed book</h2>" if include_unchanged else "<h2>Proposed position changes</h2>") +
                 "<p>Held and target figures are percentages of total NAV. Change is in percentage points. "
                 "The held baseline is the model portfolio, not confirmation of broker holdings.</p>")
    count = 0
    for r in book["lines"]:
        if abs(r["delta"]) <= 1e-8 and not include_unchanged:
            continue
        count += 1
        name = release["labels"].get(r["etf"], r["etf"])
        note = " — portfolio-risk adjustment only; selection unchanged" if r.get("risk_adjustment") else ""
        parts.append(f"<section class='position'><h3>{e(name)} ({e(r['traded'])})</h3>"
            f"<p>Strategy {e(r['sleeve'])}{e(note)}</p>"
            f"<p>Held {r['held']*100:.2f}% → Target {r['target']*100:.2f}% "
            f"· Change {r['delta']*100:+.2f}pp</p></section>")
    if not count:
        parts.append("<p>No position changes.</p>")
    if decision["d_hold"]:
        parts.append("<p>D remains on HOLD for selection. No Thursday-close substitute or new D ranking is used.</p>")
    if abs(book.get("rounding_residual_nav", 0.0)) > 1e-12:
        parts.append("<p>D holdings are unchanged; small rounding differences in totals are not trades.</p>")
    stats = release["performance"]
    parts.append(f"<h2>Historical model performance</h2><p>Valuation as of {e(stats['as_of'])}. "
        f"Source: deployed model series {e(stats['series'])}. These results do not assume the proposed trades occurred.</p>")
    for label, value in stats["values"].items():
        text = "Unavailable" if value is None else (f"{value:.2f}" if label == "Sharpe" else f"{value*100:+.2f}%")
        parts.append(f"<p>{e(label)}: {e(text)}</p>")
    parts.append(f"<p>WTD: {e(stats['wtd_start'] or 'unavailable')} to {e(stats['as_of'])}. "
                 "YTD starts at the prior year-end close; 1Y is the trailing calendar year. "
                 "Sharpe and maximum drawdown cover the full deployed-model history.</p>")
    parts.append("<p>The attached HTML contains the complete proposed book, including unchanged positions; "
        "the JSON copy provides the same book for audit. "
        "Review it against actual holdings before submitting orders.</p>"
        "<p><a href='https://phuazz.github.io/breadth-thrust-etf/'>Dashboard and Execution Timing</a></p>"
        "<p>Systematic model research, not personalised investment advice. Past performance does not guarantee future results.</p>")
    css = """html{-webkit-text-size-adjust:100%;color-scheme:light}body{margin:0;background:#fff;color:#17212f;
font:16px/1.6 Arial,sans-serif;padding:20px}main{max-width:60ch;margin:auto}p{overflow-wrap:anywhere}
h1{font-size:24px;line-height:1.3}h2{font-size:20px}h3{font-size:16px;margin:0}
.position{border-top:1px solid #d5dce5;padding:12px 0}.position p{margin:4px 0}
a{color:#164cb2}@media(max-width:480px){body{padding:16px;font-size:16px}h1{font-size:22px}}
html[data-theme=dark]{color-scheme:dark}html[data-theme=dark] body{background:#111827;color:#f3f4f6}
html[data-theme=dark] a{color:#93c5fd}"""
    return "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" + \
        f"<title>{e(wording['subject'])}</title><style>{css}</style></head><body><main>" + "".join(parts) + "</main></body></html>"


def prepare(root=ROOT, now=None, reserve=False, committed=False):
    now = now or datetime.now(timezone.utc)
    decision, release = plan(root, now, committed=committed)
    if decision["action"] not in SEND_ACTIONS:
        return decision
    html = render(decision, release)
    wording = email_wording(decision)
    candidate = {"decision": decision, "release_identity": release["identity"],
        "subject": f"{wording['subject']} · {release['anchor']}", "html": html,
        "book_html": render(decision, release, include_unchanged=True), "book": release["book"]}
    candidate["id"] = digest(candidate)
    write(root / OUT / "candidate.json", candidate)
    (root / OUT / "preview.html").write_text(html, encoding="utf-8")
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
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = candidate["subject"]
    msg["Message-ID"] = f"<component-{candidate['id']}@breadth-thrust-etf.invalid>"
    wording = email_wording(candidate["decision"])
    msg.set_content("\n\n".join(wording.values()) + "\n\nThe complete proposed model book is attached.")
    msg.add_alternative(candidate["html"], subtype="html")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "send"))
    parser.add_argument("--reserve", action="store_true")
    args = parser.parse_args()
    if args.operation == "send":
        send(committed=True)
        print("SMTP accepted the factsheet for all configured recipients; delivery ledger updated.")
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
