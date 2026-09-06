"""Build the HTML body for the weekly factsheet email.

Reads the same data sources as build_factsheet.py and emits a compact
HTML summary that gives the recipient enough context to read the PDF
without opening it — current regime state, EEM tilt state, top
holdings, this week's rebalance activity, headline stats.

The output is written to a path supplied via --out (default
docs/email_body.html) so the GitHub Actions workflow can read it back
into the dawidd6/action-send-mail step as html_body.

Personal research artefact — disclaimer is appended automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Allow importing sibling scripts/ modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import display_ticker  # noqa: E402
from regime_publish import regime_publish_status  # noqa: E402
from overlay_state import (  # noqa: E402
    derisk_fraction as _gate_derisk_fraction, sleeve_nav_weights,
    tilt_signal_as_of, tilt_signal_stale, tilt_stale_on)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

# Monospace stack for tickers and numeric columns. Bare "Courier" (the
# previous choice) renders in Gmail as a thin, low-contrast serif-y face
# that reads poorly; this stack lets each client pick a crisp modern
# monospace — SF Mono / Menlo on Apple Mail and iOS, Consolas on Outlook
# and Windows Gmail — and only ever falls back to generic monospace, never
# to Courier. Inline font-family on table cells survives Gmail's CSS
# sanitiser, so the choice takes effect in the rendered email.
MONO = ("ui-monospace,SFMono-Regular,'SF Mono',Consolas,"
        "'Liberation Mono',Menlo,monospace")

DEPLOYED_KEY_PREFERENCE = [
    "blend_35_35_10_20_gated_eem_tilted",
    "blend_35_35_10_20_gated",
    "blend_35_35_10_20",
]

# Plain-English fund names below the ticker — mirrors the dashboard's
# positions-preview card formatting. Strategy A is hand-maintained
# (its JSON does not expose inline labels); B / C / D labels are
# enriched from the sleeve JSON universe arrays at runtime.
STRATEGY_A_LABELS = {
    "SOXX": "iShares Semiconductors",
    "CSP1": "iShares Core S&P 500",
    "CNDX": "iShares NASDAQ-100",
    "IUES": "iShares S&P 500 Energy",
    "IUFS": "iShares S&P 500 Financials",
    "IUHC": "iShares S&P 500 Health Care",
    "IUIS": "iShares S&P 500 Industrials",
    "IUCS": "iShares S&P 500 Consumer Staples",
    "IUCD": "iShares S&P 500 Consumer Discretionary",
    "IUUS": "iShares S&P 500 Utilities",
    "IUMS": "iShares S&P 500 Materials",
    "IUCM": "iShares S&P 500 Communication Services",
    "IUSP": "iShares US Property Yield (REITs)",
    "IDP6": "iShares S&P SmallCap 600",
}
TILT_LABELS = {"EEM": "iShares MSCI Emerging Markets"}


def _build_label_map(sleeves: dict) -> dict[str, str]:
    """Return ETF -> plain-name lookup, combining Strategy A's hand-
    maintained labels with B/C/D labels read from each sleeve JSON's
    universe array."""
    m = {**STRATEGY_A_LABELS, **TILT_LABELS}
    for key in ("b", "c", "d"):
        for u in (sleeves.get(key, {}).get("universe") or []):
            etf, label = u.get("etf"), u.get("label")
            if etf and label and etf not in m:
                m[etf] = label
    return m


def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _get_deployed_series(multi, overlay, live_track=None):
    """Return (deployed_key, dates, equity) for the live blend.

    When ``live_track`` is provided and its anchor matches the
    resolved series' last date, intra-week NAV points are spliced on
    so the email reflects the latest daily close (not just Friday)."""
    key = None
    dates = equity = None
    if overlay and "gated_variants" in overlay:
        for k in DEPLOYED_KEY_PREFERENCE:
            if k in overlay["gated_variants"]:
                key = k
                s = overlay["gated_variants"][k]
                dates, equity = list(s["dates"]), list(s["equity"])
                break
    if key is None:
        for k in DEPLOYED_KEY_PREFERENCE:
            if k in multi["strategies"]:
                key = k
                s = multi["strategies"][k]
                dates, equity = list(s["dates"]), list(s["equity"])
                break
    if key is None:
        key = next(iter(multi["strategies"]))
        s = multi["strategies"][key]
        dates, equity = list(s["dates"]), list(s["equity"])

    # Splice live-track extension when anchor matches deployed key.
    if (live_track and live_track.get("deployed_key") == key
            and live_track.get("live_dates")
            and dates and dates[-1] == live_track.get("anchor_date")):
        dates = dates + list(live_track["live_dates"])
        equity = equity + list(live_track["live_equity"])
    return key, dates, equity


def _one_year_return(series: pd.Series):
    """Trailing 1-year total return, DATE-anchored: last close over the
    first close at/after (as-of minus one calendar year). Identical to
    the factsheet's "1 year" row (window_ret with a DateOffset anchor).

    The previous 252-TRADING-BAR window was mislabelled on this blend's
    US-intersect-Europe calendar (~246.5 bars/year): 252 bars spanned
    374 calendar days, so the 2026-07-18 email printed 1Y +32.2% while
    the PDF attached to it printed +30.8% for the same portfolio.
    pandas DateOffset handles month/year boundaries and leap days."""
    if len(series) < 2:
        return None
    start = series.index[-1] - pd.DateOffset(years=1)
    s = series.loc[start:]
    if len(s) < 2:
        return None
    return s.iloc[-1] / s.iloc[0] - 1.0


def _ytd_return(series: pd.Series):
    asof = series.index[-1]
    year_start = pd.Timestamp(year=asof.year, month=1, day=1)
    in_year = series[series.index >= year_start]
    if len(in_year) < 2:
        return None
    # Anchor to last trading day of prior year if available
    prior = series[series.index < year_start]
    base = prior.iloc[-1] if len(prior) else in_year.iloc[0]
    return in_year.iloc[-1] / base - 1.0


def _sharpe_full(series: pd.Series):
    rets = series.pct_change().dropna()
    if len(rets) < 30 or rets.std() == 0:
        return None
    return float((rets.mean() / rets.std()) * np.sqrt(252))


def _max_drawdown(series: pd.Series):
    if len(series) < 2:
        return None
    peak = series.cummax()
    dd = series / peak - 1.0
    return float(dd.min())


def _load_spy():
    """SPY closes from the asset-class price cache — the SAME source the
    factsheet PDF reads, so the email body and the attached PDF quote one
    benchmark rather than two. Returns None if unavailable; every caller
    degrades to bare strategy figures."""
    p = DATA_DIR / "asset_class_prices_cache.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:
        return None
    if "SPY" not in df.columns:
        return None
    s = df["SPY"].dropna()
    if not len(s):
        return None
    s.index = pd.to_datetime(s.index)
    return s


def _spy_metrics(spy, series, wtd):
    """Benchmark figures over EXACTLY the strategy's own windows.

    SPY is reindexed onto the strategy's trading calendar so Sharpe and max
    drawdown span the identical history (otherwise SPY's 2007-start cache
    would be compared against a 2018-inception strategy), and week-to-date
    reuses the strategy's own (from, to) dates rather than recomputing a
    week — the two must be the same window or the 'vs SPY' figure is a
    comparison of two different periods."""
    if spy is None or series is None or not len(series):
        return {}
    al = spy.reindex(series.index, method="ffill").dropna()
    if len(al) < 30:
        return {}
    m = {
        "ytd": _ytd_return(al),
        "r1y": _one_year_return(al),
        "sharpe": _sharpe_full(al),
        "mdd": _max_drawdown(al),
        "wtd": None,
    }
    if wtd:
        try:
            base = al.loc[:pd.Timestamp(wtd[1])].iloc[-1]
            last = al.loc[:pd.Timestamp(wtd[2])].iloc[-1]
            m["wtd"] = float(last / base - 1.0)
        except (KeyError, IndexError):
            m["wtd"] = None
    return m


def _fmt_pct(x, signed=True, dp=2):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return "n/a"
    fmt = f"{{:+.{dp}f}}%" if signed else f"{{:.{dp}f}}%"
    return fmt.format(x * 100)


def _regime_state(overlay):
    if not overlay:
        return ("UNKNOWN", "&mdash;")
    state = overlay.get("current_state", "UNKNOWN")
    since = overlay.get("current_state_since", "&mdash;")
    return (state, since)


def _eem_tilt_state(overlay, asof_iso):
    """(state, since, ratio) for the tilt card.

    Freshness-aware (2026-07-29): when the EEM/SPY feed has stalled the
    blend runs untilted, so the card must not print EM_TILT_ON off the
    last-valid ``current_state``. Mirrors the Phase 28.5 breadth-panel
    freshness guard below, for the other overlay leg. ``ratio`` is dropped
    on a stale feed because ``current_ratio`` is then a frozen reading.
    """
    if not overlay or "phase22_eem_tilt" not in overlay:
        return ("DISABLED", "&mdash;", None)
    p22 = overlay["phase22_eem_tilt"]
    state = p22.get("current_state", "UNKNOWN")
    since = p22.get("current_state_since", "&mdash;")
    ratio = p22.get("current_ratio")
    # Fail safe, not confident: with no as-of date we cannot prove the feed
    # is fresh, so a flagged overlay reads OFF rather than defaulting to ON.
    if tilt_signal_stale(overlay) and (
            not asof_iso or tilt_stale_on(overlay, asof_iso)):
        return ("EM_TILT_OFF",
                f"feed stale since {tilt_signal_as_of(overlay)}", None)
    return (state, since, ratio)


def _collect_holdings(sleeves, overlay, asof_iso):
    """Mirror build_factsheet's holdings table — return the effective-NAV
    holdings list, BOTH overlays applied via ``overlay_state``: the EEM
    tilt (B 35% -> 25% plus a TILT row) and the Phase 19 de-risk gate
    (equity legs scaled by 1 - derisk_fraction plus a GATE row in the
    fallback ticker). Before 2026-07-18 the gate was ignored here, so a
    RISK_OFF email would have shown the full-equity book at twice the
    live target."""
    st = sleeve_nav_weights(overlay, asof_iso)
    letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    holdings = []
    for key, sl in letter.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if not trades:
            continue
        for h in trades[-1].get("holdings", []):
            eff = h.get("weight", 0) * st[key]
            holdings.append({
                "etf": h.get("etf"),
                "sleeve": sl,
                "effective": eff,
            })
    if st["tilt_nav"] > 0:
        holdings.append({"etf": "EEM", "sleeve": "TILT",
                         "effective": st["tilt_nav"]})
    if st["shy_overlay"] > 0:
        fb = ((overlay or {}).get("gate_parameters") or {}).get(
            "fallback_ticker", "SHY")
        holdings.append({"etf": fb, "sleeve": "GATE",
                         "effective": st["shy_overlay"]})
    return sorted(holdings, key=lambda x: -x["effective"])


def _latest_rebalance_iso(sleeves) -> str:
    """The newest rebalance RUN across the four sleeves, from
    ``headline.latest_rebalance`` (emitted since 2026-09-03), falling back to
    the last trade for payloads built before the field existed. The trade
    record is the change log: on a week every sleeve held, its newest date is
    a week or more older than the rebalance that held the book, and this
    email's card used to print that as "rebalanced <date>"."""
    best = ""
    for key in ("a", "b", "c", "d"):
        h = (sleeves.get(key, {}) or {}).get("headline") or {}
        rec = h.get("latest_rebalance")
        if not rec:
            th = h.get("trade_history") or []
            rec = th[-1] if th else None
        d = (rec or {}).get("date") or ""
        if d > best:
            best = d
    return best


