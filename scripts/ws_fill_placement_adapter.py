#!/usr/bin/env python3
"""Fill-placement diagnostic adapter (registered 2026-10-03; PREREG in
reviews/2026-10-03_fill-placement-diagnostic/).

Reads the deployed engines' published weekly target-weight vectors and NAV
(data/topk_robustness.json, asset_class_rotation.json, thematic_rotation.json
and europe_rotation.json: headline.weekly_allocation and
headline.headline_equity) and the blend overlays' event logs and NAV
(data/risk_overlay.json), derives the modelled fills, fetches and freezes the
bars, runs the guards, and writes the inputs of the engine copy in
reviews/2026-10-03_fill-placement-diagnostic/engine/ in the PCC engine's
shapes.

  derive   step 2. The fills (date, side, |dw|, NAV) from the published
           vectors and the overlay events; the reconciliation of
           weekly_allocation against trade_history (the field the factsheet's
           tables read) on every date; the reproduction of the factsheet's
           target-portfolio table on every session of a sampled month,
           through the factsheet's own function and through every rendered
           factsheet PDF of that month found under docs/.
  fetch    step 3. Unadjusted OHLC and adjusted close for every priced
           symbol from yfinance, once, into data_local/ws_fill_placement/raw/
           (ignored), with a manifest of hashes.
  freeze   step 3. Prices each fill at the fetched unadjusted close on its
           fill date, applies guard 2 (the fill date must be a session in the
           line's series) and the parity guard on each fill's seven-session
           window (PREREG amendment 8), and writes engine/results/fills.json,
           book_meta.json and bars_used.json.

A fill is every line whose target weight changes on a rebalance date, side B
when the weight rises and S when it falls, size |dw| x NAV, dated on the
engine's own fill date (kind "sleeve"). The overlay legs (TILT:EEM, GATE:SHY)
fill on the overlay's own event dates, as the factsheet's TILT and GATE rows
show them (kind "overlay_leg"), and every line held at the close before a
flip is rescaled on the flip date (kind "overlay_induced"; PREREG amendment 6).

Nothing here writes to data/, docs/ or any engine file. The parity guard reads
the engine's own price panels locally from the automation clone, Norgate-built
panels included (PREREG amendment 1); only agreement statistics (lines checked,
fills compared, worst relative difference, exclusions) are written to any file,
and no panel value is written or printed.
Dates: Python datetime, months 1-indexed; ISO date strings compare in date
order.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DATA = REPO / "data"
DOCS = REPO / "docs"
STUDY = REPO / "reviews" / "2026-10-03_fill-placement-diagnostic"
ENGINE_DIR = STUDY / "engine"
ENGINE_RESULTS = ENGINE_DIR / "results"
LOCAL = REPO / "data_local" / "ws_fill_placement"
RAW = LOCAL / "raw"

SLEEVE_FILES = {"A": "topk_robustness.json", "B": "asset_class_rotation.json",
                "C": "thematic_rotation.json", "D": "europe_rotation.json"}
OVERLAY_FILE = "risk_overlay.json"
DEPLOYED_BLEND_KEY = "blend_35_35_10_20_gated_eem_tilted"
# owner decision 2026-10-03: BTC-USD trades on a seven-day calendar, so a
# three-session window is a different object there
EXCLUDED_LINES = {("C", "BTC-USD"): "BTC-USD excluded (seven-day calendar; owner decision 2026-10-03)"}
# the published weights carry four decimals; a change is at least one unit of
# the fourth decimal, so half a unit separates a change from rounding noise
WEIGHT_TOL = 0.00005
# the deployed blend's base weights and overlay mechanics, restated here
# independently of scripts/overlay_state.py so that the reproduction against
# the factsheet's own function is a check and not a tautology
BASE_SLEEVE_WEIGHTS = {"A": 0.35, "B": 0.35, "C": 0.10, "D": 0.20}
LATEST_SESSION = "2026-10-02"          # the last completed session of the frozen vintage (a Friday)
SESSIONS_BEFORE_EARLIEST_FILL = 120
REPRODUCTION_MONTH = "2026-09"
# Yahoo exchange names -> the PCC engine's exchange codes (time-zone and market tables)
YAHOO_EXCHANGE_TO_CODE = {"PCX": "ARCA", "NYQ": "NYQ", "NGM": "NGM", "NMS": "NMS", "NCM": "NMS",
                          "BTS": "BATS", "GER": "XETR", "SHZ": "SHZ"}
CODE_TZ = {"ARCA": "America/New_York", "NYQ": "America/New_York", "NGM": "America/New_York",
           "NMS": "America/New_York", "BATS": "America/New_York", "XETR": "Europe/Berlin", "SHZ": "Asia/Shanghai"}
SESSION_OPEN = {"America/New_York": (9, 30), "Europe/Berlin": (9, 0), "Asia/Shanghai": (9, 30)}
PARITY_TOL = 0.001
PARITY_STOP_SHARE = 0.10
# PREREG amendment 8: a line whose engine panel is on another basis is excluded whole and counted
BASIS_MISMATCH_LINES = {("C", "159801.SZ"): "basis mismatch: the engine's panel holds the line in another currency (PREREG amendment 8)"}
FACTOR_CHECK_MONTH = "2026-09"   # guard 1's sampled month for the six-decimal factor comparison (the reproduction month)
WINDOW_K = 3
CONFIRMATORY_FROM = "2018-10-31"   # the deployed blend's inception, a Wednesday (PREREG amendment 5)
# the automation clone holds the engine panels of the frozen vintage (its HEAD is the vintage commit)
CLONE = Path(r"C:/dev/breadth-thrust-etf-sched")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        if compact:
            json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
        else:
            json.dump(obj, fh, indent=1, ensure_ascii=False)


def stop(msg: str):
    sys.exit("STOP: " + msg)


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Instruments
# ----------------------------------------------------------------------------
def priced_symbol(sleeve: str, line: str) -> str:
    """The Yahoo symbol a line is priced on, as the engines price it: sleeve A
    and D panel ids through their registry trading proxies, B and C tickers as
    themselves, the overlay legs as their ticker."""
    if sleeve in ("A", "D"):
        sys.path.insert(0, str(SCRIPTS))
        import etf_registry  # noqa: PLC0415  (read-only use of the registry)
        return etf_registry.get_etf(line).get("yfinance_trading_proxy") or line
    return line


def provisional_ccy(symbol: str) -> str:
    """Currency by listing suffix; confirmed against the vendor's metadata at freeze."""
    if symbol.endswith(".DE"):
        return "EUR"
    if symbol.endswith(".SZ"):
        return "CNY"
    return "USD"


def line_keys(sleeve: str, line: str, symbol: str) -> tuple[str, str]:
    """(ticker, yf) as the engine reads them. The yf key is unique per sleeve
    line so that two sleeves trading one instrument on one date stay two units,
    and it ends with the Yahoo symbol so the engine's suffix tables apply."""
    return f"{sleeve}:{line}", f"{sleeve}:{line}|{symbol}"


