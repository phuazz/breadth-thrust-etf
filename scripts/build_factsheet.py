"""Weekly research factsheet — one-page PDF for weekly portfolio review.

Designed for someone reviewing the strategy WEEKLY (after the Friday
pipeline run). Surfaces the information that changes week-to-week:
  - Current regime + EEM tilt state (with days-since-switch)
  - Multi-period returns (1W / 1M / 3M / YTD / 1Y / since inception)
  - Cumulative return chart with risk-overlay bands
  - Sleeve YTD performance breakdown
  - Current top holdings (deployed weight + sleeve + signal that put it there)
  - This week's rebalance activity (entries / exits / resizes vs prior week)
  - What to watch — signal levels approaching key thresholds

Read once a week to decide whether anything material has changed.

Personal research artefact — no fund / manager branding.

Usage:
    python scripts/build_factsheet.py [--out docs/factsheet_latest.pdf]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Rectangle
from matplotlib.backends.backend_pdf import PdfPages

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

# Brand palette (matches dashboard CSS variables)
INK = "#111418"
INK_SOFT = "#4a5159"
INK_FAINT = "#6b727a"
BG_SOFT = "#f7f8fa"
BORDER = "#e3e6ea"
ACCENT = "#1351b4"
GOOD = "#1d7a3a"
BAD = "#b3261e"
WARN = "#b76e00"

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Inter", "Arial", "Helvetica", "DejaVu Sans"]
plt.rcParams["pdf.fonttype"] = 42  # TrueType so text is searchable in PDF


# ----------------- Data loading --------------------------------------------

def load_all():
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    overlay_path = DATA_DIR / "risk_overlay.json"
    overlay = json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path.exists() else None
    sleeves = {}
    for key, path in [
        ("a", DATA_DIR / "topk_robustness.json"),
        ("b", DATA_DIR / "asset_class_rotation.json"),
        ("c", DATA_DIR / "thematic_rotation.json"),
        ("d", DATA_DIR / "europe_rotation.json"),
    ]:
        if path.exists():
            sleeves[key] = json.loads(path.read_text(encoding="utf-8"))
    return multi, overlay, sleeves


def get_deployed(multi, overlay):
    """Resolve deployed key with same precedence as the dashboard."""
    overlay_variants = (overlay or {}).get("gated_variants", {})
    strategies = multi.get("strategies", {})
    for key in ("blend_35_35_10_20_gated_eem_tilted",
                 "blend_35_35_10_20_gated"):
        if key in overlay_variants:
            return key, overlay_variants[key]
    for key in ("blend_35_35_10_20", "blend_45_45_10"):
        if key in strategies:
            return key, strategies[key]
    raise RuntimeError("No deployed blend found")


# ----------------- Math helpers --------------------------------------------

def window_ret(series, start, end=None):
    s = series.loc[start:end].dropna() if end else series.loc[start:].dropna()
    if len(s) < 2:
        return None
    return s.iloc[-1] / s.iloc[0] - 1


def window_stats(series, start=None, end=None):
    s = series.loc[start:end].dropna() if (start or end) else series.dropna()
    if len(s) < 5:
        return None
    s = s / s.iloc[0]
    d = s.pct_change().fillna(0)
    n_years = (s.index[-1] - s.index[0]).days / 365.25
    return {
        "sharpe": d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0,
        "cagr": s.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else 0,
        "total": s.iloc[-1] - 1,
        "vol": d.std() * math.sqrt(252),
        "dd": ((s - s.cummax()) / s.cummax()).min(),
    }


def fmt_pct(x, signed=True):
    if x is None: return "—"
    sign = "+" if (signed and x >= 0) else ""
    return f"{sign}{x*100:.1f}%"


def fmt_num(x, signed=True, dp=2):
    if x is None: return "—"
    sign = "+" if (signed and x >= 0) else ""
    return f"{sign}{x:.{dp}f}"


# ----------------- Section renderers ----------------------------------------

def render_header(ax, asof_date, deployed_key, current_breadth_pct):
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_SOFT,
                            edgecolor="none", transform=ax.transAxes))
    ax.text(0.02, 0.72, "USD Multi-Strategy ETF Portfolio",
            fontsize=15, fontweight="bold", color=INK, va="center",
            transform=ax.transAxes)
    ax.text(0.02, 0.35,
            "Personal research artefact · Weekly factsheet · "
            "4-sleeve breadth + momentum rotation · No leverage",
            fontsize=8, color=INK_SOFT, va="center",
            transform=ax.transAxes)
    ax.text(0.98, 0.65, f"As of {asof_date}",
            fontsize=10, color=INK, fontweight="600",
            ha="right", va="center", transform=ax.transAxes)
    ax.text(0.98, 0.30,
            f"S&P 500 breadth: {current_breadth_pct:.0f}% above 200d MA",
            fontsize=8, color=INK_FAINT, ha="right", va="center",
            transform=ax.transAxes)


def render_regime_state(ax, overlay):
    """Live regime + EEM tilt state strip with days-since-switch."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    today = datetime.now(timezone.utc).date()

    # Phase 19 regime gate
    state_19 = (overlay or {}).get("current_state", "UNKNOWN")
    state_19_since = (overlay or {}).get("current_state_since")
    days_19 = ((today - datetime.fromisoformat(state_19_since).date()).days
               if state_19_since else 0)
    risk_on = state_19 == "RISK_ON"
    colour_19 = GOOD if risk_on else BAD

    # Phase 22 EEM tilt
    p22 = (overlay or {}).get("phase22_eem_tilt", {})
    p22_on = p22.get("enabled", False) and p22.get("current_state") == "EM_TILT_ON"
    p22_since = p22.get("current_state_since") if p22.get("enabled") else None
    days_22 = ((today - datetime.fromisoformat(p22_since).date()).days
               if p22_since else 0)
    colour_22 = WARN if p22_on else INK_SOFT

    # Live blend composition
    if p22_on:
        tilt = int(p22.get("parameters", {}).get("tilt_weight", 0.10) * 100)
        blend_desc = f"35% A · {35-tilt}% B · 10% C · 20% D · {tilt}% EEM"
    else:
        blend_desc = "35% A · 35% B · 10% C · 20% D"

    # Three cells: regime, EEM tilt, live blend composition
    cells = [
        ("BREADTH REGIME", state_19, colour_19,
         f"since {state_19_since} ({days_19}d)"
         if state_19_since else ""),
        ("EEM TILT", p22.get("current_state", "—"), colour_22,
         f"since {p22_since} ({days_22}d)"
         if p22_since else "—"),
        ("LIVE BLEND", blend_desc, ACCENT,
         "weights effective today"),
    ]

    cell_w = 1.0 / len(cells)
    for i, (label, value, col, sub) in enumerate(cells):
        x0 = i * cell_w
        if i > 0:
            ax.plot([x0, x0], [0.1, 0.9], color=BORDER, linewidth=0.6,
                     transform=ax.transAxes)
        ax.text(x0 + 0.01, 0.78, label,
                fontsize=7, color=INK_FAINT, fontweight="600",
                va="center", transform=ax.transAxes)
        ax.text(x0 + 0.01, 0.50, value,
                fontsize=11.5, color=col, fontweight="bold",
                va="center", transform=ax.transAxes)
        ax.text(x0 + 0.01, 0.22, sub,
                fontsize=7, color=INK_FAINT, va="center",
                transform=ax.transAxes)