def _collect_activity(sleeves, overlay):
    """Identify ENTER/EXIT/RESIZE moves from prior rebalance to current,
    across all four sleeves, plus the overlays' own trades.

    Returns one dict per move: ``{sleeve, action, etf, prev, new, date,
    nav_impact}``. Weights are PORTFOLIO-level % NAV (not within-sleeve),
    so ``nav_impact`` (the |ΔNAV| a move represents) means the same thing
    in every sleeve — a 1% within-sleeve move is 0.35% NAV in A but only
    0.10% NAV in C. ``date`` is the sleeve's own rebalance Friday (they
    differ — only Europe/D trades on a US-holiday Friday).

    2026-07-18 — each column is priced with the sleeve weights that
    applied ON ITS OWN REBALANCE DATE (``overlay_state``), not the
    current state's. The old current-state shortcut misstated every B
    prior weight on a tilt-flip week (up to 5.7pp NAV on the 2025-04-11
    pair) and omitted the flip's own trades; a tilt or gate event in the
    current week now emits its own TILT / GATE row (EEM in or out at 10%
    NAV; the gate's shift into the fallback ticker), dated with the
    event's true date.

    Every non-zero move is returned (no threshold here); the caller
    applies the 0.5%-NAV materiality filter and computes the
    reconciliation net over the FULL set. Mirrors the dashboard's
    renderPositionsPreview so the email and dashboard tell one story."""
    letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    rows = []
    latest_rebal = ""
    for key, sl in letter.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if len(trades) < 2:
            continue
        rebal_date = trades[-1].get("date", "")
        prev_date = trades[-2].get("date", "") or rebal_date
        if rebal_date > latest_rebal:
            latest_rebal = rebal_date
        sw_prev = sleeve_nav_weights(overlay, prev_date)[key]
        sw_curr = sleeve_nav_weights(overlay, rebal_date)[key]
        prev_h = {h["etf"]: h["weight"] for h in trades[-2].get("holdings", [])}
        curr_h = {h["etf"]: h["weight"] for h in trades[-1].get("holdings", [])}
        for etf in curr_h:
            if etf not in prev_h:
                rows.append({"sleeve": sl, "action": "ENTER", "etf": etf,
                             "prev": None, "new": curr_h[etf] * sw_curr,
                             "date": rebal_date,
                             "nav_impact": curr_h[etf] * sw_curr})
        for etf in prev_h:
            if etf not in curr_h:
                rows.append({"sleeve": sl, "action": "EXIT", "etf": etf,
                             "prev": prev_h[etf] * sw_prev, "new": None,
                             "date": rebal_date,
                             "nav_impact": prev_h[etf] * sw_prev})
        for etf in curr_h:
            if etf in prev_h:
                d_nav = curr_h[etf] * sw_curr - prev_h[etf] * sw_prev
                if abs(d_nav) > 1e-6:
                    rows.append({"sleeve": sl, "action": "RESIZE", "etf": etf,
                                 "prev": prev_h[etf] * sw_prev,
                                 "new": curr_h[etf] * sw_curr,
                                 "date": rebal_date, "nav_impact": abs(d_nav)})

    # Overlay trades near the latest rebalance week. A tilt flip moves 10%
    # of NAV into/out of EEM; a gate flip moves derisk_fraction into/out of
    # the fallback ticker. Both are the largest trades of exactly those
    # weeks and appeared in no table before 2026-07-18.
    if latest_rebal:
        lo = (pd.Timestamp(latest_rebal) - pd.Timedelta(days=6))
        hi = (pd.Timestamp(latest_rebal) + pd.Timedelta(days=6))
        p22 = (overlay or {}).get("phase22_eem_tilt") or {}
        fb = ((overlay or {}).get("gate_parameters") or {}).get(
            "fallback_ticker", "SHY")
        for ev in (p22.get("events") or []):
            d = ev.get("date") or ""
            if not d or not (lo <= pd.Timestamp(d) <= hi):
                continue
            day_before = (pd.Timestamp(d)
                          - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            nav_b = sleeve_nav_weights(overlay, day_before)["tilt_nav"]
            nav_a = sleeve_nav_weights(overlay, d)["tilt_nav"]
            if ev.get("direction") == "EM_TILT_ON":
                rows.append({"sleeve": "TILT", "action": "ENTER",
                             "etf": "EEM", "prev": None, "new": nav_a,
                             "date": d, "nav_impact": nav_a})
            else:
                rows.append({"sleeve": "TILT", "action": "EXIT",
                             "etf": "EEM", "prev": nav_b, "new": None,
                             "date": d, "nav_impact": nav_b})
        for ev in ((overlay or {}).get("events") or []):
            d = ev.get("date") or ""
            if not d or not (lo <= pd.Timestamp(d) <= hi):
                continue
            frac = _gate_derisk_fraction(overlay)
            if ev.get("direction") == "RISK_OFF":
                rows.append({"sleeve": "GATE", "action": "ENTER",
                             "etf": fb, "prev": None, "new": frac,
                             "date": d, "nav_impact": frac})
            else:
                rows.append({"sleeve": "GATE", "action": "EXIT",
                             "etf": fb, "prev": frac, "new": None,
                             "date": d, "nav_impact": frac})
    return rows


def _current_week_moves(activity):
    """Split activity into the latest CALENDAR WEEK's moves plus a
    summary of sleeves whose most recent rebalance is older.

    _collect_activity returns each sleeve's own latest rebalance, and
    sleeves legitimately skip weeks (no signal change; US-holiday
    Fridays), so rendering everything in one table mixes week-old moves
    the reader has already executed with the new ones. Rows from the
    same Monday-anchored week as the newest date display together —
    overlay flips (TILT / GATE rows) happen mid-week, and an exact
    latest-date match would have footnoted a Monday tilt flip as a stale
    sleeve next to Friday's rebalance rows. Older weeks' sleeves become
    a footnote, exactly as before.

    Returns ``(latest_date, current_rows, stale_sleeves)`` where
    ``stale_sleeves`` is a sorted list of ``(sleeve, date)`` pairs.
    ``latest_date`` is None when no move carries a date (corrupt input —
    caller falls back to showing nothing rather than guessing).
    """
    dated = [a for a in activity if a.get("date")]
    if not dated:
        return None, [], []
    latest = max(a["date"] for a in dated)
    # Monday of the latest date's week, via the date library (weekday():
    # Mon=0). Rows on/after this Monday are "this week".
    latest_dt = datetime.strptime(latest, "%Y-%m-%d").date()
    week_start = (latest_dt
                  - pd.Timedelta(days=latest_dt.weekday())).strftime("%Y-%m-%d")
    current = [a for a in dated if a["date"] >= week_start]
    stale = sorted({(a["sleeve"], a["date"])
                    for a in dated if a["date"] < week_start})
    return latest, current, stale


def _order_activity(shown):
    """Order the activity card's rows: action group (ENTER/EXIT/RESIZE), then
    |ΔNAV| desc, with resulting size as tie-break. Sorts in place; returns it.

    Magnitude, NOT resulting position size. The card's job is to show what
    MOVED, and ranking by the resulting weight buried the largest move: IUFS
    0.5% -> 3.9% (+3.4pp) sat BELOW a 3.0% -> 4.0% nudge purely because
    3.9 < 4.0. ``nav_impact`` is already sleeve-weighted (see
    ``_collect_activity``), so a large within-sleeve move in the small C sleeve
    cannot outrank a genuinely larger move in the big A sleeve.

    Mirrors the dashboard's ``renderPositionsPreview`` and the factsheet PDF's
    ``build_trades_table`` so all three artefacts tell one story."""
    act_order = {"ENTER": 0, "EXIT": 1, "RESIZE": 2}
    shown.sort(key=lambda a: (a.get("nav_impact", 0), a["new"] or a["prev"] or 0),
               reverse=True)
    shown.sort(key=lambda a: act_order.get(a["action"], 9))
    return shown


def _regime_colour(state):
    return {
        "RISK_ON": "#1d7a3a",
        "RISK_OFF": "#b3261e",
        "DERISK": "#b76e00",
        "EM_TILT_ON": "#1351b4",
        "EM_TILT_OFF": "#7c8590",
    }.get(state, "#3a4148")


def _compute_wtd(series: pd.Series):
    """Week-to-date return using the same algorithm as the dashboard.

    Latest equity vs equity at the trading day strictly before this
    calendar week's Monday (typically prior Friday). Returns
    (pct, from_date_iso, to_date_iso) or None if the series is too
    short or no prior-week point exists."""
    if len(series) < 2:
        return None
    last_dt = series.index[-1]
    # weekday(): Mon=0, Sun=6. Days back to Monday of this week.
    days_to_mon = last_dt.weekday()
    monday = last_dt - pd.Timedelta(days=days_to_mon)
    prior = series[series.index < monday]
    if len(prior) == 0:
        return None
    base_eq = prior.iloc[-1]
    base_dt = prior.index[-1]
    return (series.iloc[-1] / base_eq - 1.0,
            base_dt.strftime("%Y-%m-%d"),
            last_dt.strftime("%Y-%m-%d"))


# Sleeve hues, taken from the dashboard's validated categorical palette so the
# same six colours mean the same six things wherever the reader meets them —
# dashboard, public portfolio page, and this email. Do not re-pick them here.
SLEEVE_HUES = {
    "a": ("#2563eb", "Sleeve A"),
    "b": ("#b45309", "Sleeve B"),
    "c": ("#0891b2", "Sleeve C"),
    "d": ("#be185d", "Sleeve D"),
    "tilt_nav": ("#7c3aed", "EEM tilt"),
    "shy_overlay": ("#1a8754", "Cash reserve"),
}



def _alerts_strip(alerts: list[tuple[str, str]]) -> str:
    """Exceptions, at the top, or nothing at all.

    WHY THIS EXISTS. Measured on the 2026-08-14 build, "TRIPWIRE BREACHED —
    review brought forward" sat 88.5% of the way down the body, below 23
    rows of holdings, and the "REGIME STALE — DO NOT TRADE OFF THIS PANEL"
    warning renders into that same buried position. The one line that can
    change what the reader does that week was the last thing they reached,
    while a chart restating the headline tiles held the top of the page.

    RENDERS NOTHING WHEN NOTHING IS WRONG, which is the whole design. A
    status block that appears every week saying "all clear" is wallpaper
    within a month and is skipped exactly when it finally carries something.
    An empty week should look empty.

    This does not undo the earlier decision to keep regime STATE out of the
    lead — normal operating state still belongs in the footer line with the
    tilt and the allocation. Only the abnormal is promoted.
    """
    if not alerts:
        return ""
    rows = []
    for level, text in alerts:
        hue = "#b3261e" if level == "critical" else "#b76e00"
        rows.append(
            f'<tr><td style="padding:5px 0 5px 12px;border-left:3px solid {hue};'
            f'font-size:12px;color:#3a4148;line-height:1.5;">{text}</td></tr>'
        )
    return (
        '<div style="background:#fdf7f6;border:1px solid #f0d9d6;'
        'border-radius:6px;padding:12px 16px;margin-bottom:18px;">'
        '<div style="font-size:11px;letter-spacing:0.06em;color:#7c8590;'
        'text-transform:uppercase;margin-bottom:6px;">Needs attention</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
        '</div>'
    )


def _sleeve_series(d) -> pd.Series | None:
    """Equity series out of a sleeve JSON, whichever shape it uses."""
    if not d:
        return None
    h = d.get("headline", d)
    dates = (h.get("headline_equity_dates") or h.get("dates") or d.get("dates"))
    eq = (h.get("headline_equity") or h.get("equity") or d.get("equity"))
    if not dates or not eq:
        return None
    return pd.Series(eq, index=pd.to_datetime(dates)).dropna()


def _window_return(s: pd.Series | None, start_iso: str, end_iso: str):
    if s is None or s.empty:
        return None
    w = s.loc[start_iso:end_iso]
    if len(w) < 2:
        return None
    return float(w.iloc[-1]) / float(w.iloc[0]) - 1.0


def _weekly_attribution(sleeves, st_now, wtd, eem_series=None):
    """Decompose the week's blend return into per-sleeve contributions.

    contribution = sleeve weight x sleeve return, over EXACTLY the window
    the headline WTD tile uses, so the parts and the total are measured on
    the same days.

    This is exact only because the blend holds fixed 35/25/10/20 weights
    reset each week; there is no intra-week drift to account for. That is an
    assumption about the product, not a general truth about attribution, so
    the residual against the blend's own return is COMPUTED AND SHOWN rather
    than assumed to vanish. It came to -0.002pp on 2026-08-14. If it ever
    grows, the decomposition has stopped describing the portfolio and the
    line under the chart will say so before anyone acts on the split.
    """
    if not wtd:
        return None
    _, start_iso, end_iso = wtd
    plan = [("a", "a", "Sleeve A"), ("b", "b", "Sleeve B"),
            ("c", "c", "Sleeve C"), ("d", "d", "Sleeve D")]
    rows = []
    for skey, wkey, label in plan:
        r = _window_return(_sleeve_series(sleeves.get(skey)), start_iso, end_iso)
        w = st_now.get(wkey) or 0.0
        if r is None or w <= 0:
            continue
        hue = SLEEVE_HUES[wkey][0]
        rows.append({"label": label, "hue": hue, "w": w, "ret": r,
                     "contrib": w * r})
    tilt_w = st_now.get("tilt_nav") or 0.0
    tilt_r = _window_return(eem_series, start_iso, end_iso)
    if tilt_w > 0 and tilt_r is not None:
        rows.append({"label": "EEM tilt", "hue": SLEEVE_HUES["tilt_nav"][0],
                     "w": tilt_w, "ret": tilt_r, "contrib": tilt_w * tilt_r})
    if not rows:
        return None
    return {"rows": rows, "start": start_iso, "end": end_iso,
            "sum": sum(r["contrib"] for r in rows)}


def _attribution_chart(att, blend_wtd) -> str:
    """Diverging contribution bars, one row per sleeve.

    SIGN IS CARRIED BY DIRECTION, not by colour. A bar sitting left of the
    zero rule is a detractor and that reads without any colour at all, which
    frees the hue to say WHICH SLEEVE — the same blue/amber/cyan/pink/purple
    the reader has already met on the allocation bar, the dashboard and the
    public page. Colouring these green and red instead would have spent the
    palette restating what position already says, and lost the sleeve.

    The negative track is only allocated when something is actually negative.
    In a week where every sleeve contributed, half the width would otherwise
    sit empty to hold a column for nothing.
    """
    if not att:
        return ""
    rows, total = att["rows"], att["sum"]
    max_abs = max(abs(r["contrib"]) for r in rows) or 1.0
    any_neg = any(r["contrib"] < 0 for r in rows)
    pos_w, neg_w = (150, 90) if any_neg else (240, 0)

    body = []
    for r in rows:
        c = r["contrib"] * 100
        px = max(2, round(abs(r["contrib"]) / max_abs * (neg_w if c < 0 else pos_w)))
        if c < 0:
            track = (
                f'<td style="width:{neg_w}px;padding:0;" align="right">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" '
                f'style="border-collapse:collapse;margin-left:auto;"><tr>'
                f'<td style="width:{px}px;background:{r["hue"]};height:9px;'
                f'font-size:0;line-height:0;border-radius:2px;">&nbsp;</td>'
                f'</tr></table></td>'
                f'<td style="width:{pos_w}px;padding:0;">&nbsp;</td>'
            )
        else:
            track = (
                (f'<td style="width:{neg_w}px;padding:0;">&nbsp;</td>' if any_neg else "")
                + f'<td style="width:{pos_w}px;padding:0;">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" '
                f'style="border-collapse:collapse;"><tr>'
                f'<td style="width:{px}px;background:{r["hue"]};height:9px;'
                f'font-size:0;line-height:0;border-radius:2px;">&nbsp;</td>'
                f'<td style="font-size:0;line-height:0;">&nbsp;</td>'
                f'</tr></table></td>'
            )
        body.append(
            f'<tr><td style="padding:3px 8px 3px 0;font-size:11px;'
            f'color:#3a4148;white-space:nowrap;">{r["label"]}</td>'
            f'{track}'
            f'<td style="padding:3px 0 3px 8px;font-size:11px;text-align:right;'
            f'font-family:{MONO};color:#0f1217;white-space:nowrap;">'
            f'{c:+.2f}pp</td></tr>'
        )

    resid = (total - blend_wtd) * 100 if blend_wtd is not None else None
    resid_txt = ""
    if resid is not None:
        # A residual that rounds to zero is reported as "under 0.01pp", not as
        # "-0.00pp" — a signed zero invites the reader to wonder which way it
        # went when the honest answer is that it is too small to matter.
        shown = ("under 0.01pp" if abs(resid) < 0.005 else f"{resid:+.2f}pp")
        resid_txt = (f' &nbsp;&middot;&nbsp; sleeves sum {total * 100:+.2f}pp '
                     f'vs blend {blend_wtd * 100:+.2f}pp '
                     f'(residual {shown})')
    return (
        '<div style="margin:0 0 18px 0;">'
        '<div style="font-size:11px;letter-spacing:0.06em;color:#7c8590;'
        'text-transform:uppercase;margin-bottom:6px;">'
        'Where the week came from</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;">'
        f'{"".join(body)}</table>'
        '<div style="border-top:1px solid #e1e4e8;margin-top:6px;padding-top:5px;'
        'font-size:11px;color:#7c8590;">'
        f'{att["start"]} &rarr; {att["end"]}, weight &times; sleeve return'
        f'{resid_txt}</div></div>'
    )


def _move_bar(delta: float, max_abs: float, width: int = 54) -> str:
    """Magnitude bar for one rebalance move, scaled to the largest move shown.

    Direction is carried by the arrow that sits beside this, NOT by colour
    alone — the arrow is what a colour-blind reader has, and the palette's
    green/red pair is the one that fails most often. Colour here reinforces
    direction; it does not encode it.
    """
    if not max_abs or delta is None:
        return "&nbsp;"
    w = max(1, round(abs(delta) / max_abs * width))
    hue = "#1d7a3a" if delta >= 0 else "#b3261e"
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;width:{width}px;"><tr>'
        f'<td style="width:{w}px;background:{hue};height:6px;font-size:0;'
        f'line-height:0;border-radius:2px;">&nbsp;</td>'
        f'<td style="font-size:0;line-height:0;">&nbsp;</td>'
        '</tr></table>'
    )


