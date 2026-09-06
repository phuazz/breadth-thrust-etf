"""Commentary for the weekly factsheet: why the planned moves, and the week.

Every sentence here is derived from an artefact on disk — the next fill
(live_targets.json, with the signal each sleeve ranked on and the same
signal at the previous decision), the engines' last executed rebalance,
the overlay state, the holdings price panel and the blend series. Nothing
is written from memory and nothing is estimated: where a number cannot be
derived the sentence is omitted and the omission is recorded in
``notes``, so the email never carries a figure the data did not produce.
That is the reason this is a generator and not a model: the email is an
unattended send to a distribution list, and the vault rule for such a
send is a guard, not a prose style.

Output: data/commentary.json, read by build_email_body.py and by the
dashboard (pipeline.py injects it). Absent file -> the sections are not
rendered.

Python datetime months are 1-indexed (January = 1). Weekdays come from the
date library, never from arithmetic.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import display_ticker, get_etf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Same materiality as the next-fill card: below 5bp of NAV nobody places an
# order, and a sentence about it would be noise wearing a number.
MIN_MOVE = 5e-4
TOP_N = 3


def _fmt_date(iso: str | None) -> str:
    """'Fri 4 Sept 2026' from ISO, via the date library; '—' when absent."""
    if not iso:
        return "—"
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%a %d %b %Y").replace(" 0", " ", 1)


def _pct(x: float, dp: int = 1, signed: bool = False) -> str:
    return f"{x * 100:{'+' if signed else ''}.{dp}f}%"


def _pp(x: float, dp: int = 2) -> str:
    return f"{x * 100:+.{dp}f}pp"


def _label(etf: str, labels: dict) -> str:
    return labels.get(etf, "")


# ---------------------------------------------------------------------------
# Signals: rank and phrase
# ---------------------------------------------------------------------------
def _ranks(signals: dict | None) -> dict[str, int]:
    """1 = strongest, over the names that carry a value."""
    if not signals:
        return {}
    items = [(k, v) for k, v in signals.items() if v is not None]
    items.sort(key=lambda kv: -kv[1])
    return {k: i + 1 for i, (k, _) in enumerate(items)}


def _signal_phrase(kind: str | None, now: float | None, prev: float | None,
                   rank_now: int | None, rank_prev: int | None, n: int,
                   top_k: int | None) -> str:
    """The move's driver, in the sleeve's own signal units."""
    if now is None:
        return ""
    if kind == "breadth":
        body = (f"breadth {prev:.1f}% → {now:.1f}%" if prev is not None
                else f"breadth {now:.1f}%")
    else:
        body = (f"{prev * 100:+.1f}% → {now * 100:+.1f}% against its 200-day average"
                if prev is not None
                else f"{now * 100:+.1f}% against its 200-day average")
    if rank_now is not None and rank_prev is not None and rank_now != rank_prev:
        body += f", rank {rank_prev} → {rank_now} of {n}"
    elif rank_now is not None:
        body += f", rank {rank_now} of {n}"
    if top_k and rank_now is not None and rank_prev is not None:
        if rank_prev <= top_k < rank_now:
            body += f", out of the top {top_k}"
        elif rank_now <= top_k < rank_prev:
            body += f", into the top {top_k}"
    return body


def _action(ln: dict) -> str:
    held, target = ln.get("held", 0.0), ln.get("target", 0.0)
    if held <= 0 and target > 0:
        return "BUY"
    if target <= 0 and held > 0:
        return "SELL ALL"
    return "ADD" if ln.get("delta", 0) > 0 else "TRIM"


def moves_commentary(lt: dict, labels: dict) -> dict:
    """One line per material move, ordered by |delta|, plus a summary."""
    sleeves = {s["sleeve"]: s for s in lt.get("sleeves", [])}
    lines = [ln for ln in lt.get("lines", []) if abs(ln.get("delta", 0)) >= MIN_MOVE]
    lines.sort(key=lambda x: -abs(x["delta"]))
    out, notes = [], []
    for ln in lines:
        s = sleeves.get(ln["sleeve"], {})
        sig, prev = s.get("signals") or {}, s.get("signals_prev") or {}
        kind, k = s.get("signal_kind"), s.get("top_k")
        rn, rp = _ranks(sig), _ranks(prev)
        etf = ln["etf"]
        act = _action(ln)
        sym = ln.get("traded") or display_ticker(etf)
        name = _label(etf, labels)
        who = f"{sym}" + (f" ({name})" if name else "")
        if etf in ("SHY", "IEF"):
            why = "the cash proxy takes the weight the signal floor leaves unfilled"
        else:
            why = _signal_phrase(kind, sig.get(etf), prev.get(etf),
                                 rn.get(etf), rp.get(etf), len(rn), k)
        if act == "BUY":
            head = f"{who} enters at {_pct(ln['target'])} of NAV"
        elif act == "SELL ALL":
            head = f"{who} exits from {_pct(ln['held'])} of NAV"
        else:
            head = (f"{who} {_pct(ln['held'])} → {_pct(ln['target'])} of NAV "
                    f"({_pp(ln['delta'], 1)})")
        text = head + (f": {why}." if why else ".")
        if not why and etf not in ("SHY", "IEF"):
            notes.append(f"no signal recorded for {etf} in sleeve {ln['sleeve']}; the move is stated without its driver")
        out.append({"sleeve": ln["sleeve"], "etf": etf, "traded": sym, "action": act,
                    "held": ln["held"], "target": ln["target"], "delta": ln["delta"],
                    "signal_now": sig.get(etf), "signal_prev": prev.get(etf),
                    "rank_now": rn.get(etf), "rank_prev": rp.get(etf),
                    "signal_kind": kind, "text": text})
    holds = [s for s in lt.get("sleeves", []) if s.get("status") != "READY"]
    hold_text = [f"Sleeve {s['sleeve']} is held and its book is unchanged: "
                 f"{s.get('reason') or 'its signal does not reach the last close'}."
                 for s in holds]
    n_buy = sum(1 for m in out if m["action"] == "BUY")
    n_sell = sum(1 for m in out if m["action"] == "SELL ALL")
    fills = (lt.get("next_fill") or {}).get("by_venue") or {}
    when = ", ".join(f"{v} {_fmt_date(d)}" for v, d in sorted(fills.items()) if d)
    decided = {s.get("decision_session") for s in lt.get("sleeves", []) if s.get("decision_session")}
    dec = _fmt_date(sorted(decided)[-1]) if decided else "—"
    if out:
        summary = (f"{len(out)} move{'s' if len(out) != 1 else ''} at the next fill "
                   f"({when}), ranked on the {dec} close; one-way turnover "
                   f"{lt.get('one_way_turnover', 0) * 100:.2f}% of NAV"
                   + (f"; {n_buy} new name{'s' if n_buy != 1 else ''}" if n_buy else "")
                   + (f"; {n_sell} exit{'s' if n_sell != 1 else ''}" if n_sell else "")
                   + ". Signals compared with the previous decision close.")
    else:
        summary = f"No move above 0.05pp of NAV at the next fill ({when})."
    return {"summary": summary, "moves": out, "holds": hold_text, "notes": notes,
            "decision_session": sorted(decided)[-1] if decided else None,
            "fill_by_venue": fills}


# ---------------------------------------------------------------------------
# The week: blend, sleeves, holdings, context
# ---------------------------------------------------------------------------
def _series(hp: dict, key: str) -> pd.Series | None:
    p = (hp.get("prices") or {}).get(key)
    if not p or not p.get("dates") or not p.get("prices"):
        return None
    s = pd.Series(p["prices"], index=pd.to_datetime(p["dates"])).dropna()
    return s if len(s) else None


def _price_key(etf: str, hp: dict) -> str | None:
    """Which series prices this holding: the traded symbol, the registry's
    US proxy, or the panel key — whichever the panel carries."""
    cands = []
    try:
        cands.append(display_ticker(etf))
    except Exception:  # noqa: BLE001
        pass
    try:
        px = get_etf(etf).get("yfinance_trading_proxy")
        if px:
            cands.append(px)
    except Exception:  # noqa: BLE001
        pass
    cands.append(etf)
    prices = hp.get("prices") or {}
    for c in cands:
        if c in prices:
            return c
    return None


def _window_return(s: pd.Series | None, start: str, end: str) -> float | None:
    if s is None:
        return None
    w = s.loc[start:end]
    if len(w) < 2:
        return None
    return float(w.iloc[-1]) / float(w.iloc[0]) - 1.0


def week_commentary(wtd, attribution: dict | None, holdings: list[dict],
                    hp: dict, fills_by_sleeve: dict, overlay: dict | None,
                    breadth_panel: dict | None, labels: dict) -> dict:
    """The week just ended: the blend, the sleeves, the holdings that drove
    it, and the regime context. ``wtd`` is (return, start_iso, end_iso) from
    the same window the headline tile uses."""
    notes: list[str] = []
    if not wtd:
        return {"text": "", "notes": ["no week-to-date window could be computed"]}
    ret, start, end = wtd
    spy = _window_return(_series(hp, "SPY"), start, end)
    parts = [f"Week {_fmt_date(start)} close → {_fmt_date(end)} close: the blend "
             f"returned {_pct(ret, 2, signed=True)}"
             + (f" against SPY {_pct(spy, 2, signed=True)}" if spy is not None else "")
             + "."]
    if spy is None:
        notes.append("SPY series not in the holdings panel; no benchmark stated")

    if attribution and attribution.get("rows"):
        rows = sorted(attribution["rows"], key=lambda r: -r["contrib"])
        parts.append("By sleeve: "
                     + ", ".join(f"{r['label']} {_pp(r['contrib'])} "
                                 f"({_pct(r['ret'], 2, signed=True)} at "
                                 f"{_pct(r['w'], 0)} of NAV)" for r in rows) + ".")

    # Holdings: return from the later of the window start and the sleeve's
    # own fill date (the book changed at that close), at the weight held.
    contribs = []
    n_priced = 0
    for h in holdings:
        key = _price_key(h["etf"], hp)
        s = _series(hp, key) if key else None
        fill = fills_by_sleeve.get(h["sleeve"])
        lo = max(start, fill) if fill else start
        r = _window_return(s, lo, end)
        if r is None:
            continue
        n_priced += 1
        contribs.append({"etf": h["etf"], "traded": key, "sleeve": h["sleeve"],
                         "weight": h["effective"], "ret": r,
                         "contrib": h["effective"] * r, "from": lo})
    contribs.sort(key=lambda c: -c["contrib"])
    if contribs:
        def _one(c):
            nm = _label(c["etf"], labels)
            return (f"{c['traded']}{' (' + nm + ')' if nm else ''} {_pp(c['contrib'])} "
                    f"({_pct(c['ret'], 1, signed=True)} at {_pct(c['weight'], 1)})")
        top = [c for c in contribs if c["contrib"] > 0][:TOP_N]
        bottom = [c for c in contribs if c["contrib"] < 0][-TOP_N:][::-1]
        if top:
            parts.append("Largest contributors: " + "; ".join(_one(c) for c in top) + ".")
        if bottom:
            parts.append("Largest detractors: " + "; ".join(_one(c) for c in bottom) + ".")
        starts = sorted({c["from"] for c in contribs})
        basis = (f"from the {_fmt_date(starts[0])} close" if len(starts) == 1
                 else f"from each sleeve's own fill close ({', '.join(_fmt_date(x) for x in starts)})")
        parts.append(f"Holding contributions are weight × return {basis} to the "
                     f"{_fmt_date(end)} close, {n_priced} of {len(holdings)} holdings priced "
                     f"from the one-year price panel.")
    else:
        notes.append("no holding could be priced from the one-year panel")

    ctx = []
    if overlay:
        st = overlay.get("current_state")
        since = overlay.get("current_state_since")
        b = overlay.get("current_breadth")
        line = f"Regime {st}" + (f" since {_fmt_date(since)}" if since else "")
        if b is not None:
            line += f", S&P 500 breadth {b * 100:.1f}% above the 50-day average"
            prev = _breadth_at(breadth_panel, start)
            if prev is not None:
                line += f" ({(b - prev) * 100:+.1f}pp on the week)"
        ctx.append(line + ".")
    text = " ".join(parts + ctx)
    return {"text": text, "start": start, "end": end, "blend_return": ret,
            "spy_return": spy, "holdings": contribs[:12], "notes": notes,
            "n_priced": n_priced, "n_held": len(holdings)}


def _breadth_at(panel: dict | None, iso: str) -> float | None:
    """The S&P panel's 50-day breadth on or before ``iso`` (fraction)."""
    if not panel:
        return None
    ser = panel.get("series") or {}
    dates, vals = ser.get("dates") or [], ser.get("ma_breadth") or []
    best = None
    for d, v in zip(dates, vals):
        if d <= iso and v is not None:
            best = v
    return best


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build(data_dir: Path = DATA_DIR, now_utc: datetime | None = None) -> dict:
    import build_email_body as eb   # the same helpers the email itself uses

    def load(name):
        p = data_dir / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    lt = load("live_targets.json") or {}
    overlay = load("risk_overlay.json")
    multi = load("multi_strategy.json")
    live_track = load("live_track.json")
    hp = load("holdings_prices_1y.json") or {}
    panel = load("breadth_csp1.json")
    sleeves = {}
    for key, fname in [("a", "topk_robustness.json"), ("b", "asset_class_rotation.json"),
                       ("c", "thematic_rotation.json"), ("d", "europe_rotation.json")]:
        d = load(fname)
        if d:
            sleeves[key] = d
    labels = eb._build_label_map(sleeves)

    notes: list[str] = []
    moves = moves_commentary(lt, labels) if lt else {"summary": "", "moves": [],
                                                    "holds": [], "notes": ["live_targets.json absent"]}
    notes += moves.get("notes", [])

    week = {"text": "", "notes": []}
    if multi:
        _key, dates, equity = eb._get_deployed_series(multi, overlay, live_track)
        series = pd.Series(equity, index=pd.to_datetime(dates))
        asof_iso = series.index[-1].strftime("%Y-%m-%d")
        wtd = eb._compute_wtd(series)
        st_now = eb.sleeve_nav_weights(overlay, asof_iso)
        eem = _series(hp, "EEM")
        att = eb._weekly_attribution(sleeves, st_now, wtd, eem)
        holdings = eb._collect_holdings(sleeves, overlay, asof_iso)
        fills = {}
        for key, sl in (("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")):
            h = (sleeves.get(key) or {}).get("headline") or {}
            rec = h.get("latest_rebalance") or ((h.get("trade_history") or [None])[-1])
            if rec and rec.get("date"):
                fills[sl] = rec["date"]
        week = week_commentary(wtd, att, holdings, hp, fills, overlay, panel, labels)
        week["as_of"] = asof_iso
    else:
        week["notes"].append("multi_strategy.json absent; no week review")
    notes += week.get("notes", [])

    return {
        "computed_at_utc": (now_utc or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "as_of": lt.get("as_of"),
        "next_fill": moves,
        "week": week,
        "notes": notes,
        "basis": ("Signals from live_targets.json at the decision close, compared with "
                  "the previous decision close; week figures from the deployed blend "
                  "series and the one-year holdings price panel."),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DATA_DIR / "commentary.json"))
    args = ap.parse_args(argv)
    blob = build()
    Path(args.out).write_text(json.dumps(blob, indent=2), encoding="utf-8")
    nf = blob["next_fill"]
    print(f"Wrote {args.out}")
    print(f"  {nf.get('summary')}")
    for m in nf.get("moves", []):
        print(f"    - {m['text']}")
    for h in nf.get("holds", []):
        print(f"    - {h}")
    if blob["week"].get("text"):
        print(f"  {blob['week']['text']}")
    for n in blob.get("notes", []):
        print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
