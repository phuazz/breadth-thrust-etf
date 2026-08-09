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
    st_now = sleeve_nav_weights(overlay, asof_iso)

    def _wfmt(x):
        return f"{x * 100:g}%"

    alloc_parts = [f"A {_wfmt(st_now['a'])}", f"B {_wfmt(st_now['b'])}",
                   f"C {_wfmt(st_now['c'])}", f"D {_wfmt(st_now['d'])}"]
    if st_now["tilt_nav"] > 0:
        alloc_parts.append(f"EEM tilt {_wfmt(st_now['tilt_nav'])}")
    if st_now["shy_overlay"] > 0:
        alloc_parts.append(
            f"SHY overlay {_wfmt(st_now['shy_overlay'])} (DE-RISKED)")
    alloc = " &middot; ".join(alloc_parts)

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

    # (Regime state moved to a compact footer line below — holdings and
    # activity are what readers want to see first.)

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
    spy_m = _spy_metrics(_load_spy(), series, wtd)

    def _bench(spy_val, strat_val, dp=1, ratio=False):
        base = ('<div style="font-size:10px;color:#7c8590;'
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
            f'<div style="color:{lbl_colour};font-size:10px;'
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
    date_note = latest_rebal or asof_iso

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
            '<tr style="color:#7c8590;font-size:10px;text-transform:uppercase;'
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
                         f'font-size:10px;">{"&#9650;" if up else "&#9660;"}</span>')
            else:
                arrow = "&nbsp;"
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
                f"Sleeve {s} unchanged this week (last rebalanced {d})"
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
        '<tr style="color:#7c8590;font-size:10px;text-transform:uppercase;'
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
            f'{tilt_state}</span> since {tilt_since}{tilt_ratio_str} &nbsp;&middot;&nbsp; '
            f'<strong>Allocation:</strong> '
            f'<span style="font-family:{MONO};font-size:11px;">'
            f'{alloc}</span>'
            f'</div>'
        )

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
        trip_tag = ('<strong style="color:#b3261e;">TRIPWIRE BREACHED '
                    '&mdash; review brought forward. </strong>'
                    if wrow.get("tripwire") else "")
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
    out.append(
        '<div style="background:#f7f8fa;border:1px solid #e1e4e8;'
        'padding:14px 18px;border-radius:4px;margin-bottom:18px;">'
        '<p style="margin:0 0 6px 0;font-size:13px;">'
        f'Full 2-page PDF factsheet attached: '
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
        f'font-family:{MONO};font-size:10px;">{deployed_key}</code>.'
        '</p>'
    )

    out.append('</div>')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline is REQUIRED — the GitHub Actions workflow loads
    # this file into a $GITHUB_OUTPUT heredoc; without a trailing \n the
    # closing delimiter ends up appended to the last HTML line and the
    # heredoc never closes ("Matching delimiter not found").
    # Write in binary mode with explicit LF endings so the file is
    # deterministic on both Windows and Linux runners.
    body = ("\n".join(out) + "\n").encode("utf-8")
    out_path.write_bytes(body)
    print(f"Wrote {out_path.relative_to(ROOT)}")
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