def _allocation_bar(st_now: dict) -> str:
    """A stacked allocation bar, built from table cells rather than SVG.

    Email clients are the constraint, not taste. Gmail strips <svg> and
    Outlook renders CSS flex unpredictably, so the only encoding that survives
    everywhere is a fixed-layout table whose cells carry a background colour
    and a percentage width. font-size:0/line-height:0 stops the cell inheriting
    a text line box, which is what otherwise makes these bars 18px tall in
    Outlook and 12px everywhere else.

    Every segment is drawn at its true width, including ones too small to see.
    A 1bp cash reserve renders as nothing, which is honest — the legend beneath
    still names it and carries the number, and the same segment becomes the
    second largest on the bar the moment the gate de-risks.
    """
    segs, legend = [], []
    for key, (hue, label) in SLEEVE_HUES.items():
        w = st_now.get(key) or 0.0
        if w <= 0:
            continue
        segs.append(
            f'<td style="width:{w * 100:.4f}%;background:{hue};'
            f'height:12px;font-size:0;line-height:0;">&nbsp;</td>'
        )
        # The de-risk flag rides with the segment, not in a caption elsewhere.
        # The plain-text line this replaced said "(DE-RISKED)" and that signal
        # is the single most consequential thing the allocation can say.
        flag = ""
        if key == "shy_overlay" and st_now.get("derisk_on"):
            flag = (' <strong style="color:#b3261e;">DE-RISKED</strong>')
        legend.append(
            f'<span style="white-space:nowrap;margin-right:14px;">'
            f'<span style="display:inline-block;width:9px;height:9px;'
            f'background:{hue};border-radius:2px;"></span>'
            f'<span style="font-size:11px;color:#3a4148;">&nbsp;{label} '
            f'<strong>{w * 100:g}%</strong>{flag}</span></span>'
        )
    if not segs:
        return ""
    return (
        '<div style="margin-bottom:18px;">'
        '<div style="font-size:11px;letter-spacing:0.06em;color:#7c8590;'
        'text-transform:uppercase;margin-bottom:6px;">Allocation by sleeve</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;table-layout:fixed;'
        'border-radius:3px;overflow:hidden;">'
        f'<tr>{"".join(segs)}</tr></table>'
        f'<div style="margin-top:8px;line-height:1.9;">{"".join(legend)}</div>'
        '</div>'
    )