def render_returns_table(ax, deployed_series, spy_series):
    """Multi-period returns table — strategy + benchmark + relative."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.95, "Total return — multi-period (strategy vs SPY)",
            fontsize=10, fontweight="bold", color=INK, va="top",
            transform=ax.transAxes)

    last_date = deployed_series.index[-1]
    windows = [
        ("1W",   last_date - pd.Timedelta(days=7)),
        ("1M",   last_date - pd.DateOffset(months=1)),
        ("3M",   last_date - pd.DateOffset(months=3)),
        ("YTD",  pd.Timestamp(last_date.year, 1, 1)),
        ("1Y",   last_date - pd.DateOffset(years=1)),
        ("3Y",   last_date - pd.DateOffset(years=3)),
        ("Inc.", deployed_series.index[0]),
    ]

    # Header row
    headers = ["Period"] + [w[0] for w in windows]
    n_cols = len(headers)
    col_w = 1.0 / n_cols
    for j, h in enumerate(headers):
        ax.text(j * col_w + 0.005, 0.78, h,
                fontsize=8, color=INK_FAINT, fontweight="600",
                va="center", transform=ax.transAxes)

    # Strategy row
    ax.text(0.005, 0.58, "Strategy",
            fontsize=8.5, color=INK, fontweight="600",
            va="center", transform=ax.transAxes)
    strategy_rets = []
    for j, (_, start) in enumerate(windows):
        ret = window_ret(deployed_series, start)
        strategy_rets.append(ret)
        col = GOOD if (ret or 0) >= 0 else BAD
        ax.text((j + 1) * col_w + 0.005, 0.58, fmt_pct(ret),
                fontsize=8.5, color=col, fontweight="600",
                va="center", transform=ax.transAxes)

    # SPY row
    if spy_series is not None:
        ax.text(0.005, 0.40, "SPY",
                fontsize=8.5, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        spy_rets = []
        for j, (_, start) in enumerate(windows):
            ret = window_ret(spy_series, start)
            spy_rets.append(ret)
            col = GOOD if (ret or 0) >= 0 else BAD
            ax.text((j + 1) * col_w + 0.005, 0.40, fmt_pct(ret),
                    fontsize=8.5, color=col,
                    va="center", transform=ax.transAxes)

        # Relative row
        ax.text(0.005, 0.22, "Relative",
                fontsize=8.5, color=INK_FAINT,
                va="center", transform=ax.transAxes)
        for j in range(len(windows)):
            rel = None
            if strategy_rets[j] is not None and spy_rets[j] is not None:
                rel = strategy_rets[j] - spy_rets[j]
            col = GOOD if (rel or 0) >= 0 else BAD
            ax.text((j + 1) * col_w + 0.005, 0.22, fmt_pct(rel),
                    fontsize=8.5, color=col,
                    va="center", transform=ax.transAxes)


def render_cumulative_chart(ax, deployed_series, spy_series, overlay=None):
    s = deployed_series / deployed_series.iloc[0]
    ax.plot(s.index, (s - 1) * 100, color=ACCENT, linewidth=1.6,
            label="Deployed strategy", zorder=3)
    if spy_series is not None:
        spy = spy_series.reindex(s.index, method="ffill").dropna()
        spy = spy / spy.iloc[0]
        ax.plot(spy.index, (spy - 1) * 100, color=INK_FAINT,
                 linewidth=1.0, linestyle="--",
                 label="SPY (US large-cap)", zorder=2)
    if overlay and overlay.get("events"):
        events = overlay["events"]
        off_start = None
        for ev in events:
            if ev["direction"] == "RISK_OFF":
                off_start = pd.to_datetime(ev["date"])
            elif ev["direction"] == "RISK_ON" and off_start is not None:
                ax.axvspan(off_start, pd.to_datetime(ev["date"]),
                            color=WARN, alpha=0.10, zorder=1)
                off_start = None
        if off_start is not None:
            ax.axvspan(off_start, s.index[-1], color=WARN, alpha=0.10, zorder=1)
    ax.set_title("Cumulative return since inception (%)",
                  fontsize=9, fontweight="bold", color=INK, loc="left")
    ax.set_ylabel("Return %", fontsize=7.5, color=INK_FAINT)
    ax.tick_params(labelsize=7, colors=INK_FAINT)
    ax.grid(True, color="#eef0f3", linewidth=0.6, axis="y")
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color(BORDER)
    ax.legend(loc="upper left", fontsize=7, frameon=False)


def render_sleeve_ytd(ax, sleeves, deployed_series, spy_series, p22_active=False):
    """Sleeve YTD performance bars."""
    ax.set_title("Sleeve YTD return contribution",
                  fontsize=9, fontweight="bold", color=INK, loc="left")
    last_date = deployed_series.index[-1]
    ytd_start = pd.Timestamp(last_date.year, 1, 1)

    sleeve_meta = [
        ("a", "A · US Sectors",    ACCENT,    0.35),
        ("b", "B · Asset Class",   GOOD,      0.25 if p22_active else 0.35),
        ("c", "C · Thematic",      "#dc2626", 0.10),
        ("d", "D · Europe",        "#0e7490", 0.20),
    ]
    if p22_active:
        sleeve_meta.append(("eem", "EEM tilt", WARN, 0.10))

    names, returns, contribs, weights, colours = [], [], [], [], []
    for key, label, col, wt in sleeve_meta:
        if key == "eem":
            # Use EEM ETF YTD as approximation; we don't store EEM series separately
            ret = None
        else:
            s = sleeves.get(key, {})
            blob = s.get("headline", {})
            dates = blob.get("headline_equity_dates")
            equity = blob.get("headline_equity")
            if not dates or not equity:
                ret = None
            else:
                ser = pd.Series(equity, index=pd.to_datetime(dates))
                ret = window_ret(ser, ytd_start)
        names.append(label)
        returns.append(ret if ret is not None else 0)
        weights.append(wt)
        contribs.append((ret or 0) * wt)
        colours.append(col)

    y_pos = np.arange(len(names))
    ax.barh(y_pos, [r * 100 for r in returns], color=colours,
            edgecolor="white", linewidth=0.5, height=0.5)
    for i, (r, w, c) in enumerate(zip(returns, weights, contribs)):
        x = r * 100
        ax.text(x + (0.5 if x >= 0 else -0.5),
                i,
                f"  {fmt_pct(r)} × {w*100:.0f}% = {c*100:+.1f}pp",
                fontsize=7, color=INK_SOFT,
                va="center", ha="left" if x >= 0 else "right")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=8, color=INK)
    ax.invert_yaxis()
    ax.axvline(0, color=INK_FAINT, linewidth=0.6)
    ax.tick_params(axis="x", labelsize=7, colors=INK_FAINT)
    ax.grid(True, color="#eef0f3", linewidth=0.5, axis="x")
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color(BORDER)


def render_top_holdings(ax, sleeves, p22_active):
    """Current top 12 holdings across all sleeves (deployed weights)."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.97, "Current top holdings — deployed weights",
            fontsize=10, fontweight="bold", color=INK, va="top",
            transform=ax.transAxes)

    weights = {0.35: "a", 0.25 if p22_active else 0.35: "b",
                0.10: "c", 0.20: "d"}
    holdings = []
    sleeve_letters = {"a": "A", "b": "B", "c": "C", "d": "D"}
    sleeve_weights = {"a": 0.35, "b": 0.25 if p22_active else 0.35,
                      "c": 0.10, "d": 0.20}
    for key, sleeve_wt in sleeve_weights.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if not trades: continue
        latest = trades[-1]
        for h in latest.get("holdings", []):
            eff = h.get("weight", 0) * sleeve_wt
            signal = h.get("signal_pct") or h.get("breadth_pct")
            holdings.append({
                "etf": h.get("etf"),
                "sleeve": sleeve_letters[key],
                "within": h.get("weight", 0),
                "effective": eff,
                "signal": signal,
            })
    if p22_active:
        # Add EEM
        holdings.append({"etf": "EEM", "sleeve": "EEM",
                         "within": 1.0, "effective": 0.10,
                         "signal": None})
    holdings = sorted(holdings, key=lambda x: -x["effective"])[:12]

    # Header
    headers = ["ETF", "Sleeve", "Sleeve wt", "Effective", "Signal"]
    col_x = [0.02, 0.20, 0.36, 0.55, 0.78]
    for x, h in zip(col_x, headers):
        ax.text(x, 0.83, h, fontsize=7.5, color=INK_FAINT,
                fontweight="600", va="center", transform=ax.transAxes)

    row_h = 0.060
    y = 0.75
    for h in holdings:
        ax.text(col_x[0], y, h["etf"], fontsize=8, color=INK,
                fontweight="600", va="center", transform=ax.transAxes)
        ax.text(col_x[1], y, h["sleeve"], fontsize=8, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        ax.text(col_x[2], y, fmt_pct(h["within"], signed=False),
                fontsize=8, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        ax.text(col_x[3], y, fmt_pct(h["effective"], signed=False),
                fontsize=8, color=INK, fontweight="600",
                va="center", transform=ax.transAxes)
        sig_txt = (f"{h['signal']:+.1f}%" if h["signal"] is not None
                   else "—")
        ax.text(col_x[4], y, sig_txt, fontsize=8, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        y -= row_h


def render_recent_activity(ax, sleeves):
    """Last 1-2 rebalances per sleeve — entries / exits / resizes."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.97, "This week's rebalance activity (vs previous week)",
            fontsize=10, fontweight="bold", color=INK, va="top",
            transform=ax.transAxes)

    rows = []
    sleeve_letters = {"a": "A", "b": "B", "c": "C", "d": "D"}
    for key, sleeve in sleeve_letters.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if len(trades) < 2: continue
        prev_holdings = {h["etf"]: h["weight"] for h in trades[-2]["holdings"]}
        curr_holdings = {h["etf"]: h["weight"] for h in trades[-1]["holdings"]}
        # Entries (in current, not in prev)
        for etf in curr_holdings:
            if etf not in prev_holdings:
                rows.append((sleeve, "ENTER", etf, None, curr_holdings[etf]))
        # Exits (in prev, not in current)
        for etf in prev_holdings:
            if etf not in curr_holdings:
                rows.append((sleeve, "EXIT", etf, prev_holdings[etf], None))
        # Resizes (in both, weight differs by more than 1%)
        for etf in curr_holdings:
            if etf in prev_holdings:
                d = curr_holdings[etf] - prev_holdings[etf]
                if abs(d) > 0.01:
                    rows.append((sleeve, "RESIZE", etf,
                                prev_holdings[etf], curr_holdings[etf]))

    if not rows:
        ax.text(0.0, 0.78,
                "No material position changes this week — strategy stable.",
                fontsize=8, color=INK_SOFT, va="top", transform=ax.transAxes)
        return

    # Table header
    headers = ["Sleeve", "Action", "ETF", "Prior wt", "New wt", "Δ"]
    col_x = [0.02, 0.12, 0.24, 0.42, 0.58, 0.74]
    for x, h in zip(col_x, headers):
        ax.text(x, 0.83, h, fontsize=7, color=INK_FAINT,
                fontweight="600", va="center", transform=ax.transAxes)
    row_h = 0.062
    y = 0.74
    for sleeve, action, etf, prev_w, new_w in rows[:9]:
        action_colour = (GOOD if action == "ENTER"
                          else BAD if action == "EXIT"
                          else WARN)
        ax.text(col_x[0], y, sleeve, fontsize=8, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        ax.text(col_x[1], y, action, fontsize=7.5, color=action_colour,
                fontweight="600", va="center", transform=ax.transAxes)
        ax.text(col_x[2], y, etf, fontsize=8, color=INK,
                fontweight="600", va="center", transform=ax.transAxes)
        ax.text(col_x[3], y,
                fmt_pct(prev_w, signed=False) if prev_w is not None else "—",
                fontsize=8, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        ax.text(col_x[4], y,
                fmt_pct(new_w, signed=False) if new_w is not None else "—",
                fontsize=8, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        d = (new_w or 0) - (prev_w or 0)
        ax.text(col_x[5], y, f"{d*100:+.1f}pp",
                fontsize=8, color=GOOD if d >= 0 else BAD,
                va="center", transform=ax.transAxes)
        y -= row_h


def render_what_to_watch(ax, overlay):
    """Signal levels approaching thresholds — actionable forward-looking."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.95, "What to watch next week",
            fontsize=9, fontweight="bold", color=INK, va="top",
            transform=ax.transAxes)

    items = []
    if overlay:
        cur_breadth = overlay.get("current_breadth", 0) * 100
        off_thresh = (overlay.get("gate_parameters", {}).get("off_threshold", 0.20)) * 100
        on_thresh = (overlay.get("gate_parameters", {}).get("on_threshold", 0.50)) * 100
        state = overlay.get("current_state")
        if state == "RISK_ON":
            margin = cur_breadth - off_thresh
            items.append(f"Breadth gate: {cur_breadth:.0f}% — "
                          f"would trigger RISK_OFF if it falls below {off_thresh:.0f}% "
                          f"(buffer: {margin:.0f}pp).")
        else:
            margin = on_thresh - cur_breadth
            items.append(f"Breadth gate: {cur_breadth:.0f}% — "
                          f"would re-engage full blend if it rises above {on_thresh:.0f}% "
                          f"(needs: +{margin:.0f}pp).")

        p22 = overlay.get("phase22_eem_tilt", {})
        if p22.get("enabled"):
            fast = p22.get("current_fast_ma", 0)
            slow = p22.get("current_slow_ma", 0)
            ratio = p22.get("current_ratio", 0)
            spread = fast - slow
            spread_pct = (spread / slow * 100) if slow else 0
            if p22.get("current_state") == "EM_TILT_ON":
                items.append(f"EEM tilt: 50d MA {spread_pct:+.1f}% vs 200d MA — "
                              f"tilt deactivates on a death cross "
                              f"(50d below 200d).")
            else:
                items.append(f"EEM tilt: 50d MA {spread_pct:+.1f}% vs 200d MA — "
                              f"tilt activates on a golden cross "
                              f"(50d above 200d).")

    for i, item in enumerate(items[:3]):
        ax.text(0.02, 0.75 - i * 0.22, "• " + item,
                fontsize=7.5, color=INK_SOFT, va="top",
                wrap=True, transform=ax.transAxes)


def render_footer(ax, computed_at):
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_SOFT,
                            edgecolor="none", transform=ax.transAxes))
    txt = (
        "DISCLOSURE   Personal research artefact. NOT investment advice, NOT an offer to "
        "subscribe to any product, NOT affiliated with any regulated fund. Returns shown "
        "include backtest results — past simulated performance is not indicative of future "
        "returns. Walk-forward parameter selection (annual refit on prior-period data only); "
        "transaction costs of 2-5 bps per unit weight change per sleeve; no leverage; signals "
        "lagged one trading day. Source code: github.com/phuazz/breadth-thrust-etf."
    )
    ax.text(0.02, 0.85, txt, fontsize=5.5, color=INK_SOFT,
            ha="left", va="top", wrap=True, transform=ax.transAxes,
            linespacing=1.4)
    ax.text(0.98, 0.12, f"Generated {computed_at}",
            fontsize=5.5, color=INK_FAINT, ha="right", va="center",
            transform=ax.transAxes)