# ----------------------------------------------------------------------------
# Fills from the published vectors
# ----------------------------------------------------------------------------
def derive_line_fills(dates: list[str], alloc: dict[str, list[float]], sleeve: str,
                      excluded_lines: dict = EXCLUDED_LINES, tol: float = WEIGHT_TOL) -> tuple[list[dict], list[dict]]:
    """Every line whose target weight changes from one rebalance date to the
    next is a fill on the later date: side B when the weight rises, S when it
    falls; the first date is measured from zero (the sleeve's inception
    trades, which the engine's turnover also counts). Returns (fills,
    excluded), the excluded list holding the fills of excluded lines."""
    for d0, d1 in zip(dates, dates[1:]):
        if not d0 < d1:
            raise ValueError(f"rebalance dates not strictly increasing at {d0}, {d1}")
    fills, excluded = [], []
    for line, ws in alloc.items():
        if len(ws) != len(dates):
            raise ValueError(f"{sleeve}:{line} has {len(ws)} weights for {len(dates)} dates")
        prev = 0.0
        for d, w in zip(dates, ws):
            dw = w - prev
            if abs(dw) >= tol:
                rec = {"sleeve": sleeve, "line": line, "date": d, "side": "B" if dw > 0 else "S", "kind": "sleeve",
                       "dw_abs": round(abs(dw), 6), "w_before": round(prev, 6), "w_after": round(w, 6)}
                if (sleeve, line) in excluded_lines:
                    excluded.append(dict(rec, reason=excluded_lines[(sleeve, line)]))
                else:
                    fills.append(rec)
            prev = w
    fills.sort(key=lambda r: (r["date"], r["sleeve"], r["line"]))
    excluded.sort(key=lambda r: (r["date"], r["sleeve"], r["line"]))
    return fills, excluded


def state_on(events: list[dict] | None, date_iso: str, on_direction: str, initial: bool = False) -> bool:
    """The direction of the latest event dated on or before the date; an event
    dated D takes effect on D; before any event the overlay is in its initial
    state (inactive unless `initial`)."""
    active = initial
    for ev in sorted(events or [], key=lambda e: e.get("date") or ""):
        d = ev.get("date")
        if not d or d > date_iso:
            break
        active = ev.get("direction") == on_direction
    return active


# PREREG amendment 11: run_risk_overlay starts the gate from its state file, RISK_OFF at
# the deployed blend's first close; the published days_risk_off and n_switches reproduce
# only under that reading (gate_counts() asserts it), so the first logged event (RISK_ON)
# is a real flip. Before the blend exists the gate does not apply.
GATE_STARTS_RISK_OFF = True


def blend_start(overlay: dict) -> str | None:
    gv = (overlay.get("gated_variants") or {}).get(DEPLOYED_BLEND_KEY) or {}
    return (gv.get("dates") or [None])[0]


def gate_active(overlay: dict, date_iso: str) -> bool:
    start = blend_start(overlay)
    if start is not None and date_iso < start:
        return False
    return state_on(overlay.get("events"), date_iso, "RISK_OFF",
                    initial=GATE_STARTS_RISK_OFF if start is not None else False)


def gate_counts(overlay: dict) -> tuple[int, int]:
    """(days RISK_OFF over the blend's dates, state switches counting the
    inception's), the published counts' definitions."""
    dates = ((overlay.get("gated_variants") or {}).get(DEPLOYED_BLEND_KEY) or {}).get("dates") or []
    states = [gate_active(overlay, d) for d in dates]
    switches = sum(1 for a, b in zip(states, states[1:]) if a != b) + (1 if states and states[0] else 0)
    return sum(states), switches


def overlay_legs(overlay: dict, date_iso: str) -> dict:
    """Sleeve NAV multipliers and overlay legs on a date (independent restatement
    of the deployed blend's mechanics: the tilt funds EEM from sleeve B, the
    gate scales every equity leg and holds the freed fraction in SHY)."""
    p22 = overlay.get("phase22_eem_tilt") or {}
    gp = overlay.get("gate_parameters") or {}
    tilt_w = float((p22.get("parameters") or {}).get("tilt_weight", 0.10))
    d_frac = float(gp.get("derisk_fraction", 0.50))
    stale_after = p22.get("signal_as_of") if p22.get("signal_stale") else None
    tilt = bool(p22.get("enabled")) and state_on(p22.get("events"), date_iso, "EM_TILT_ON") \
        and not (stale_after and date_iso > stale_after)
    gate = gate_active(overlay, date_iso)
    scaler = (1.0 - d_frac) if gate else 1.0
    mult = {"A": BASE_SLEEVE_WEIGHTS["A"] * scaler,
            "B": (BASE_SLEEVE_WEIGHTS["B"] - tilt_w if tilt else BASE_SLEEVE_WEIGHTS["B"]) * scaler,
            "C": BASE_SLEEVE_WEIGHTS["C"] * scaler, "D": BASE_SLEEVE_WEIGHTS["D"] * scaler}
    return {"mult": mult, "tilt_on": tilt, "gate_on": gate,
            "TILT": tilt_w * scaler if tilt else 0.0, "GATE": d_frac if gate else 0.0,
            "fallback": gp.get("fallback_ticker", "SHY"), "tilt_ticker": (p22.get("parameters") or {}).get("eem_ticker", "EEM")}