def _sentence(s: str) -> str:
    """Upper-case the first character only. str.capitalize() lower-cases the
    rest, which turned "sleeves B and C" into "sleeves b and c" on 2026-08-31."""
    return s[:1].upper() + s[1:] if s else s


def _long_date(iso: str | None) -> str:
    """'Tue 8 Sep 2026' from ISO via the date library; never a computed
    weekday. '—' when absent."""
    if not iso:
        return "&mdash;"
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%a %d %b %Y").replace(" 0", " ", 1)


# Same materiality as the dashboard's next-fill card: below 5bp of NAV nobody
# places an order, and a row about it reads as an instruction.
NF_MIN_MOVE = 5e-4


def _next_fill_section(lt: dict, nf: dict, name_cell) -> str:
    """The planned fill, with its derived commentary (2026-09-06).

    Mirrors the dashboard card's three states and safety labels. PLANNED:
    every sleeve final on the close its fill uses. PARTLY HELD: every sleeve
    ranked on that close but one or more HOLD — the final sleeves' moves are
    actionable, the held sleeve's book is unchanged and says so. PROVISIONAL:
    ranked before the decision close, so every remaining session re-ranks it.
    ``nf`` is the commentary block for the same decision session (may be
    empty: then the table renders without its "why" lines, never with
    invented ones).
    """
    sleeves = lt.get("sleeves") or []
    held_sl = [s for s in sleeves if s.get("status") != "READY"]
    ready_sl = [s for s in sleeves if s.get("status") == "READY"]
    ranked = bool(sleeves) and all(
        s.get("decision_session") and s.get("decision_session_for_fill")
        and s["decision_session"] == s["decision_session_for_fill"] for s in sleeves)
    final = lt.get("targets_final") is True
    partly = (not final) and ranked and bool(held_sl) and bool(ready_sl)
    fills = (lt.get("next_fill") or {}).get("by_venue") or {}
    when = ", ".join(f"{v} {_long_date(d)}" for v, d in sorted(fills.items()) if d) or "&mdash;"
    decided = sorted({s.get("decision_session_for_fill") for s in sleeves
                      if s.get("decision_session_for_fill")})
    ranked_on = _long_date(decided[-1]) if decided else "the decision"

    def _names(arr):
        n = [s["sleeve"] for s in arr]
        return n[0] if len(n) == 1 else ", ".join(n[:-1]) + " and " + n[-1]

    if final:
        pill, colour = "PLANNED", "#b76e00"
        note = (f"<strong>Nothing here has been traded.</strong> These are the target "
                f"weights for the next fill, ranked on the {_long_date(lt.get('as_of'))} "
                f"close and intended for {when}.")
    elif partly:
        pill, colour = "PARTLY HELD", "#b76e00"
        note = (f"<strong>Nothing here has been traded.</strong> Sleeve"
                f"{'' if len(ready_sl) == 1 else 's'} {_names(ready_sl)} "
                f"{'is' if len(ready_sl) == 1 else 'are'} final: ranked on the {ranked_on} "
                f"close, the close {when} uses. Sleeve"
                f"{'' if len(held_sl) == 1 else 's'} {_names(held_sl)} "
                f"{'is' if len(held_sl) == 1 else 'are'} held and must be left as held.")
    else:
        pill, colour = "PROVISIONAL", "#7c8590"
        note = (f"<strong>Nothing here has been traded, and these are not final.</strong> "
                f"A fill on {when} is ranked on the {ranked_on} close; this is ranked on "
                f"{_long_date(lt.get('as_of'))}, so every session between now and then "
                f"re-ranks it.")

    moves = [ln for ln in (lt.get("lines") or []) if abs(ln.get("delta", 0)) >= NF_MIN_MOVE]
    moves.sort(key=lambda x: -abs(x["delta"]))
    why = {(m.get("sleeve"), m.get("etf")): m.get("text") for m in (nf.get("moves") or [])}
    action_colour = {"BUY": "#1d7a3a", "ADD": "#1d7a3a", "TRIM": "#b76e00", "SELL ALL": "#b3261e"}

    out = [
        '<h3 style="margin:0 0 10px 0;font-size:14px;color:#3a4148;'
        'text-transform:uppercase;letter-spacing:1px;">Next fill '
        f'<span style="display:inline-block;font-size:10px;font-weight:700;'
        f'letter-spacing:.04em;padding:2px 8px;border-radius:999px;margin-left:8px;'
        f'background:rgba(183,110,0,0.12);color:{colour};'
        f'border:1px solid rgba(183,110,0,0.30);vertical-align:2px;">{pill}</span>'
        f'<span style="float:right;font-size:11px;color:#7c8590;font-weight:400;'
        f'text-transform:none;letter-spacing:0;">{when}</span></h3>',
        f'<p style="font-size:12.5px;color:#4a5159;margin:2px 0 10px;line-height:1.5;">'
        f'{note}</p>',
    ]
    if not moves:
        out.append('<p style="color:#7c8590;font-style:italic;margin-bottom:18px;">'
                   'No position changes above 0.05pp of NAV at the next fill.</p>')
    else:
        out.append('<table style="width:100%;border-collapse:collapse;margin-bottom:6px;'
                   'font-size:13px;">')
        out.append(
            '<tr style="color:#7c8590;font-size:11px;text-transform:uppercase;'
            'letter-spacing:0.5px;">'
            '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #c8ccd2;">Sleeve</th>'
            '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #c8ccd2;">Action</th>'
            '<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #c8ccd2;">Ticker &amp; name</th>'
            '<th style="text-align:right;padding:4px 10px;border-bottom:1px solid #c8ccd2;">Held &rarr; Target (% NAV)</th></tr>')
        for ln in moves:
            held, target = ln.get("held", 0.0), ln.get("target", 0.0)
            action = ("BUY" if held <= 0 and target > 0 else "SELL ALL" if target <= 0 and held > 0
                      else "ADD" if ln["delta"] > 0 else "TRIM")
            hold_tag = ('' if ln.get("status") == "READY" else
                        ' <span style="font-size:10px;color:#b3261e;">hold</span>')
            out.append(
                f'<tr><td style="padding:6px 10px;color:#3a4148;vertical-align:top;'
                f'border-bottom:1px solid #f0f2f4;">{ln["sleeve"]}</td>'
                f'<td style="padding:6px 10px;font-weight:700;font-size:11px;letter-spacing:0.4px;'
                f'vertical-align:top;color:{action_colour[action]};border-bottom:1px solid #f0f2f4;">'
                f'{action}{hold_tag}</td>'
                f'<td style="padding:6px 10px;vertical-align:top;border-bottom:1px solid #f0f2f4;">'
                f'{name_cell(ln["etf"])}</td>'
                f'<td style="padding:6px 10px;text-align:right;font-family:{MONO};vertical-align:top;'
                f'border-bottom:1px solid #f0f2f4;">{held * 100:.1f}% &rarr; {target * 100:.1f}% '
                f'<span style="color:{"#1d7a3a" if ln["delta"] >= 0 else "#b3261e"};">'
                f'({ln["delta"] * 100:+.1f}pp)</span></td></tr>')
        out.append('</table>')
        out.append(f'<p style="color:#7c8590;font-size:11px;margin:6px 0 8px 0;line-height:1.5;">'
                   f'{len(moves)} move{"" if len(moves) == 1 else "s"}, one-way turnover '
                   f'{(lt.get("one_way_turnover") or 0) * 100:.2f}% of NAV.</p>')
    for s in held_sl:
        out.append('<p style="font-size:12.5px;margin:6px 0 0;padding:7px 10px;border-radius:6px;'
                   'background:rgba(179,38,30,0.06);color:#b3261e;border:1px solid rgba(179,38,30,0.20);">'
                   f'<strong>Do not trade sleeve {s["sleeve"]}.</strong> '
                   f'{s.get("reason") or "Its signal does not reach the last close."} '
                   f'Leave it as held.</p>')
    why_html = _why_block(nf, why, moves)
    if why_html:
        out.append(why_html)
    else:
        out.append('<div style="margin:0 0 18px;"></div>')
    return "".join(out)