# ----------------- Main -----------------------------------------------------

def build(out_path: Path):
    multi, overlay, sleeves = load_all()
    deployed_key, blend = get_deployed(multi, overlay)
    deployed_series = pd.Series(blend["equity"], index=pd.to_datetime(blend["dates"]))

    # SPY for benchmark
    spy_series = None
    spy_cache = DATA_DIR / "asset_class_prices_cache.parquet"
    if spy_cache.exists():
        try:
            spy_df = pd.read_parquet(spy_cache)
            if "SPY" in spy_df.columns:
                spy_series = spy_df["SPY"].dropna()
        except Exception:
            pass

    asof_date = deployed_series.index[-1].strftime("%A %d %B %Y")
    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    current_breadth = (overlay or {}).get("current_breadth", 0) * 100
    p22_active = (overlay and (overlay.get("phase22_eem_tilt", {})
                                .get("current_state") == "EM_TILT_ON"))

    # A4 portrait: 8.27 x 11.69 inches
    fig = plt.figure(figsize=(8.27, 11.69), facecolor="white")
    gs = gridspec.GridSpec(
        nrows=8, ncols=2,
        height_ratios=[0.65, 0.65, 0.85, 1.7, 1.3, 1.6, 1.3, 0.6],
        width_ratios=[1.05, 0.95],
        hspace=0.42, wspace=0.30,
        left=0.06, right=0.96, top=0.97, bottom=0.04,
    )

    # Row 1: header
    render_header(fig.add_subplot(gs[0, :]), asof_date,
                   deployed_key, current_breadth)
    # Row 2: live regime + EEM + blend
    render_regime_state(fig.add_subplot(gs[1, :]), overlay)
    # Row 3: returns table
    render_returns_table(fig.add_subplot(gs[2, :]),
                          deployed_series, spy_series)
    # Row 4: cumulative chart
    render_cumulative_chart(fig.add_subplot(gs[3, :]),
                              deployed_series, spy_series, overlay)
    # Row 5: sleeve YTD + (right) what to watch
    render_sleeve_ytd(fig.add_subplot(gs[4, 0]),
                       sleeves, deployed_series, spy_series, p22_active)
    render_what_to_watch(fig.add_subplot(gs[4, 1]), overlay)
    # Row 6: top holdings
    render_top_holdings(fig.add_subplot(gs[5, :]), sleeves, p22_active)
    # Row 7: this week's activity
    render_recent_activity(fig.add_subplot(gs[6, :]), sleeves)
    # Row 8: footer
    render_footer(fig.add_subplot(gs[7, :]), computed_at)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out_path) as pdf:
        pdf.savefig(fig, bbox_inches="tight", pad_inches=0.2)
        info = pdf.infodict()
        info["Title"] = "USD Multi-Strategy ETF Portfolio — Weekly Factsheet"
        info["Subject"] = f"Weekly research factsheet, as of {asof_date}"
        info["Keywords"] = "ETF rotation, multi-strategy, USD, breadth, momentum, weekly"
    plt.close(fig)

    print(f"Wrote {out_path.relative_to(ROOT)}")
    print(f"  Deployed key: {deployed_key}")
    print(f"  As of:        {asof_date}")
    print(f"  PDF size:     {out_path.stat().st_size:,} bytes")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DOCS_DIR / "factsheet_latest.pdf"))
    args = p.parse_args()
    return build(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
