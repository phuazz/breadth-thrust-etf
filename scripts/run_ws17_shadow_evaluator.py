"""WS17 shadow evaluator — daily SOXX panel refresh + thrust state + xyz:SMH quote log.

Countersigned protocol: reviews/2026-08-16_ws17_smh-shadow-protocol.md (Amendment 1:
micro-live US$300, 4-week venue-qualification horizon). This script LOGS and ALERTS
only — it never places an order (frozen guard). Daily at 07:30 SGT via Task Scheduler
task BreadthThrust-WS17Shadow.

What one run does, in order:
  1. git pull --rebase (shared-tree safety); failure -> MISSED row, exit 2.
  2. Refresh the SOXX panel in place (compute_breadth --etf SOXX).
  3. Freshness guard: panel end must be within 2 NYSE sessions of the last
     completed session (nyse_sessions), else MISSED-EVALUATION row + alert.
  4. Replay the filed engine (backtest.run_strategy, config bit-identical to the
     WS17 focus variant) and diff the trade list against the state file: a new
     trade -> ENTRY-DUE alert; a newly closed trade -> EXIT-DUE alert. The live
     open position is a trade whose exit_reason is "data_end" at the panel end.
  5. Fetch the xyz:SMH venue row (mark/mid/impact/oracle px, funding, OI, 24h
     volume, book depth within +/-25bp) from the public Hyperliquid API and
     append a daily quote row — the venue dataset accrues without any trade.
  6. Append rows to data/ws17_shadow_log.json (append-only), update
     data/ws17_shadow_state.json, commit and push ONLY those files plus the
     refreshed panel.

Exit codes: 0 ok · 2 connectivity/preflight stall · 1 real failure.
"""

from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import (  # noqa: E402
    DEFAULT_CONFIG,
    download_soxx_ohlc,
    load_breadth,
    run_strategy,
)
from nyse_sessions import last_completed_session, sessions_behind  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_PATH = DATA_DIR / "ws17_shadow_log.json"
STATE_PATH = DATA_DIR / "ws17_shadow_state.json"

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_COIN = "xyz:SMH"
DEPTH_BAND = 0.0025          # +/-25bp around mid for the depth measure
FRESHNESS_MAX_SESSIONS = 2   # protocol section 4

# Frozen WS17 focus config — bit-identical to run_etf_oos.py CONFIGS.
FOCUS_CONFIG = {
    **DEFAULT_CONFIG,
    "trailing_stop_k": None,
    "entry_delay_bars": 5,
    "use_trend_filter": True,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def append_rows(rows: list[dict]) -> None:
    log = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else []
    log.extend(rows)
    LOG_PATH.write_text(json.dumps(log, indent=1), encoding="utf-8")


def send_alert(subject: str, body: str) -> bool:
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = user
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return True


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                          text=True, timeout=300)