def _why_block(nf: dict, why: dict, moves: list[dict]) -> str:
    """"Why these moves", grouped by sleeve: the unit once in the heading,
    the numbers in columns, a one-line story per sleeve. Falls back to the
    flat sentences for a commentary written before the groups existed."""
    groups = nf.get("sleeves") or []
    if not groups and not any(why.get((ln["sleeve"], ln["etf"])) for ln in moves) \
            and not nf.get("summary"):
        return ""
    head = ('<div style="margin:10px 0 18px;font-size:12.5px;line-height:1.5;color:#1a1a1a;">'
            '<div style="font-size:11px;font-weight:700;letter-spacing:.04em;'
            'text-transform:uppercase;color:#3a4148;margin:0 0 4px;">Why these moves</div>'
            + (f'<p style="margin:0 0 8px;color:#4a5159;">{nf["summary"]}</p>'
               if nf.get("summary") else ''))
    if not groups:
        items = [f'<li style="margin:0 0 4px;">{why[(ln["sleeve"], ln["etf"])]}</li>'
                 for ln in moves if why.get((ln["sleeve"], ln["etf"]))]
        return head + (f'<ul style="margin:0;padding-left:18px;">{"".join(items)}</ul>' if items else '') + '</div>'
    th = ('style="text-align:{a};padding:3px 8px;border-bottom:1px solid #c8ccd2;'
          'font-size:10.5px;color:#7c8590;text-transform:uppercase;letter-spacing:0.4px;font-weight:600;"')
    td = 'style="padding:4px 8px;border-bottom:1px solid #f0f2f4;vertical-align:top;{x}"'
    parts = [head]
    for g in groups:
        parts.append(f'<div style="margin:8px 0 3px;font-weight:700;color:#0f1217;">{g["heading"]}</div>')
        if g.get("story"):
            parts.append(f'<div style="margin:0 0 5px;color:#4a5159;">{g["story"]}</div>')
        parts.append('<table style="width:100%;border-collapse:collapse;margin:0 0 6px;font-size:12px;">'
                     f'<tr><th {th.format(a="left")}>Ticker</th><th {th.format(a="right")}>Move (% NAV)</th>'
                     f'<th {th.format(a="right")}>{g["signal_unit"]}</th><th {th.format(a="right")}>Rank</th></tr>')
        for m in g["moves"]:
            if m["action"] == "BUY":
                move = f'enters {m["target"] * 100:.1f}'
            elif m["action"] == "SELL ALL":
                move = f'exits {m["held"] * 100:.1f}'
            else:
                move = f'{m["held"] * 100:.1f} &rarr; {m["target"] * 100:.1f}'
            dcol = "#1d7a3a" if m["delta"] >= 0 else "#b3261e"
            move += f' <span style="color:{dcol};">({m["delta"] * 100:+.1f}pp)</span>'
            if m.get("cash_proxy"):
                sig, rank = "cash proxy", "&mdash;"
            else:
                sig = (f'{m["signal_prev_fmt"]} &rarr; {m["signal_now_fmt"]}'
                       if m.get("signal_prev") is not None else m["signal_now_fmt"])
                if m.get("rank_now") is None:
                    rank = "&mdash;"
                elif m.get("rank_prev") and m["rank_prev"] != m["rank_now"]:
                    rank = f'{m["rank_prev"]} &rarr; {m["rank_now"]} of {m["n"]}'
                else:
                    rank = f'{m["rank_now"]} of {m["n"]}'
                if m.get("cut") == "out":
                    rank += f' <span style="color:#b3261e;">out of top {g["top_k"]}</span>'
                elif m.get("cut") == "in":
                    rank += f' <span style="color:#1d7a3a;">into top {g["top_k"]}</span>'
            name = (f'<br><span style="font-size:10.5px;color:#7c8590;">{m["name"]}</span>'
                    if m.get("name") else "")
            parts.append(
                f'<tr><td {td.format(x="")}><strong style="font-family:{MONO};">{m["traded"]}</strong>{name}</td>'
                f'<td {td.format(x="text-align:right;font-family:" + MONO + ";white-space:nowrap;")}>{move}</td>'
                f'<td {td.format(x="text-align:right;font-family:" + MONO + ";white-space:nowrap;")}>{sig}</td>'
                f'<td {td.format(x="text-align:right;white-space:nowrap;")}>{rank}</td></tr>')
        parts.append('</table>')
    parts.append('</div>')
    return "".join(parts)


def _week_block(week: dict) -> str:
    """The week as short labelled lines: headline, by sleeve, helped, hurt,
    regime, and the basis as a footnote. Falls back to the paragraph."""
    if not week:
        return ""
    if not week.get("headline"):
        return (f'<p style="margin:0 0 18px 0;font-size:13px;line-height:1.55;color:#1a1a1a;">'
                f'{week.get("text", "")}</p>') if week.get("text") else ""
    rows = "".join(
        f'<tr><td style="padding:2px 10px 2px 0;color:#7c8590;font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.4px;white-space:nowrap;vertical-align:top;">{ln["label"]}</td>'
        f'<td style="padding:2px 0;color:#1a1a1a;">{ln["text"]}</td></tr>'
        for ln in week.get("lines") or [])
    basis = (f'<p style="margin:6px 0 0;font-size:11px;color:#7c8590;line-height:1.45;">{week["basis"]}</p>'
             if week.get("basis") else "")
    return ('<div style="margin:0 0 18px 0;font-size:12.5px;line-height:1.5;">'
            f'<div style="font-weight:700;color:#0f1217;margin:0 0 4px;">{week["headline"]}</div>'
            f'<table style="border-collapse:collapse;font-size:12.5px;">{rows}</table>'
            + basis + '</div>')