def derive_overlay_fills(overlay: dict) -> list[dict]:
    """The overlay legs' fills on their own event dates: the tilt's EEM leg on a
    tilt event and the gate's SHY leg on a gate event, each the change in that
    leg's NAV weight from the previous calendar day to the event date (the
    factsheet's own convention for its TILT and GATE rows). An event that does
    not change the state is no fill."""
    p22 = overlay.get("phase22_eem_tilt") or {}
    out = []
    # PREREG amendment 11: on the blend's first close the gate is already RISK_OFF, so the
    # SHY leg starts from zero (a buy, as a sleeve's inception is); no induced fill, since
    # nothing was held before the blend existed
    start = blend_start(overlay)
    if start is not None:
        legs0 = overlay_legs(overlay, start)
        if legs0["GATE"] > 0:
            out.append({"sleeve": "GATE", "line": legs0["fallback"], "date": start, "side": "B", "kind": "overlay_leg",
                        "dw_abs": round(legs0["GATE"], 6), "w_before": 0.0, "w_after": round(legs0["GATE"], 6),
                        "event": "inception (RISK_OFF from the blend's first close)"})
    for leg, events in (("TILT", p22.get("events") or []), ("GATE", overlay.get("events") or [])):
        for ev in sorted(events, key=lambda e: e["date"]):
            d = ev["date"]
            before_day = (dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat()
            before, after = overlay_legs(overlay, before_day), overlay_legs(overlay, d)
            dw = after[leg] - before[leg]
            if abs(dw) < 1e-12:
                continue
            ticker = after["tilt_ticker"] if leg == "TILT" else after["fallback"]
            out.append({"sleeve": leg, "line": ticker, "date": d, "side": "B" if dw > 0 else "S", "kind": "overlay_leg",
                        "dw_abs": round(abs(dw), 6), "w_before": round(before[leg], 6), "w_after": round(after[leg], 6),
                        "event": ev.get("direction")})
    return out


def derive_overlay_induced_fills(pub: dict, excluded_lines: dict = EXCLUDED_LINES) -> tuple[list[dict], list[dict]]:
    """PREREG amendment 6: on every date a gate or tilt flip changes the
    overlay state, each line held at the close before the flip (the latest
    published vector dated before the flip date) is rescaled by the change in
    its sleeve's NAV multiplier: side by the sign of that change, |dw| the
    line's within-sleeve weight times the change, sized later on the blend's
    NAV. The tilt's EEM leg is rescaled by a gate flip like any held line.
    Returns (fills, excluded)."""
    ov = pub["overlay"]
    p22 = ov.get("phase22_eem_tilt") or {}
    dates = sorted({e["date"] for e in (p22.get("events") or []) + (ov.get("events") or [])})
    fills, excluded = [], []
    for d in dates:
        before_day = (dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat()
        lb, la = overlay_legs(ov, before_day), overlay_legs(ov, d)
        if lb["tilt_on"] == la["tilt_on"] and lb["gate_on"] == la["gate_on"]:
            continue                                     # the event left the state unchanged
        events = [e.get("direction") for e in (p22.get("events") or []) + (ov.get("events") or []) if e["date"] == d]
        for s, payload in pub["sleeves"].items():
            dm = la["mult"][s] - lb["mult"][s]
            if abs(dm) < 1e-12:
                continue
            h = payload["headline"]
            vd = h["weekly_allocation_dates"]
            i = max((j for j, x in enumerate(vd) if x < d), default=None)
            if i is None:
                continue
            for line, ws in h["weekly_allocation"].items():
                w = ws[i]
                if w <= 1e-6:
                    continue
                rec = {"sleeve": s, "line": line, "date": d, "side": "B" if dm > 0 else "S", "kind": "overlay_induced",
                       "dw_abs": round(w * abs(dm), 8), "w_within_held": round(w, 6),
                       "mult_before": round(lb["mult"][s], 6), "mult_after": round(la["mult"][s], 6), "event": "+".join(events)}
                if (s, line) in excluded_lines:
                    excluded.append(dict(rec, reason=excluded_lines[(s, line)]))
                else:
                    fills.append(rec)
        if lb["TILT"] > 0 and la["TILT"] > 0 and abs(la["TILT"] - lb["TILT"]) > 1e-12:
            dm = la["TILT"] - lb["TILT"]
            fills.append({"sleeve": "TILT", "line": la["tilt_ticker"], "date": d, "side": "B" if dm > 0 else "S",
                          "kind": "overlay_induced", "dw_abs": round(abs(dm), 8), "w_within_held": 1.0,
                          "mult_before": round(lb["TILT"], 6), "mult_after": round(la["TILT"], 6), "event": "+".join(events)})
    return fills, excluded


def nav_lookup(dates: list[str], values: list[float], label: str):
    table = dict(zip(dates, values))

    def get(d: str) -> float:
        if d not in table:
            stop(f"{label}: no NAV on {d}")
        return float(table[d])

    return get


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True).stdout


def published_at(commit: str) -> dict:
    """The published sleeve and overlay files as committed at `commit` (the
    frozen vintage is a commit, never the working tree, which every weekly
    refresh rewrites)."""
    files = list(SLEEVE_FILES.values()) + [OVERLAY_FILE]
    raw = {f: _git("show", f"{commit}:data/{f}") for f in files}
    data = {f: json.loads(raw[f].decode("utf-8")) for f in files}
    prov = {"vintage_commit": _git("rev-parse", commit).decode().strip(),
            "files_last_changed_in": _git("log", "-1", "--format=%H %cI %s", commit, "--",
                                          *[f"data/{f}" for f in files]).decode("utf-8").strip(),
            "files": {f: {"blob_sha256": hashlib.sha256(raw[f]).hexdigest(),
                          "git_blob": _git("rev-parse", f"{commit}:data/{f}").decode().strip(),
                          "computed_at_utc": data[f].get("computed_at_utc"),
                          "price_source": data[f].get("price_source")} for f in files}}
    return {"sleeves": {s: data[f] for s, f in SLEEVE_FILES.items()}, "overlay": data[OVERLAY_FILE], "_provenance": prov}


def load_published(commit: str = "HEAD") -> dict:
    return published_at(commit)


def derive(pub: dict) -> dict:
    fills, excluded = [], []
    starts = {}
    for s, payload in pub["sleeves"].items():
        h = payload["headline"]
        f, e = derive_line_fills(h["weekly_allocation_dates"], h["weekly_allocation"], s)
        for r in e:
            r["kind"] = "sleeve"
        nav = nav_lookup(h["headline_equity_dates"], h["headline_equity"], f"sleeve {s} headline_equity")
        for r in f + e:
            r["nav"] = nav(r["date"])
            r["nav_source"] = f"{SLEEVE_FILES[s]} headline.headline_equity"
        fills += f
        excluded += e
        starts[s] = {"first_rebalance": h["weekly_allocation_dates"][0], "eligible_start": h.get("eligible_start"),
                     "rebalances": len(h["weekly_allocation_dates"])}
    ov = pub["overlay"]
    blend = ov["gated_variants"][DEPLOYED_BLEND_KEY]
    blend_nav = nav_lookup(blend["dates"], blend["equity"], f"{DEPLOYED_BLEND_KEY} equity")
    # PREREG amendment 11: the gate model must reproduce the engine's published counts
    off_days, switches = gate_counts(ov)
    if (off_days, switches) != (ov.get("days_risk_off"), ov.get("n_switches")):
        stop(f"the gate model gives {off_days} days RISK_OFF and {switches} switches against the published "
             f"{ov.get('days_risk_off')} and {ov.get('n_switches')}")
    ofills = derive_overlay_fills(ov)
    ifills, iexcl = derive_overlay_induced_fills(pub)
    for r in ofills + ifills + iexcl:
        r["nav"] = blend_nav(r["date"])
        r["nav_source"] = f"{OVERLAY_FILE} gated_variants.{DEPLOYED_BLEND_KEY}.equity"
    fills += ofills + ifills
    excluded += iexcl
    for r in fills + excluded:
        r["symbol"] = priced_symbol(r["sleeve"], r["line"])
        r["notional_nav"] = round(r["dw_abs"] * r["nav"], 8)
    fills.sort(key=lambda r: (r["date"], r["sleeve"], r["line"], r["kind"]))
    weekday = Counter(dt.date.fromisoformat(r["date"]).strftime("%a") for r in fills if r["kind"] == "sleeve")
    units = Counter((r["sleeve"], r["line"], r["date"], r["side"]) for r in fills)
    mixed = Counter()
    for r in fills:
        if units[(r["sleeve"], r["line"], r["date"], r["side"])] > 1:
            mixed[(r["sleeve"], r["line"], r["date"], r["side"])] = 1
    return {
        "fills": fills, "excluded": excluded,
        "counts": {"fills": len(fills), "excluded": len(excluded),
                   "by_sleeve": dict(sorted(Counter(r["sleeve"] for r in fills).items())),
                   "by_side": dict(Counter(r["side"] for r in fills)),
                   "by_year": dict(sorted(Counter(r["date"][:4] for r in fills).items())),
                   "by_kind": dict(sorted(Counter(r["kind"] for r in fills).items())),
                   "confirmatory_from": CONFIRMATORY_FROM,
                   "confirmatory": sum(1 for r in fills if r["date"] >= CONFIRMATORY_FROM),
                   "pre_blend": sum(1 for r in fills if r["date"] < CONFIRMATORY_FROM),
                   "pre_blend_by_sleeve": dict(sorted(Counter(r["sleeve"] for r in fills if r["date"] < CONFIRMATORY_FROM).items())),
                   "units_aggregating_a_sleeve_and_an_induced_fill": len(mixed),
                   "gate_model": {"starts_risk_off_at_blend_inception": GATE_STARTS_RISK_OFF, "blend_start": blend["dates"][0],
                                  "days_risk_off": off_days, "switches": switches, "published": [ov.get("days_risk_off"), ov.get("n_switches")]},
                   "sleeve_fill_weekdays": dict(weekday),
                   "excluded_by_reason": dict(Counter(r["reason"] for r in excluded)),
                   "lines": len({(r["sleeve"], r["line"]) for r in fills}),
                   "symbols": sorted({r["symbol"] for r in fills}),
                   "before_blend_start": dict(Counter(r["sleeve"] for r in fills if r["date"] < blend["dates"][0])),
                   "blend_start": blend["dates"][0]},
        "history_starts": starts,
    }


# ----------------------------------------------------------------------------
# Reconciliation and reproduction
# ----------------------------------------------------------------------------
def reconcile_trade_history(pub: dict, derived: dict) -> dict:
    """weekly_allocation against trade_history on every trade record, and the
    fill dates against the trade dates (a fill date must be a trade date, and
    every trade date after the first must carry a fill)."""
    out = {}
    fills_by = defaultdict(set)
    for r in derived["fills"] + derived["excluded"]:
        if r["sleeve"] in SLEEVE_FILES and r.get("kind", "sleeve") == "sleeve":   # induced fills fall on flip dates
            fills_by[r["sleeve"]].add(r["date"])
    for s, payload in pub["sleeves"].items():
        h = payload["headline"]
        idx = {d: i for i, d in enumerate(h["weekly_allocation_dates"])}
        wa = h["weekly_allocation"]
        th = h["trade_history"]
        max_diff, off_grid = 0.0, 0
        for rec in th:
            i = idx.get(rec["date"])
            if i is None:
                off_grid += 1
                continue
            held = {x["etf"]: x["weight"] for x in rec["holdings"]}
            for k in set(held) | {k for k in wa if wa[k][i] > 1e-6}:
                max_diff = max(max_diff, abs(held.get(k, 0.0) - (wa[k][i] if k in wa else 0.0)))
        trade_dates = {rec["date"] for rec in th}
        fill_dates = fills_by[s]
        out[s] = {"trade_records": len(th), "trade_records_off_grid": off_grid, "max_abs_weight_diff": max_diff,
                  "fill_dates": len(fill_dates), "fill_dates_not_trade_dates": sorted(fill_dates - trade_dates),
                  "trade_dates_without_fill": sorted((trade_dates - fill_dates) - {th[0]["date"]} if th else set())}
    ok = all(v["trade_records_off_grid"] == 0 and v["max_abs_weight_diff"] < WEIGHT_TOL and not v["fill_dates_not_trade_dates"]
             and not v["trade_dates_without_fill"] for v in out.values())
    return {"by_sleeve": out, "reconciled": ok}


def adapter_holdings(pub: dict, asof: str) -> dict:
    """The target book on an as-of date from the weekly vectors and the
    restated overlay mechanics: {(sleeve, engine id): NAV weight}."""
    legs = overlay_legs(pub["overlay"], asof)
    out = {}
    for s, payload in pub["sleeves"].items():
        h = payload["headline"]
        dates = h["weekly_allocation_dates"]
        i = max((j for j, d in enumerate(dates) if d <= asof), default=None)
        if i is None:
            continue
        for line, ws in h["weekly_allocation"].items():
            if ws[i] > 1e-6:
                out[(s, line)] = ws[i] * legs["mult"][s]
    if legs["TILT"] > 0:
        out[("TILT", legs["tilt_ticker"])] = legs["TILT"]
    if legs["GATE"] > 0:
        out[("GATE", legs["fallback"])] = legs["GATE"]
    return out


def factsheet_holdings(pub: dict, asof: str) -> dict:
    """The factsheet's own function (build_factsheet._collect_deployed_holdings,
    read-only import) on the same vintage's files, keyed as load_all() keys them."""
    sys.path.insert(0, str(SCRIPTS))
    with contextlib.redirect_stdout(io.StringIO()):
        import build_factsheet  # noqa: PLC0415
        sleeves = {s.lower(): payload for s, payload in pub["sleeves"].items()}
        rows = build_factsheet._collect_deployed_holdings(sleeves, pub["overlay"], asof)
    return {(r["sleeve"], r["etf"]): float(r["effective"]) for r in rows}


# the signal column prints "--" (or a dash) on the overlay legs, which carry no signal
TABLE_ROW = re.compile(r"^\s*(\S+)\s+([A-D]|TILT|GATE)\s+(?:([+\-−]?[\d.]+%|—|-+)\s+)?([\d.]+)%\s+\$([\d,]+)\s*$")


def parse_rendered_table(text: str) -> dict:
    """Rows of the CURRENT TARGET PORTFOLIO table from `pdftotext -table` text:
    {(sleeve, displayed ticker): weight}, the weight read from the "$ ON $1.0M"
    column (a $1,000,000 book, so six decimals of weight)."""
    out, inside = {}, False
    for raw in text.splitlines():
        if "CURRENT TARGET PORTFOLIO" in raw:
            inside = True
            continue
        if inside and "ASSET CLASS EXPOSURE" in raw:
            break
        if not inside:
            continue
        m = TABLE_ROW.match(raw)
        if m:
            out[(m.group(2), m.group(1))] = int(m.group(5).replace(",", "")) / 1_000_000
    return out


def rendered_tables(month: str, pdftotext: str | None) -> list[dict]:
    """Every factsheet PDF under docs/ whose as-of date falls in the month (the
    dated copies are named by their as-of date), plus factsheet_latest.pdf when
    its as-of date (docs/factsheet_meta.json) does."""
    if not pdftotext:
        return [{"skipped": "no pdftotext executable given"}]
    pdfs = []
    for p in sorted(DOCS.glob("factsheet_20*.pdf")):
        d = p.stem.replace("factsheet_", "")
        if d.startswith(month):
            pdfs.append((d, p))
    meta = DOCS / "factsheet_meta.json"
    if meta.exists():
        m = load_json(meta)
        if (m.get("asof_iso") or "").startswith(month) and not any(d == m["asof_iso"] for d, _ in pdfs):
            pdfs.append((m["asof_iso"], DOCS / m.get("latest_pdf", "factsheet_latest.pdf")))
    out = []
    for asof, p in pdfs:
        txt = subprocess.run([pdftotext, "-table", str(p), "-"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
        out.append({"pdf": str(p.relative_to(REPO)), "asof": asof, "sha256": sha256_of(p), "rows": parse_rendered_table(txt)})
    return out


def reproduce(pub: dict, month: str, pdftotext: str | None, latest_pdf_check: bool = True) -> dict:
    sys.path.insert(0, str(SCRIPTS))
    import etf_registry  # noqa: PLC0415
    blend = pub["overlay"]["gated_variants"][DEPLOYED_BLEND_KEY]
    asofs = [d for d in blend["dates"] if d.startswith(month)]
    code = {"asof_dates": asofs, "comparisons": 0, "exact": 0, "max_abs_diff": 0.0, "mismatch_4dp": [], "key_mismatch": []}
    for a in asofs:
        mine, theirs = adapter_holdings(pub, a), factsheet_holdings(pub, a)
        if set(mine) != set(theirs):
            code["key_mismatch"].append({"asof": a, "only_adapter": sorted(map(list, set(mine) - set(theirs))),
                                         "only_factsheet": sorted(map(list, set(theirs) - set(mine)))})
        for key in set(mine) & set(theirs):
            diff = abs(mine[key] - theirs[key])
            code["comparisons"] += 1
            code["exact"] += int(diff == 0.0)
            code["max_abs_diff"] = max(code["max_abs_diff"], diff)
            if diff >= 0.5e-4:   # half a unit of the fourth decimal (a tolerance, so a value on a rounding boundary is not miscounted)
                code["mismatch_4dp"].append({"asof": a, "key": list(key), "adapter": mine[key], "factsheet": theirs[key]})
    rendered = []
    tables = rendered_tables(month, pdftotext) if latest_pdf_check else []
    for t in tables:
        if "skipped" in t:
            rendered.append(t)
            continue
        mine = {(s, etf_registry.display_ticker(line) if s in ("A", "B", "C", "D") else line): w
                for (s, line), w in adapter_holdings(pub, t["asof"]).items()}
        rows = t["rows"]
        diffs = {k: abs(mine[k] - rows[k]) for k in set(mine) & set(rows)}
        rendered.append({"pdf": t["pdf"], "asof": t["asof"], "sha256": t["sha256"], "rows": len(rows),
                         "matched": len(diffs), "only_adapter": sorted(map(list, set(mine) - set(rows))),
                         "only_pdf": sorted(map(list, set(rows) - set(mine))),
                         "max_abs_diff": max(diffs.values()) if diffs else None,
                         "agree_4dp": sum(1 for k in diffs if diffs[k] < 0.5e-4),
                         "disagree_4dp": [{"key": list(k), "adapter": round(mine[k], 6), "pdf": rows[k]}
                                          for k in diffs if diffs[k] >= 0.5e-4]})
    code_ok = not code["key_mismatch"] and not code["mismatch_4dp"] and code["comparisons"] > 0
    latest = None
    meta_path = DOCS / "factsheet_meta.json"
    if latest_pdf_check and pdftotext and meta_path.exists():
        m = load_json(meta_path)
        p = DOCS / m.get("latest_pdf", "factsheet_latest.pdf")
        latest = compare_rendered(pub, p, m["asof_iso"], pdftotext)
        latest["note"] = "factsheet_latest.pdf, rendered from the vintage these published files are"
    return {"month": month, "code_path": code, "rendered": rendered, "latest_pdf": latest, "code_path_reproduced_4dp": code_ok}


def compare_rendered(pub: dict, pdf: Path, asof: str, pdftotext: str) -> dict:
    sys.path.insert(0, str(SCRIPTS))
    import etf_registry  # noqa: PLC0415
    txt = subprocess.run([pdftotext, "-table", str(pdf), "-"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    rows = parse_rendered_table(txt)
    mine = {(s, etf_registry.display_ticker(line) if s in ("A", "B", "C", "D") else line): w
            for (s, line), w in adapter_holdings(pub, asof).items()}
    diffs = {k: abs(mine[k] - rows[k]) for k in set(mine) & set(rows)}
    return {"pdf": str(pdf.relative_to(REPO)), "asof": asof, "sha256": sha256_of(pdf), "rows": len(rows), "matched": len(diffs),
            "only_adapter": sorted(map(list, set(mine) - set(rows))), "only_pdf": sorted(map(list, set(rows) - set(mine))),
            "max_abs_diff": max(diffs.values()) if diffs else None,
            "agree_4dp": sum(1 for k in diffs if diffs[k] < 0.5e-4),
            "agree_6dp": sum(1 for k in diffs if diffs[k] < 0.5e-6)}


def vintage_check(month: str, pdftotext: str) -> list[dict]:
    """Each dated factsheet PDF of the month against the adapter's book built
    from the published files of the last commit before the PDF was written
    (located by the PDF's file time): agreement shows the PDF was rendered
    from that vintage, so a difference from the frozen vintage is a
    restatement of the published history and not a misread series."""
    out = []
    for p in sorted(DOCS.glob(f"factsheet_{month}-*.pdf")):
        asof = p.stem.replace("factsheet_", "")
        written = dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        commit = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%H", f"--until={written}", "--",
                                 *[f"data/{f}" for f in list(SLEEVE_FILES.values()) + [OVERLAY_FILE]]],
                                capture_output=True, text=True, check=True).stdout.strip()
        rec = compare_rendered(published_at(commit), p, asof, pdftotext)
        rec.update({"vintage_commit": commit, "pdf_written_local": written})
        out.append(rec)
    return out


# ----------------------------------------------------------------------------
# Bars
# ----------------------------------------------------------------------------
def fetch(derived: dict, refetch: list[str] | None = None) -> dict:
    """Fetch every priced symbol once. A symbol already on disk is not fetched
    again unless named in --refetch, and any refetch is recorded."""
    import yfinance as yf  # noqa: PLC0415
    try:   # raise vendor errors instead of returning an empty frame (yfinance 1.x)
        yf.config.debug.hide_exceptions = False
    except AttributeError:
        pass
    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else {"symbols": {}, "refetches": []}
    earliest = {}
    for r in derived["fills"]:
        earliest[r["symbol"]] = min(earliest.get(r["symbol"], r["date"]), r["date"])
    end = (dt.date.fromisoformat(LATEST_SESSION) + dt.timedelta(days=1)).isoformat()   # yfinance end is exclusive
    for sym in sorted(earliest):
        path = RAW / f"{sym}.json"
        if path.exists() and sym not in (refetch or []):
            continue
        if path.exists():
            manifest["refetches"].append({"symbol": sym, "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                          "previous_sha256": sha256_of(path)})
        # 120 sessions before the earliest fill: ask for 260 calendar days, trimmed at freeze
        start = (dt.date.fromisoformat(earliest[sym]) - dt.timedelta(days=260)).isoformat()
        t = yf.Ticker(sym)
        df = t.history(start=start, end=end, interval="1d", auto_adjust=False, back_adjust=False,
                       actions=True, repair=False)
        if df is None or df.empty or "Adj Close" not in df.columns:
            stop(f"{sym}: the vendor returned no usable bars; nothing written for it")
        meta = dict(t.history_metadata or {})
        tz = meta.get("exchangeTimezoneName")
        bars = []
        for ts, row in df.iterrows():
            bars.append({"date": ts.date().isoformat(), "ts_utc": int(ts.timestamp()),
                         **{c: (None if row[c] != row[c] else float(row[c])) for c in df.columns}})
        rec = {"symbol": sym, "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "source": "yfinance Ticker.history (Yahoo chart API)", "yfinance_version": yf.__version__,
               "request": {"start": start, "end_exclusive": end, "interval": "1d", "auto_adjust": False,
                           "back_adjust": False, "actions": True, "repair": False, "hide_exceptions": False},
               "metadata": {k: meta.get(k) for k in ("exchangeName", "fullExchangeName", "exchangeTimezoneName",
                                                     "currency", "instrumentType", "longName", "shortName")},
               "index_tz": str(df.index.tz) if len(df) else None, "bars": bars}
        if tz and rec["index_tz"] and rec["index_tz"] != tz:
            stop(f"{sym}: index time zone {rec['index_tz']} differs from metadata {tz}")
        write_json(path, rec, compact=True)
        manifest["symbols"][sym] = {"sha256": sha256_of(path), "fetched_at_utc": rec["fetched_at_utc"], "bars": len(bars),
                                    "first": bars[0]["date"] if bars else None, "last": bars[-1]["date"] if bars else None,
                                    "earliest_fill": earliest[sym]}
        write_json(manifest_path, manifest)
        print(f"  {sym:10s} {len(bars):5d} bars {manifest['symbols'][sym]['first']} .. {manifest['symbols'][sym]['last']}", flush=True)
    return manifest


class ParityReferenceUnavailable(Exception):
    pass


_PANEL_CACHE: dict = {}


def _clone_parquet(name: str):
    import pandas as pd  # noqa: PLC0415
    if name not in _PANEL_CACHE:
        path = CLONE / "data" / name
        if not path.exists():
            raise ParityReferenceUnavailable(f"no engine panel {name} in the automation clone")
        _PANEL_CACHE[name] = pd.read_parquet(path)
    return _PANEL_CACHE[name]


def engine_panel_reference(sleeve: str, line: str, symbol: str, vintage: str | None = None) -> dict[str, float]:
    """The engine's own price panel for a line (PREREG amendment 1), read
    locally and never written anywhere: sleeve A's proxies and sleeve D's Xetra
    lines from the per-proxy OHLC caches (`<proxy>_ohlc_cache.parquet`, Close,
    which backtest.download_soxx_ohlc builds adjusted), B and the gate's SHY
    from asset_class_prices_cache.parquet (run_risk_overlay prices the
    fallback leg there), C from thematic_prices_cache.parquet, all in the
    automation clone, whose HEAD is the frozen vintage's commit; the tilt's EEM
    leg from data/em_regime_context.parquet as committed at the vintage. A line
    with no panel raises ParityReferenceUnavailable and is declared unchecked."""
    import pandas as pd  # noqa: PLC0415
    if sleeve == "TILT":
        blob = _git("show", f"{vintage or 'HEAD'}:data/em_regime_context.parquet")
        df = pd.read_parquet(io.BytesIO(blob))
        ser = df[symbol]
    elif sleeve in ("A", "D"):
        ser = _clone_parquet(f"{symbol.lower()}_ohlc_cache.parquet")["Close"]
    elif sleeve in ("B", "GATE"):
        df = _clone_parquet("asset_class_prices_cache.parquet")
        if symbol not in df.columns:
            raise ParityReferenceUnavailable(f"{sleeve}:{line}: {symbol} is not a column of the sleeve-B panel")
        ser = df[symbol]
    elif sleeve == "C":
        df = _clone_parquet("thematic_prices_cache.parquet")
        if symbol not in df.columns:
            raise ParityReferenceUnavailable(f"{sleeve}:{line}: {symbol} is not a column of the sleeve-C panel")
        ser = df[symbol]
    else:
        raise ParityReferenceUnavailable(f"{sleeve}:{line}: no engine price panel is known for this line")
    ser = ser.dropna()
    return {pd.Timestamp(ix).date().isoformat(): float(v) for ix, v in ser.items()}


def clone_state() -> dict:
    """The automation clone's HEAD and cleanliness (metadata only)."""
    head = subprocess.run(["git", "-C", str(CLONE), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(CLONE), "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                           text=True).stdout.strip()
    return {"path": str(CLONE), "head": head, "tracked_changes": bool(dirty)}


def session_open_ts(date_iso: str, tz: str) -> int:
    """UTC seconds of the regular-session open on the date in the exchange's own
    time zone (Yahoo's chart convention), so the engine's tz mapping returns the
    same calendar date. Months 1-indexed (Python datetime)."""
    y, m, d = (int(x) for x in date_iso.split("-"))
    hh, mm = SESSION_OPEN[tz]
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp())


def line_bars(raw_rec: dict, earliest_fill: str, sessions_before: int = SESSIONS_BEFORE_EARLIEST_FILL) -> tuple[list[dict], int]:
    """The line's bars in the engine's extract shape, from `sessions_before`
    sessions before its earliest fill (or the first bar the vendor holds) to
    the latest session. Returns (bars, sessions available before the earliest
    fill)."""
    bars = raw_rec["bars"]
    dates = [b["date"] for b in bars]
    i0 = next((i for i, d in enumerate(dates) if d >= earliest_fill), len(dates))
    start = max(0, i0 - sessions_before)
    tz = raw_rec["metadata"]["exchangeTimezoneName"]
    out = []
    for b in bars[start:]:
        out.append({"d": session_open_ts(b["date"], tz), "o": b.get("Open"), "h": b.get("High"), "l": b.get("Low"),
                    "c": b.get("Close"), "ac": b.get("Adj Close")})
    return out, i0 - start


def engine_inputs(priced: list[dict], raw: dict) -> tuple[list[dict], dict, dict, dict]:
    """(fills rows, book meta, bars extract, per-line bar record) in the PCC
    engine's shapes. The book meta's exchange code is the vendor's exchange
    for the priced symbol, so the engine's time-zone and proxy-feed tables see
    the instrument that is filled; the time zone it implies must equal the
    vendor's."""
    rows, meta, extract, record = [], {}, {}, {}
    earliest = {}
    for r in priced:
        k = (r["sleeve"], r["line"], r["symbol"])
        earliest[k] = min(earliest.get(k, r["date"]), r["date"])
    for (s, line, sym), first in sorted(earliest.items()):
        rec = raw[sym]
        md = rec["metadata"]
        code = YAHOO_EXCHANGE_TO_CODE.get(md.get("exchangeName"))
        if code is None:
            stop(f"{sym}: unmapped exchange {md.get('exchangeName')}")
        if CODE_TZ[code] != md.get("exchangeTimezoneName"):
            stop(f"{sym}: exchange {code} implies {CODE_TZ[code]}, the vendor says {md.get('exchangeTimezoneName')}")
        t, yf_key = line_keys(s, line, sym)
        bars, before = line_bars(rec, first)
        extract[yf_key] = {"name": md.get("longName") or md.get("shortName") or sym, "history": bars}
        meta[t] = {"name": md.get("longName") or sym, "yf": yf_key, "exchange": code, "ccy": md.get("currency"),
                   "type": "ETF", "sleeve": s, "line": line, "symbol": sym}
        record[yf_key] = {"bars": len(bars), "sessions_before_earliest_fill": before, "earliest_fill": first,
                          "first_bar": rec["bars"][0]["date"], "last_bar": rec["bars"][-1]["date"]}
    for r in priced:
        t, yf_key = line_keys(r["sleeve"], r["line"], r["symbol"])
        rows.append({"d": r["date"], "a": r["side"], "t": t, "q": r["notional_nav"] / r["price"], "p": r["price"],
                     "ccy": meta[t]["ccy"], "yf": yf_key, "th": r["sleeve"], "kind": r["kind"], "fee": None, "ref": None})
    return rows, meta, extract, record


def price_fills(fills: list[dict], raw: dict) -> tuple[list[dict], list[dict]]:
    """Guard 2 and pricing: a fill whose date is not a session with a close in
    its line's fetched series is excluded and counted; every other fill is
    priced at that session's unadjusted close (the engines' modelled fill)."""
    priced, guard2, bars_by = [], [], {}
    for r in fills:
        sym = r["symbol"]
        if sym not in bars_by:
            bars_by[sym] = {b["date"]: b for b in raw[sym]["bars"]}
        b = bars_by[sym].get(r["date"])
        if b is None or b.get("Close") is None:
            guard2.append(dict(r, reason="guard 2: the fill date is not a session in the line's fetched series"))
        else:
            priced.append(dict(r, price=b["Close"]))
    return priced, guard2


def _median(xs: list[float]) -> float:
    ys = sorted(xs)
    m = len(ys) // 2
    return ys[m] if len(ys) % 2 else (ys[m - 1] + ys[m]) / 2


def window_parity(priced: list[dict], raw: dict, panel_for, tol: float = PARITY_TOL,
                  basis_mismatch: dict = BASIS_MISMATCH_LINES, k: int = WINDOW_K,
                  factor_month: str = FACTOR_CHECK_MONTH) -> tuple[list[dict], list[dict], dict]:
    """PREREG amendment 8. For every fill, the fetched window's price ratios
    (each bar's close over the fill-session close on the dividend-rebased
    basis, that is the fetched adjusted closes) against the engine panel's
    ratios over the same 2k+1 sessions: any ratio off by more than `tol`, or a
    panel bar missing in the window, excludes the fill and is counted. Lines in
    `basis_mismatch` are excluded whole and counted. The level drift (fetched
    adjusted close against the panel on each fill date) is tabulated by line
    and calendar year, and guard 1's six-decimal factor comparison is run on
    the sampled month; neither gates. `panel_for(sleeve, line, symbol)`
    returns {date: panel close} or raises ParityReferenceUnavailable. Returns
    (kept, excluded, record); the record holds agreement statistics only."""
    by_line = defaultdict(list)
    for r in priced:
        by_line[(r["sleeve"], r["line"], r["symbol"])].append(r)
    kept, excluded, lines, unchecked, basis, drift, factor = [], [], {}, [], [], {}, {}
    tot = Counter()
    for (s_, line, sym), rows in sorted(by_line.items()):
        key = f"{s_}:{line}"
        if (s_, line) in basis_mismatch:
            excluded.extend(dict(r, reason=basis_mismatch[(s_, line)]) for r in rows)
            basis.append({"line": key, "fills": len(rows)})
            lines[key] = {"symbol": sym, "basis_mismatch": True, "fills": len(rows)}
            continue
        try:
            ref = panel_for(s_, line, sym)
        except ParityReferenceUnavailable as exc:
            unchecked.append({"line": key, "reason": str(exc), "fills": len(rows)})
            lines[key] = {"symbol": sym, "checked": False, "fills": len(rows)}
            kept.extend(rows)            # declared unchecked and counted, never silently passed
            continue
        bars = raw[sym]["bars"]
        dates = [b["date"] for b in bars]
        idx = {d: i for i, d in enumerate(dates)}
        adj = [b.get("Adj Close") for b in bars]
        status = {}
        for d in sorted({r["date"] for r in rows}):
            i = idx[d]                   # present: guard 2 ran first
            if i < k or i + k >= len(dates):
                status[d] = ("edge", None)
                continue
            a_t, p_t = adj[i], ref.get(d)
            if a_t is None or p_t is None or not (a_t > 0 and p_t > 0):
                status[d] = ("missing", None)
                continue
            dev, missing = 0.0, False
            for j in range(i - k, i + k + 1):
                a_j, p_j = adj[j], ref.get(dates[j])
                if a_j is None or p_j is None or not (p_j > 0):
                    missing = True
                    break
                dev = max(dev, abs((a_j / a_t) / (p_j / p_t) - 1))
            status[d] = ("missing", None) if missing else (("over", dev) if dev > tol else ("ok", dev))
        per = Counter(st for st, _ in status.values())
        compared = [dv for st, dv in status.values() if st in ("ok", "over")]
        lines[key] = {"symbol": sym, "checked": True, "fills": len(rows), "windows": len(status),
                      "windows_ok": per["ok"], "windows_over_tol": per["over"], "windows_missing_a_panel_bar": per["missing"],
                      "windows_at_series_edge": per["edge"],
                      "worst_ratio_dev_compared": max(compared) if compared else None,
                      "worst_ratio_dev_kept": max((dv for st, dv in status.values() if st == "ok"), default=None)}
        tot.update(per)
        for r in rows:
            st, dv = status[r["date"]]
            if st == "over":
                excluded.append(dict(r, reason="parity: a within-window price ratio off by more than 0.1 per cent", ratio_dev=dv))
            elif st == "missing":
                excluded.append(dict(r, reason="parity: a panel bar missing in the window"))
            else:
                kept.append(r)           # "edge" windows are left to the engine's own window rule
        # level drift, a disclosure (never a gate)
        by_year = defaultdict(list)
        for d in status:
            a_t, p_t = adj[idx[d]], ref.get(d)
            if a_t is not None and p_t is not None and p_t > 0:
                by_year[d[:4]].append(abs(a_t / p_t - 1))
        drift[key] = {y: {"fill_dates": len(v), "worst_rel": max(v), "median_rel": _median(v)} for y, v in sorted(by_year.items())}
        # guard 1: the six-decimal adjustment factor against the engine's own on the sampled month
        diffs = []
        for b in bars:
            if b["date"].startswith(factor_month) and b.get("Close") and b.get("Adj Close") and ref.get(b["date"]):
                diffs.append(abs(b["Adj Close"] / b["Close"] - ref[b["date"]] / b["Close"]))
        factor[key] = {"sessions": len(diffs), "max_abs_diff": max(diffs) if diffs else None,
                       "agree_6dp": sum(1 for x in diffs if x < 0.5e-6)}
    n_priced = len(priced)
    record = {
        "rule": "PREREG amendment 8: each fill's seven-session window, fetched adjusted-close ratios to the fill session against the engine panel's, tolerance 0.1 per cent; a failing or incomplete window excludes the fill; basis-mismatch lines excluded whole; the level drift a disclosure",
        "tolerance_rel": tol, "lines": len(by_line), "lines_checked": sum(1 for v in lines.values() if v.get("checked")),
        "lines_unchecked": len(unchecked), "unchecked": unchecked, "basis_mismatch_excluded": basis,
        "windows": sum(v.get("windows", 0) for v in lines.values()),
        "windows_ok": tot["ok"], "windows_over_tol": tot["over"], "windows_missing_a_panel_bar": tot["missing"],
        "windows_at_series_edge": tot["edge"],
        "fills_priced": n_priced, "fills_excluded": len(excluded), "excluded_share": round(len(excluded) / n_priced, 6) if n_priced else 0.0,
        "fills_excluded_by_reason": dict(Counter(r["reason"] for r in excluded)),
        "worst_ratio_dev_kept": max((v["worst_ratio_dev_kept"] for v in lines.values() if v.get("worst_ratio_dev_kept") is not None), default=None),
        "by_line": lines,
        "level_drift_disclosure": drift,
        "factor_check": {"month": factor_month, "by_line": factor,
                         "lines_all_sessions_agree_6dp": sum(1 for v in factor.values() if v["sessions"] and v["agree_6dp"] == v["sessions"])},
    }
    return kept, excluded, record


def freeze(derived: dict) -> dict:
    """Step 3: guard 2, the amended parity guard (PREREG amendment 8) and the
    engine's inputs written to engine/results/. Stops, writing nothing, if
    more than a tenth of the priced fills are excluded."""
    manifest = load_json(RAW / "manifest.json")
    raw = {}
    for sym in sorted({r["symbol"] for r in derived["fills"]}):
        path = RAW / f"{sym}.json"
        if sha256_of(path) != manifest["symbols"][sym]["sha256"]:
            stop(f"{sym}: raw file differs from its manifest hash")
        raw[sym] = load_json(path)
    vintage = derived["_provenance"]["published"]["vintage_commit"]
    clone = clone_state()
    if clone["head"] != vintage:
        stop(f"the automation clone's HEAD {clone['head'][:8]} is not the frozen vintage {vintage[:8]}")
    priced, guard2 = price_fills(derived["fills"], raw)
    kept, excluded, parity = window_parity(priced, raw, lambda s_, l_, y_: engine_panel_reference(s_, l_, y_, vintage))
    parity.update({"reference": "the engine's own price panels, read locally from the automation clone at the frozen vintage "
                                "(Norgate-built panels included), the tilt's EEM leg from its committed panel (PREREG amendment 1); "
                                "agreement statistics only",
                   "clone_head": clone["head"], "clone_tracked_changes": clone["tracked_changes"]})
    if parity["excluded_share"] > PARITY_STOP_SHARE:
        write_json(LOCAL / "freeze_parity_stop.json", {"parity": parity, "guard2_excluded": len(guard2)})
        stop(f"the parity guard excludes {parity['fills_excluded']} of {parity['fills_priced']} priced fills "
             f"({parity['excluded_share']:.1%}), more than a tenth; nothing written to engine/results")
    rows, meta, extract, record = engine_inputs(kept, raw)
    fetched_at = sorted(rec["fetched_at_utc"] for rec in raw.values())
    first = next(iter(raw.values()))
    extract = {"_provenance": {"source": "yfinance Ticker.history (Yahoo chart API), unadjusted OHLC and adjusted close",
                               "yfinance_version": first["yfinance_version"],
                               "request": {k: v for k, v in first["request"].items() if k not in ("start", "end_exclusive")}
                                          | {"start": "260 calendar days before each symbol's earliest fill", "end_exclusive": first["request"]["end_exclusive"]},
                               "fetched_at_utc": {"first": fetched_at[0], "last": fetched_at[-1]},
                               "raw_files": "data_local/ws_fill_placement/raw/ (ignored), one file per symbol",
                               "raw_sha256": {sym: manifest["symbols"][sym]["sha256"] for sym in sorted(raw)},
                               "sessions_before_earliest_fill_target": SESSIONS_BEFORE_EARLIEST_FILL,
                               "lines": record,
                               "bar_timestamp": "the regular-session open in the exchange's own time zone, in UTC seconds"},
               **extract}
    rec_ok = bool(derived["reconciliation"]["reconciled"] and derived["reproduction"]["code_path_reproduced_4dp"])
    latest = derived["reproduction"].get("latest_pdf") or {}
    def brief(r):
        return {"line": f"{r['sleeve']}:{r['line']}", "date": r["date"], "side": r["side"], "kind": r["kind"], "reason": r["reason"]}
    book = {"meta": meta, "_provenance": {
        "adapter": "scripts/ws_fill_placement_adapter.py", "adapter_sha256": sha256_of(Path(__file__)),
        "vintage": derived["_provenance"]["published"],
        "derive_counts": {k: v for k, v in derived["counts"].items() if k != "symbols"},
        "excluded_btc_usd": {"fills": len(derived["excluded"]), "by_kind": dict(Counter(r["kind"] for r in derived["excluded"]))},
        "guard2_excluded": [brief(r) for r in guard2],
        "parity": parity,
        "parity_excluded": [brief(r) for r in excluded],
        "reproduction": {"reconciled": rec_ok,
                         "trade_history_reconciled": derived["reconciliation"]["reconciled"],
                         "code_path": {k: v for k, v in derived["reproduction"]["code_path"].items() if k in ("comparisons", "exact", "max_abs_diff")}
                                      | {"sessions": len(derived["reproduction"]["code_path"]["asof_dates"]), "month": derived["reproduction"]["month"]},
                         "latest_pdf": {k: latest.get(k) for k in ("pdf", "asof", "rows", "matched", "agree_4dp", "agree_6dp", "sha256")}},
        "rows_written": len(rows), "by_kind_written": dict(sorted(Counter(r["kind"] for r in kept).items())),
        "confirmatory_from": CONFIRMATORY_FROM,
        "confirmatory_written": sum(1 for r in kept if r["date"] >= CONFIRMATORY_FROM),
        "pre_blend_written": sum(1 for r in kept if r["date"] < CONFIRMATORY_FROM),
    }}
    ENGINE_RESULTS.mkdir(parents=True, exist_ok=True)
    write_json(ENGINE_RESULTS / "fills.json", rows, compact=True)
    write_json(ENGINE_RESULTS / "book_meta.json", book)
    write_json(ENGINE_RESULTS / "bars_used.json", extract, compact=True)
    out = {"rows_written": len(rows), "guard2_excluded": len(guard2),
           "parity": {k: v for k, v in parity.items() if k not in ("by_line", "level_drift_disclosure", "factor_check")}
                     | {"factor_check_lines_all_sessions_agree_6dp": parity["factor_check"]["lines_all_sessions_agree_6dp"]},
           "confirmatory_written": book["_provenance"]["confirmatory_written"], "pre_blend_written": book["_provenance"]["pre_blend_written"],
           "by_kind_written": book["_provenance"]["by_kind_written"],
           "sha256": {f: sha256_of(ENGINE_RESULTS / f) for f in ("fills.json", "book_meta.json", "bars_used.json")}}
    print(json.dumps(out, indent=1, default=str))
    return out


def parity_within_window(derived: dict) -> dict:
    """Diagnostic, not the registered guard: for every fill, the seven-session
    window's adjusted closes divided by the fill session's (the rebased prices
    u, the pre leg and the post leg are computed from) in the fetched series
    against the same ratios in the engine's own panel; the worst relative
    difference per window. Statistics only; no panel value is written."""
    import numpy as np  # noqa: PLC0415
    vintage = derived["_provenance"]["published"]["vintage_commit"]
    by_line = defaultdict(set)
    for r in derived["fills"]:
        by_line[(r["sleeve"], r["line"], r["symbol"])].add(r["date"])
    raw, lines = {}, {}
    for (s_, line, sym), dates in sorted(by_line.items()):
        if sym not in raw:
            raw[sym] = load_json(RAW / f"{sym}.json")
        bars = raw[sym]["bars"]
        bd = [b["date"] for b in bars]
        idx = {x: i for i, x in enumerate(bd)}
        a = np.array([b["Adj Close"] if b.get("Adj Close") is not None else np.nan for b in bars])
        try:
            ref = engine_panel_reference(s_, line, sym, vintage)
        except ParityReferenceUnavailable as exc:
            lines[f"{s_}:{line}"] = {"checked": False, "reason": str(exc)}
            continue
        rr_all = np.array([ref.get(x, np.nan) for x in bd])
        devs, missing = [], 0
        for t in sorted(dates):
            i = idx.get(t)
            if i is None or i < 3 or i + 3 >= len(bd):
                continue
            sl = slice(i - 3, i + 4)
            ra, rr = a[sl] / a[i], rr_all[sl] / rr_all[i]
            if np.isnan(ra).any() or np.isnan(rr).any():
                missing += 1
                continue
            devs.append(float(np.max(np.abs(ra / rr - 1))))
        devs = np.array(devs)
        lines[f"{s_}:{line}"] = {"checked": True, "windows": int(len(devs)), "windows_missing_a_bar": missing,
                                 "worst": float(devs.max()) if len(devs) else None,
                                 "windows_over_tol": int((devs > PARITY_TOL).sum())}
    chk = [v for v in lines.values() if v.get("checked")]
    return {"tolerance_rel": PARITY_TOL, "windows": sum(v["windows"] for v in chk),
            "windows_over_tol": sum(v["windows_over_tol"] for v in chk),
            "lines_with_a_window_over_tol": sorted(k for k, v in lines.items() if v.get("windows_over_tol")),
            "by_line": lines}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive")
    d.add_argument("--pdftotext", default=None, help="path to an xpdf/poppler pdftotext executable for the rendered-table check")
    d.add_argument("--month", default=REPRODUCTION_MONTH)
    d.add_argument("--vintage", default="HEAD", help="the commit whose published files are the frozen vintage")
    f = sub.add_parser("fetch")
    f.add_argument("--refetch", nargs="*", default=None)
    sub.add_parser("freeze")
    sub.add_parser("parity-diagnostic")
    args = ap.parse_args()
    LOCAL.mkdir(parents=True, exist_ok=True)
    if args.cmd == "derive":
        pub = load_published(args.vintage)
        derived = derive(pub)
        derived["reconciliation"] = reconcile_trade_history(pub, derived)
        repro = reproduce(pub, args.month, args.pdftotext)
        if args.pdftotext:
            repro["rendered_at_own_vintage"] = vintage_check(args.month, args.pdftotext)
        derived["reproduction"] = repro
        derived["_provenance"] = {"written_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                  "adapter_sha256": sha256_of(Path(__file__)), "git_head": git_head(),
                                  "published": pub["_provenance"]}
        write_json(LOCAL / "derived_fills.json", derived)
        brief = lambda r: {k: r.get(k) for k in ("pdf", "asof", "rows", "matched", "agree_4dp", "agree_6dp", "max_abs_diff",
                                                  "only_adapter", "only_pdf", "vintage_commit", "skipped") if k in r}
        print(json.dumps({"counts": {k: v for k, v in derived["counts"].items() if k != "symbols"},
                          "history_starts": derived["history_starts"],
                          "reconciled": derived["reconciliation"]["reconciled"],
                          "code_path_reproduced_4dp": repro["code_path_reproduced_4dp"],
                          "code_path": {k: (len(v) if isinstance(v, list) else v) for k, v in repro["code_path"].items()},
                          "latest_pdf": brief(repro["latest_pdf"]) if repro.get("latest_pdf") else None,
                          "rendered_at_frozen_vintage": [brief(r) for r in repro["rendered"]],
                          "rendered_at_own_vintage": [brief(r) for r in repro.get("rendered_at_own_vintage", [])]},
                         indent=1, default=str))
        return
    derived = load_json(LOCAL / "derived_fills.json")
    if args.cmd == "fetch":
        fetch(derived, args.refetch)
        return
    if args.cmd == "freeze":
        freeze(derived)
    if args.cmd == "parity-diagnostic":
        out = parity_within_window(derived)
        write_json(LOCAL / "parity_within_window.json", out)
        print(json.dumps({k: v for k, v in out.items() if k != "by_line"}, indent=1))


if __name__ == "__main__":
    main()