def hl_quote_row(stamp: str) -> dict:
    """One venue quote row for xyz:SMH. Failures degrade to an error note —
    the evaluation itself must not die on a venue hiccup."""
    row = {"type": "quote", "ts_utc": stamp, "coin": HL_COIN}
    try:
        meta, ctxs = requests.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs", "dex": "xyz"},
                                   timeout=20).json()
        idx = next(i for i, u in enumerate(meta["universe"]) if u["name"] == HL_COIN)
        c = ctxs[idx]
        row.update({
            "mark": float(c["markPx"]), "mid": float(c["midPx"]) if c.get("midPx") else None,
            "oracle": float(c["oraclePx"]),
            "impact_bid": float(c["impactPxs"][0]) if c.get("impactPxs") else None,
            "impact_ask": float(c["impactPxs"][1]) if c.get("impactPxs") else None,
            "funding_hourly": float(c["funding"]),
            "funding_ann_pct": round(float(c["funding"]) * 24 * 365 * 100, 2),
            "open_interest": float(c["openInterest"]),
            "day_ntl_vlm_m": round(float(c["dayNtlVlm"]) / 1e6, 3),
        })
        book = requests.post(HL_INFO_URL, json={"type": "l2Book", "coin": HL_COIN},
                             timeout=20).json()
        mid = row["mid"] or row["mark"]
        depth = {"bid_usd_25bp": 0.0, "ask_usd_25bp": 0.0}
        for side, key in ((0, "bid_usd_25bp"), (1, "ask_usd_25bp")):
            for lvl in book["levels"][side]:
                px, sz = float(lvl["px"]), float(lvl["sz"])
                if abs(px / mid - 1) <= DEPTH_BAND:
                    depth[key] += px * sz
        row.update({k: round(v, 0) for k, v in depth.items()})
    except Exception as e:  # noqa: BLE001 — recorded, not fatal
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def main() -> int:
    stamp = now_utc().isoformat()
    rows: list[dict] = []

    # 1. Shared-tree preflight.
    pull = git("pull", "--rebase", "origin", "main")
    if pull.returncode != 0:
        append_rows([{"type": "ops", "ts_utc": stamp, "event": "MISSED-EVALUATION",
                      "reason": "git pull --rebase failed", "detail": pull.stderr[-400:]}])
        send_alert("[WS17 shadow] MISSED — git preflight", pull.stderr[-1000:])
        return 2

    # 2. Panel refresh in place.
    ref = subprocess.run([sys.executable, "scripts/compute_breadth.py", "--etf", "SOXX"],
                         cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=1800)
    panel_ok = ref.returncode == 0

    # 3. Freshness guard.
    breadth, signal_records = load_breadth(etf="SOXX")
    panel_end = breadth.index[-1].date()
    expected = last_completed_session(now_utc())
    behind = sessions_behind(panel_end, expected)
    fresh = behind <= FRESHNESS_MAX_SESSIONS
    if not panel_ok or not fresh:
        rows.append({"type": "ops", "ts_utc": stamp, "event": "MISSED-EVALUATION",
                     "reason": ("panel refresh failed" if not panel_ok else
                                f"panel stale: {behind} sessions behind {expected}"),
                     "panel_end": str(panel_end)})
        rows.append(hl_quote_row(stamp))  # venue dataset still accrues
        append_rows(rows)
        send_alert("[WS17 shadow] MISSED-EVALUATION",
                   f"panel_ok={panel_ok} behind={behind} expected={expected}\n"
                   + (ref.stderr[-800:] if not panel_ok else ""))
        _commit("auto: ws17 shadow MISSED " + str(panel_end))
        return 1

    # 4. Replay the filed engine and diff against state.
    signal_dates = [s["date"] for s in signal_records]
    dl_start = (breadth.index[0] - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    dl_end = (breadth.index[-1] + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    ohlc = download_soxx_ohlc(dl_start, dl_end, etf="SOXX", yf_symbol="SOXX")
    ohlc = ohlc[~ohlc.index.duplicated(keep="first")]
    trades = run_strategy(signal_dates, ohlc, breadth, config=FOCUS_CONFIG)

    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {
        "known_entries": [], "known_exits": [], "activated": "2026-08-16"}
    alerts = []
    open_pos = None
    for t in trades:
        is_open = t.exit_reason == "data_end"
        if t.entry_date not in state["known_entries"]:
            state["known_entries"].append(t.entry_date)
            rows.append({"type": "signal", "ts_utc": stamp, "event": "ENTRY",
                         "entry_date": t.entry_date, "modelled_entry": t.entry_price,
                         "open": is_open})
            alerts.append(f"ENTRY due {t.entry_date} @ modelled {t.entry_price}")
        if not is_open and t.exit_date not in state["known_exits"]:
            state["known_exits"].append(t.exit_date)
            rows.append({"type": "signal", "ts_utc": stamp, "event": "EXIT",
                         "exit_date": t.exit_date, "modelled_exit": t.exit_price,
                         "exit_reason": t.exit_reason})
            alerts.append(f"EXIT due {t.exit_date} ({t.exit_reason}) @ modelled {t.exit_price}")
        if is_open:
            open_pos = {"entry_date": t.entry_date, "modelled_entry": t.entry_price}
    state["open_position"] = open_pos
    state["last_panel_end"] = str(panel_end)
    state["last_run_utc"] = stamp

    # 5. Venue quote row, daily regardless of signals.
    rows.append(hl_quote_row(stamp))
    if open_pos:
        rows.append({"type": "hold", "ts_utc": stamp, "entry_date": open_pos["entry_date"],
                     "panel_end": str(panel_end)})

    rows.append({"type": "ops", "ts_utc": stamp, "event": "OK",
                 "panel_end": str(panel_end), "sessions_behind": behind,
                 "n_trades_engine": len(trades)})
    append_rows(rows)
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")

    if alerts:
        sent = send_alert("[WS17 shadow] ACTION: " + "; ".join(a.split(" @")[0] for a in alerts),
                          "\n".join(alerts) + "\nProtocol window 07:30-09:30 SGT. Log fills with "
                          "scripts/ws17_log_fill.py.")
        rows_note = "alert sent" if sent else "ALERT NOT SENT (creds absent)"
        print(rows_note)

    _commit(f"auto: ws17 shadow evaluation {panel_end}")
    print(f"OK panel_end={panel_end} behind={behind} trades={len(trades)} "
          f"open={'yes' if open_pos else 'no'} alerts={len(alerts)}")
    return 0


def _commit(message: str) -> None:
    git("add", "data/breadth_soxx.json", "data/ws17_shadow_log.json",
        "data/ws17_shadow_state.json")
    c = git("commit", "-m", message)
    if c.returncode == 0:
        p = git("push", "origin", "main")
        if p.returncode != 0:
            git("pull", "--rebase", "origin", "main")
            git("push", "origin", "main")


if __name__ == "__main__":
    sys.exit(main())