def build_html(out_path: Path):
    multi = _load_json(DATA_DIR / "multi_strategy.json")
    overlay = _load_json(DATA_DIR / "risk_overlay.json")
    live_track = _load_json(DATA_DIR / "live_track.json")  # optional
    sleeves = {}
    for key, fname in [("a", "topk_robustness.json"),
                        ("b", "asset_class_rotation.json"),
                        ("c", "thematic_rotation.json"),
                        ("d", "europe_rotation.json")]:
        d = _load_json(DATA_DIR / fname)
        if d:
            sleeves[key] = d

    deployed_key, dates, equity = _get_deployed_series(multi, overlay, live_track)
    series = pd.Series(equity, index=pd.to_datetime(dates))
    asof = series.index[-1]
    asof_str = asof.strftime("%d %B %Y")
    asof_iso = asof.strftime("%Y-%m-%d")

    # Headline stats
    ytd = _ytd_return(series)
    r1y = _one_year_return(series)
    sharpe = _sharpe_full(series)
    mdd = _max_drawdown(series)
    wtd = _compute_wtd(series)

    # Regime + tilt state
    regime_state, regime_since = _regime_state(overlay)

    # Phase 28.5 — regime publish freshness guard. The 2026-06-13 email
    # printed 'RISK_ON since 2025-05-02' while the panel had stopped
    # advancing 11 trading days earlier; nothing in this path noticed.
    breadth_panel = _load_json(DATA_DIR / "breadth_csp1.json") or {}
    panel_end_iso = breadth_panel.get("end_date")
    # Tilt card resolved against the same panel date the regime card uses,
    # so a stalled tilt feed is caught by the same yardstick.
    tilt_state, tilt_since, tilt_ratio = _eem_tilt_state(overlay, panel_end_iso)
    regime_publish = None
    if panel_end_iso and overlay and overlay.get("current_breadth") is not None:
        gp = (overlay.get("gate_parameters") or {})
        regime_publish = regime_publish_status(
            panel_end_date=date.fromisoformat(panel_end_iso),
            current_breadth=overlay["current_breadth"],
            off_threshold=gp.get("off_threshold", 0.20),
            on_threshold=gp.get("on_threshold", 0.50),
            today=date.today(),
        )

    # Holdings + activity — sleeve weights carry BOTH overlays for the
    # as-of date (tilt funding and the de-risk gate) via overlay_state.
    holdings = _collect_holdings(sleeves, overlay, asof_iso)
    activity = _collect_activity(sleeves, overlay)
    labels = _build_label_map(sleeves)

    # Allocation summary from the same per-date weights the tables use.
    # Rendered by _allocation_bar; the mono run-on string it replaced carried
    # the same four-to-six figures with no shape and no colour tying them to
    # anything else the reader had already seen.
    st_now = sleeve_nav_weights(overlay, asof_iso)

    # ----- HTML assembly --------------------------------------------------
    css = (
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,"
        "Arial,sans-serif;color:#0f1217;line-height:1.5;"
        "max-width:640px;margin:0 auto;padding:0 8px;"
    )
    out = []
    out.append(f'<div style="{css}">')

    # Banner
    out.append(
        f'<div style="background:#1a2333;color:#fff;padding:18px 22px;'
        f'border-radius:6px;margin-bottom:18px;">'
        f'<div style="font-size:18px;font-weight:600;letter-spacing:0.3px;">'
        f'USD Multi-Strategy ETF Portfolio</div>'
        f'<div style="font-size:13px;color:#b8c0cc;margin-top:4px;">'
        f'Weekly factsheet &middot; signals as of <strong style="color:#fff;">'
        f'{asof_str}</strong></div></div>'
    )

    # ----- Mixed pricing basis (2026-08-31) -----
    # The as-at above is ONE date over four sleeves. When they do not share a
    # session, that line is not the whole truth and the NAV below it fuses two
    # vintages. Placed here, immediately under the as-at and above the
    # holdings, because a reader who stops after the first screen must still
    # have seen it — a footnote would not do.
    #
    # Built from data/strategy_freshness.json, never from a typed date, so it
    # states whatever is actually true and vanishes by itself once the sleeves
    # agree. The third sentence is the substantive point and was approved as
    # the reason this is publishable at all: selection happens WITHIN a
    # sleeve, so a per-sleeve vintage cannot corrupt any rebalance.
    fresh = _load_json(DATA_DIR / "strategy_freshness.json") or {}
    _rows = [s for s in (fresh.get("strategies") or []) if s.get("data_through")]
    _groups: dict[str, list[str]] = {}
    for _s in _rows:
        _groups.setdefault(_s["data_through"], []).append(_s["sleeve"])
    if len(_groups) > 1:
        _parts = []
        for _d in sorted(_groups, reverse=True):
            _sl = sorted(_groups[_d])
            _nm = (f"sleeve {_sl[0]}" if len(_sl) == 1
                   else f"sleeves {', '.join(_sl[:-1])} and {_sl[-1]}")
            # Weekday spelled out, from a date library rather than reasoned
            # about: a factsheet naming the wrong weekday for its own close
            # is the kind of error that costs a reader's trust in every other
            # number on the page. %d is zero-padded on Windows, so strip it.
            _dt = datetime.strptime(_d, "%Y-%m-%d")
            _pretty = _dt.strftime("%A %d %B %Y").replace(" 0", " ", 1)
            _parts.append(f"{_nm} to {_pretty}")
        out.append(
            '<div style="margin:0 0 18px;padding:12px 14px;background:#fff8e6;'
            'border-left:3px solid #d99e00;border-radius:4px;font-size:13px;'
            'line-height:1.55;color:#1a1a1a;">'
            '<strong>The pricing basis for this week is mixed.</strong> '
            + _sentence('; '.join(_parts)) + '.'
            '<div style="margin-top:8px;">The price vendor withheld this '
            'week’s closing prices across every holding and has not '
            'restored them. The lines that could be recovered from a second '
            'licensed feed were; that feed does not carry every market, so the '
            'remainder could not be brought forward.</div>'
            '<div style="margin-top:8px;">Each sleeve’s own rebalance is '
            'unaffected: selection happens within a sleeve, and every sleeve '
            'is internally consistent on a single session. The blended NAV '
            'combines the vintages above.</div>'
            '</div>'
        )

    # (Regime state moved to a compact footer line below — holdings and
    # activity are what readers want to see first.)
    #
    # Exceptions are the one thing that outranks them, so a slot is reserved
    # here and filled once every check has run. Reserved rather than appended
    # because the C-seat tripwire is not known until much further down the
    # build, and it must print above everything, not where it is discovered.
    alerts: list[tuple[str, str]] = []
    alert_slot = len(out)
    out.append("")

    if regime_publish and regime_publish.status == "stale":
        alerts.append(("critical",
                       "<strong>Regime panel is stale — do not trade off it.</strong> "
                       f"{regime_publish.message}"))
    elif regime_publish and regime_publish.status == "near":
        alerts.append(("warn",
                       f"Regime <strong>{regime_state}</strong> is near its "
                       f"threshold — a flip is live this week."))
    # Same compound condition _eem_tilt_state uses to force the tilt OFF, so
    # the alert fires exactly when the state below has been degraded — never
    # on a stale flag the tilt logic itself decided to ignore.
    if overlay and tilt_signal_stale(overlay) and (
            not panel_end_iso or tilt_stale_on(overlay, panel_end_iso)):
        alerts.append(("warn",
                       f"EEM tilt feed is stale since "
                       f"{tilt_signal_as_of(overlay)} — the tilt is forced OFF "
                       f"rather than trusted."))

    # Headline stats — adds Week-to-date as the leading card (live
    # deployment tracking) alongside the longer-window backtest stats.
    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               'Headline performance</h3>')
    out.append('<table style="width:100%;border-collapse:collapse;'
               'margin-bottom:6px;font-size:13px;">')
    sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "n/a"
    wtd_pct = wtd[0] if wtd else None
    wtd_colour = "#1d7a3a" if (wtd_pct is not None and wtd_pct >= 0) else "#b3261e"
    wtd_str = _fmt_pct(wtd_pct, signed=True, dp=2) if wtd_pct is not None else "n/a"

    # Every return cell carries its benchmark in-cell ('SPY x · vs y'),
    # mirroring the factsheet PDF's KPI cards so the body and the attached
    # PDF answer "did we beat the index?" the same way and in one place.
    # Sharpe and max drawdown show SPY as bare context: a signed "vs" delta
    # on a ratio, or on two negative drawdowns, reads ambiguously.
    # One load, shared by the headline tiles and the relative chart, so the
    # benchmark quoted in the numbers is the one drawn in the picture.
    spy = _load_spy()
    spy_m = _spy_metrics(spy, series, wtd)

    def _bench(spy_val, strat_val, dp=1, ratio=False):
        base = ('<div style="font-size:11px;color:#7c8590;'
                'margin-top:3px;line-height:1.35;">')
        if spy_val is None:
            return ""
        if ratio:
            return f'{base}SPY {spy_val:.2f}</div>'
        spy_txt = _fmt_pct(spy_val, signed=True, dp=dp)
        if strat_val is None:
            return f'{base}SPY {spy_txt}</div>'
        d = strat_val - spy_val
        dcol = "#1d7a3a" if d >= 0 else "#b3261e"
        return (f'{base}SPY {spy_txt} &middot; '
                f'<span style="color:{dcol};font-weight:700;">'
                f'vs {_fmt_pct(d, signed=True, dp=dp)}</span></div>')

    # (label, value, value_colour, value_size, value_weight, highlighted, bench_html)
    cells = [
        ("Week-to-date", wtd_str, wtd_colour, 16, 700, True,
         _bench(spy_m.get("wtd"), wtd_pct, dp=2)),
        ("YTD", _fmt_pct(ytd, signed=True, dp=1), "#0f1217", 15, 600, False,
         _bench(spy_m.get("ytd"), ytd)),
        ("1Y", _fmt_pct(r1y, signed=True, dp=1), "#0f1217", 15, 600, False,
         _bench(spy_m.get("r1y"), r1y)),
        ("Sharpe", sharpe_str, "#0f1217", 15, 600, False,
         _bench(spy_m.get("sharpe"), None, ratio=True)),
        ("Max DD", _fmt_pct(mdd, signed=True, dp=1), "#b3261e", 15, 600, False,
         _bench(spy_m.get("mdd"), None)),
    ]
    row = ["<tr>"]
    for label, val, colour, size, weight, hi, bench in cells:
        bg, border = ("#eef3fb", "#c5d6ee") if hi else ("#f7f8fa", "#e1e4e8")
        lbl_colour = "#3a4148" if hi else "#7c8590"
        row.append(
            f'<td style="padding:8px 10px;background:{bg};'
            f'border:1px solid {border};width:20%;text-align:center;'
            f'vertical-align:top;">'
            f'<div style="color:{lbl_colour};font-size:11px;'
            f'text-transform:uppercase;letter-spacing:0.5px;">{label}</div>'
            f'<div style="font-weight:{weight};font-size:{size}px;'
            f'color:{colour};">{val}</div>{bench}</td>'
        )
    row.append("</tr></table>")
    out.append("".join(row))
    if wtd:
        bench_note = (" Benchmark SPY over the identical windows."
                      if spy_m else "")
        out.append(
            f'<p style="margin:0 0 16px 0;font-size:11px;color:#7c8590;">'
            f'WTD window: {wtd[1]} close &rarr; {wtd[2]} close. '
            f'YTD anchors to the prior year-end close; 1Y is the trailing '
            f'calendar year; Sharpe and Max DD span the full deployed-blend '
            f'history.{bench_note}</p>'
        )

    # No trailing-year chart here. Three versions were built — columns from
    # a false floor, a position trace, then strategy against SPY — and none
    # earned its place: the five tiles above already carry WTD, YTD, 1Y,
    # Sharpe and max drawdown against the same benchmark, and the dashboard
    # draws the curve properly. A picture that restates the numbers beside
    # it is decoration, and it was holding the most valuable space on the
    # page while a breached tripwire sat at 88% depth.

    # ...and the attribution sits under the WTD figure, on the WTD window,
    # for the same reason. EEM is the one leg with no sleeve JSON of its own,
    # so its return comes from the holdings price panel the dashboard uses.
    eem_series = None
    _hp = _load_json(DATA_DIR / "holdings_prices_1y.json") or {}
    _eem = (_hp.get("prices") or {}).get("EEM")
    if _eem and _eem.get("dates") and _eem.get("prices"):
        eem_series = pd.Series(
            _eem["prices"], index=pd.to_datetime(_eem["dates"])).dropna()
    out.append(_attribution_chart(
        _weekly_attribution(sleeves, st_now, wtd, eem_series),
        wtd[0] if wtd else None))

    # ----- The week in review (2026-09-06) -----
    # Derived sentences from data/commentary.json: the blend against SPY on
    # the same window as the tile above, the sleeve split, the holdings
    # that drove it, and the regime context. Rendered only when computed
    # for THIS as-of; omitted, never estimated, otherwise.
    commentary = _load_json(DATA_DIR / "commentary.json") or {}
    _week = commentary.get("week") or {}
    if (_week.get("text") or _week.get("headline")) and _week.get("as_of") == asof_iso:
        out.append(_week_block(_week))

    # Shared cell renderer — ticker bold with the fund name as a small
    # grey secondary line. Used by both the rebalance and holdings tables.
    def _name_cell(etf: str) -> str:
        # Names stay keyed by the panel key; the printed ticker is the traded
        # one. Sleeve D's EXH3 is an internal panel id for a fund that trades
        # as EXH4.DE, so an email telling the reader what moved this week must
        # name the instrument, not the id.
        sym = display_ticker(etf)
        nm = labels.get(etf, "")
        if not nm:
            return f'<strong style="font-family:{MONO};">{sym}</strong>'
        return (f'<strong style="font-family:{MONO};">{sym}</strong>'
                f'<br><span style="font-size:11px;color:#7c8590;">{nm}</span>')

    # ----- Next fill: planned, not traded (2026-09-06) -----
    # The factsheet now goes out on the weekend BEFORE the Monday fill, so
    # the actionable payload is the planned fill, not last Monday's executed
    # one. Rendered from data/live_targets.json with the derived "why" lines
    # from data/commentary.json, carrying the same safety labels as the
    # dashboard card (PLANNED / PARTLY HELD / PROVISIONAL, the not-traded
    # sentence, the fill date per venue). Omitted entirely when the targets
    # are absent or were ranked for another as-of than this email's.
    _lt = _load_json(DATA_DIR / "live_targets.json") or {}
    if _lt.get("lines") and _lt.get("as_of") == asof_iso:
        _nf = commentary.get("next_fill") or {}
        if commentary.get("as_of") not in (None, asof_iso):
            _nf = {}
        out.append(_next_fill_section(_lt, _nf, _name_cell))

    # Latest rebalance changes — placed directly after performance (owner
    # preference 2026-07-18: the week's trades are the actionable payload
    # of this email and read first). Portfolio-level % NAV figures and a
    # net reconciliation line, aligned with the dashboard's activity card.
    # A static email has no reveal-toggle, so every move shows, with
    # sub-0.5%-NAV moves counted in the note rather than hidden.
    #
    # Only the LATEST rebalance date's rows display. _collect_activity
    # returns each sleeve's own most recent rebalance, and sleeves skip
    # weeks (no signal change; US-holiday Fridays), so one table would
    # otherwise mix week-old moves the reader has already executed with
    # the new ones (2026-07-17 build: C's 07-10 CIBR/PAVE rows sat
    # beside A/B/D's 07-17 rows under a "07-10 -> 07-17" range chip).
    # Sleeves whose latest rebalance is older get a footnote, not rows.
    MATERIAL_NAV = 0.005
    latest_rebal, shown, stale_sleeves = _current_week_moves(activity)
    _order_activity(shown)
    n_small = sum(1 for a in shown if a["nav_impact"] < MATERIAL_NAV)
    net_nav = sum((a["new"] or 0) - (a["prev"] or 0) for a in shown)
    # The card is dated by the last rebalance RUN, not the newest trade
    # (2026-09-03): on a held week the two differ and the trade date is stale.
    date_note = _latest_rebalance_iso(sleeves) or latest_rebal or asof_iso

    def _net_note():
        sign = "+" if net_nav >= -0.00005 else "−"
        note = (f'Buys minus sells net to <strong>{sign}{abs(net_nav) * 100:.1f}% NAV'
                f'</strong> across the moves shown (remainder is unchanged holdings or cash).')
        if n_small:
            note += (f' Includes {n_small} smaller move{"" if n_small == 1 else "s"} '
                     f'(&lt;0.5% NAV) — full set shown, nothing omitted.')
        return ('<p style="color:#7c8590;font-size:11px;margin:6px 0 18px 0;'
                'line-height:1.5;">' + note + '</p>')

    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               f"Latest rebalance changes <span style=\"float:right;font-size:11px;"
               f"color:#7c8590;font-weight:400;text-transform:none;"
               f"letter-spacing:0;\">rebalanced {date_note}</span></h3>")
    if not shown:
        out.append('<p style="color:#7c8590;font-style:italic;'
                   'margin-bottom:18px;">'
                   'No position changes this week &mdash; strategy stable.</p>')
    else:
        out.append('<table style="width:100%;border-collapse:collapse;'
                   'margin-bottom:6px;font-size:13px;">')
        out.append(
            '<tr style="color:#7c8590;font-size:11px;text-transform:uppercase;'
            'letter-spacing:0.5px;">'
            '<th style="text-align:left;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Sleeve</th>'
            '<th style="text-align:left;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Action</th>'
            '<th style="text-align:left;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Ticker &amp; name</th>'
            '<th style="text-align:right;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Prior &rarr; New (% NAV)</th>'
            '<th style="padding:4px 2px 4px 0;'
            'border-bottom:1px solid #c8ccd2;">&nbsp;</th></tr>'
        )
        action_colour = {"ENTER": "#1d7a3a", "EXIT": "#b3261e",
                          "RESIZE": "#b76e00"}

        # Every row carries a signed change in NAV: an ENTER is a move up
        # from nothing, an EXIT a move down to nothing. Bars are scaled to
        # the largest move IN THIS WEEK'S SET, so the column answers "which
        # of these mattered most" and never implies a fixed scale across
        # weeks — a 2% move looks the same as a 6% one if the week's biggest
        # differs, which is why the prior/new percentages stay beside it.
        def _delta(a):
            prev = a["prev"] or 0.0
            new = a["new"] or 0.0
            return new - prev

        max_abs_move = max((abs(_delta(a)) for a in shown), default=0.0)

        for a in shown:
            prev_str = f"{a['prev'] * 100:.1f}%" if a["prev"] is not None else "&mdash;"
            new_str = f"{a['new'] * 100:.1f}%" if a["new"] is not None else "&mdash;"
            # Slight direction marker on RESIZE rows: the amber action
            # label says the weight changed but not which way — ENTER and
            # EXIT already carry direction in their green/red labels.
            if (a["action"] == "RESIZE" and a["prev"] is not None
                    and a["new"] is not None):
                up = a["new"] > a["prev"]
                arrow = (f'<span style="color:{"#1d7a3a" if up else "#b3261e"};'
                         f'font-size:11px;">{"&#9650;" if up else "&#9660;"}</span>')
            else:
                arrow = "&nbsp;"
            # Arrow first (direction, non-chromatic), bar second (magnitude).
            arrow = (f'<div style="text-align:right;">{arrow}</div>'
                     f'{_move_bar(_delta(a), max_abs_move)}')
            out.append(
                f'<tr><td style="padding:6px 10px;color:#3a4148;vertical-align:top;'
                f'border-bottom:1px solid #f0f2f4;">{a["sleeve"]}</td>'
                f'<td style="padding:6px 10px;font-weight:700;font-size:11px;'
                f'letter-spacing:0.4px;vertical-align:top;'
                f'color:{action_colour.get(a["action"], "#3a4148")};'
                f'border-bottom:1px solid #f0f2f4;">{a["action"]}</td>'
                f'<td style="padding:6px 10px;vertical-align:top;'
                f'border-bottom:1px solid #f0f2f4;">{_name_cell(a["etf"])}</td>'
                f'<td style="padding:6px 10px;text-align:right;'
                f'font-family:{MONO};vertical-align:top;'
                f'border-bottom:1px solid #f0f2f4;">'
                f'{prev_str} &rarr; {new_str}</td>'
                f'<td style="padding:6px 2px 6px 0;width:16px;'
                f'vertical-align:top;border-bottom:1px solid #f0f2f4;">'
                f'{arrow}</td></tr>'
            )
        out.append('</table>')
        if stale_sleeves:
            unchanged = " &middot; ".join(
                f"Sleeve {s} unchanged this week (last traded {d})"
                for s, d in stale_sleeves)
            out.append('<p style="color:#7c8590;font-size:11px;'
                       'margin:6px 0 0 0;line-height:1.5;">'
                       + unchanged + '</p>')
        out.append(_net_note())

    # Deep-link to the full, filterable ledger on the live dashboard. The
    # email lists this week's moves; the dashboard holds the entire history
    # (every rebalance across all four sleeves). The #combined-ledger-section
    # hash opens the Trade History tab and scrolls to the ledger.
    out.append(
        '<p style="margin:0 0 18px 0;font-size:12px;">&rarr; '
        '<a href="https://phuazz.github.io/breadth-thrust-etf/#combined-ledger-section" '
        'style="color:#1351b4;font-weight:600;">Open the full interactive trade ledger</a> '
        '<span style="color:#7c8590;">&mdash; every rebalance across all four sleeves, '
        'filterable by sleeve, ETF, or date.</span></p>'
    )

    # Top holdings — mirrors the dashboard's positions-preview card:
    # ticker bold, fund name in small grey secondary line, sleeve tag,
    # % NAV, $ on $1.0M. Renders AFTER the rebalance card (owner
    # preference 2026-07-18: the week's trades are the actionable payload
    # and read first; the standing book follows).
    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               f'Current holdings <span style="float:right;font-size:11px;'
               f'color:#7c8590;font-weight:400;text-transform:none;'
               f'letter-spacing:0;">as of {asof_iso}</span></h3>')
    out.append('<table style="width:100%;border-collapse:collapse;'
               'margin-bottom:18px;font-size:13px;">')
    out.append(
        '<tr style="color:#7c8590;font-size:11px;text-transform:uppercase;'
        'letter-spacing:0.5px;">'
        '<th style="text-align:left;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">Ticker &amp; name</th>'
        '<th style="text-align:left;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">Sleeve</th>'
        '<th style="text-align:right;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">% NAV</th>'
        '<th style="text-align:right;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">$ on $1.0M</th></tr>'
    )
    for h in holdings:  # show every deployed position, not just the top 8
        cash = h["effective"] * 1_000_000
        out.append(
            f'<tr><td style="padding:6px 10px;vertical-align:top;'
            f'border-bottom:1px solid #f0f2f4;">{_name_cell(h["etf"])}</td>'
            f'<td style="padding:6px 10px;color:#3a4148;vertical-align:top;'
            f'border-bottom:1px solid #f0f2f4;">{h["sleeve"]}</td>'
            f'<td style="padding:6px 10px;text-align:right;color:#1351b4;'
            f'font-weight:600;vertical-align:top;'
            f'border-bottom:1px solid #f0f2f4;">'
            f'{h["effective"] * 100:.1f}%</td>'
            f'<td style="padding:6px 10px;text-align:right;'
            f'font-family:{MONO};vertical-align:top;'
            f'border-bottom:1px solid #f0f2f4;">'
            f'${cash:,.0f}</td></tr>'
        )
    out.append('</table>')

    # Compact regime / tilt / allocation line — single row instead of
    # the 3-row table that used to dominate the top of the email.
    # Phase 28.5 — when the breadth panel is stale, replace the regime
    # cell with a STALE banner. The email reader must see this BEFORE
    # the holdings table, not after.
    tilt_ratio_str = f", ratio {tilt_ratio:.3f}" if tilt_ratio else ""
    if regime_publish and regime_publish.status == "stale":
        out.append(
            f'<div style="background:#fff4e6;border:1px solid #b3261e;'
            f'border-radius:4px;padding:12px 16px;font-size:13px;'
            f'color:#7f1010;margin-bottom:18px;line-height:1.5;">'
            f'<strong>REGIME STALE — DO NOT TRADE OFF THIS PANEL.</strong> '
            f'{regime_publish.message}'
            f'</div>'
        )
    else:
        regime_label = regime_state
        if regime_publish and regime_publish.status == "near":
            regime_label = f"{regime_state} (NEAR THRESHOLD)"
        out.append(
            f'<div style="background:#f7f8fa;border:1px solid #e1e4e8;'
            f'border-radius:4px;padding:10px 14px;font-size:12px;'
            f'color:#3a4148;margin-bottom:18px;line-height:1.6;">'
            f'<strong>Regime:</strong> '
            f'<span style="color:{_regime_colour(regime_state)};font-weight:600;">'
            f'{regime_label}</span> since {regime_since} &nbsp;&middot;&nbsp; '
            f'<strong>EEM tilt:</strong> '
            f'<span style="color:{_regime_colour(tilt_state)};font-weight:600;">'
            f'{tilt_state}</span> since {tilt_since}{tilt_ratio_str}'
            f'</div>'
        )
        # The allocation was a run-on mono string at the end of the same line.
        # It is the one figure on the page with a shape, so it is drawn.
        out.append(_allocation_bar(st_now))

    # WS7 — Sleeve C seat watch (KICKOFF_ws7-c-seat.md §7). One line from
    # the append-only OOS tracker; absent file renders nothing (pre-
    # registration emails). A STALE tag fires when the tracker's last week
    # trails the email's as-of by more than a week — the workflow runs the
    # tracker soft-fail, so staleness must surface here, never silently.
    watch = _load_json(DATA_DIR / "c_seat_watch.json")
    if watch and watch.get("weeks"):
        wrow = watch["weeks"][-1]
        wk_end = pd.Timestamp(wrow["week_end"])
        stale_tag = (' <strong style="color:#b3261e;">(STALE &mdash; '
                     'tracker did not run this week)</strong>'
                     if (asof - wk_end).days > 7 else "")
        # States the breach, and no longer asserts its consequence. The clause
        # "review brought forward" was true of section 6's default and became
        # false on 2026-08-16 when the early review was declined and the
        # quarter held — leaving the line claiming a review had been advanced
        # while printing the original 2026-10-02 date two clauses later. The
        # date the watch line already carries is the honest statement of where
        # the review stands; the flag only needs to record that the limit was
        # crossed.
        trip_tag = ('<strong style="color:#b3261e;">TRIPWIRE BREACHED. </strong>'
                    if wrow.get("tripwire") else "")
        # NO TRIPWIRE ALERT, DELIBERATELY. The breach was raised to the
        # attention strip and then removed on 2026-08-16, and the reasoning
        # matters more than the removal.
        #
        # The finding behind it is substantiated — WS3 measured 7.5 years and
        # the rotation lost to its own same-universe equal-weight basket at a
        # 1.0x break-even cost multiple. What fails is the VEHICLE. The review
        # is deliberately held to 2026-10-02, so nothing about this is
        # actionable for the reader for seven more weeks, and a permanent
        # entry in a box headed "needs attention" is precisely the wallpaper
        # that box exists to avoid. A strip that always has something in it
        # teaches the reader to skip it, and it will then be skipped on the
        # week it finally carries a regime flip.
        #
        # Nothing is lost by the removal: the watch line below still prints
        # the gap, the seat number, the review date and the noise band every
        # week, which is monitoring where it belongs. The decision itself is
        # recorded in data/c_seat_tripwire_log.json and section 11 of
        # KICKOFF_ws7-c-seat.md, neither of which depends on the email.
        #
        # Tracker STALENESS is a different thing and stays an alert: that is
        # a plumbing fault, it is actionable the day it appears, and it is
        # silent unless something is genuinely broken.
        if (asof - wk_end).days > 7:
            alerts.append((
                "warn",
                f"C seat tracker did not run this week — last recorded "
                f"{wrow['week_end']}, email as of {asof_iso}."))
        out.append(
            f'<p style="margin:0 0 18px 0;font-size:11px;color:#7c8590;'
            f'line-height:1.5;">{trip_tag}'
            f'<strong>C seat watch (WS7):</strong> rotation vs EW-25 '
            f'{wrow["cum_rotation_minus_ew_pp"]:+.2f}pp &middot; '
            f'seat {wrow["cum_with_minus_without_pp"]:+.2f}pp '
            f'since {watch.get("anchor_date", "2026-07-03")} '
            f'(week {wrow["week_end"]}) &middot; '
            f'review {watch.get("review_date", "2026-10-02")}, '
            f'&plusmn;{watch.get("noise_band_pp", 2.0):g}pp noise band'
            f'{stale_tag}</p>'
        )

    # PDF + dashboard link
    #
    # The page count is READ FROM THE PDF, never asserted. It was hardcoded as
    # "2-page" and the factsheet had since grown to five, so the email spent an
    # unknown number of weeks telling the reader the wrong length of the thing
    # attached to it. A quantity word in prose is a number in disguise: derive
    # it from the artefact or leave it out. If the file cannot be read, the
    # phrase degrades to "Full PDF factsheet" rather than guessing.
    pages_phrase = "Full PDF factsheet"
    try:
        from pypdf import PdfReader  # noqa: PLC0415 — optional, only for prose
        n_pages = len(PdfReader(str(ROOT / "docs" / "factsheet_latest.pdf")).pages)
        if n_pages:
            pages_phrase = f"Full {n_pages}-page PDF factsheet"
    except Exception:  # noqa: BLE001 — a missing count must not fail the email
        pass
    out.append(
        '<div style="background:#f7f8fa;border:1px solid #e1e4e8;'
        'padding:14px 18px;border-radius:4px;margin-bottom:18px;">'
        '<p style="margin:0 0 6px 0;font-size:13px;">'
        f'{pages_phrase} attached: '
        f'<strong>factsheet_{asof_iso}.pdf</strong></p>'
        '<p style="margin:0;font-size:12px;color:#3a4148;">'
        'Live dashboard with charts, signal traces, and per-sleeve detail: '
        '<a href="https://phuazz.github.io/breadth-thrust-etf/" '
        'style="color:#1351b4;">phuazz.github.io/breadth-thrust-etf</a></p>'
        '</div>'
    )

    # Disclaimer
    out.append(
        '<p style="color:#7c8590;font-size:11px;line-height:1.5;'
        'border-top:1px solid #e1e4e8;padding-top:10px;margin-top:18px;">'
        'Personal research artefact. Not investment advice and not '
        'affiliated with any regulated fund or manager. Backtest performance '
        'is not indicative of future returns. Stats include reasonable '
        'transaction costs but are still subject to '
        'survivorship bias, look-ahead risk, and ~&pm;0.4 Sharpe sample '
        'noise. Deployed key: '
        f'<code style="background:#f0f2f4;padding:1px 4px;border-radius:2px;'
        f'font-family:{MONO};font-size:11px;">{deployed_key}</code>.'
        '</p>'
    )

    out.append('</div>')

    # Every check has now run, so the reserved slot can be filled. Empty
    # string when nothing is wrong, which is most weeks.
    out[alert_slot] = _alerts_strip(alerts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline is REQUIRED — the GitHub Actions workflow loads
    # this file into a $GITHUB_OUTPUT heredoc; without a trailing \n the
    # closing delimiter ends up appended to the last HTML line and the
    # heredoc never closes ("Matching delimiter not found").
    # Write in binary mode with explicit LF endings so the file is
    # deterministic on both Windows and Linux runners.
    body = ("\n".join(out) + "\n").encode("utf-8")
    out_path.write_bytes(body)
    # An operator preview may live outside the repo; the write above has
    # already happened, so a path that is not under ROOT is printed as is.
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"Wrote {shown}")
    print(f"  As of:        {asof_str}")
    print(f"  Deployed key: {deployed_key}")
    print(f"  Regime:       {regime_state} (since {regime_since})")
    print(f"  EEM tilt:     {tilt_state}")
    print(f"  Top holdings: {len(holdings)}")
    print(f"  Activity:     {len(shown)} move(s) dated {date_note}; "
          f"{n_small} sub-0.5% NAV; "
          f"{len(activity) - len(shown)} older-dated move(s) footnoted")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DOCS_DIR / "email_body.html"))
    args = p.parse_args()
    return build_html(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
