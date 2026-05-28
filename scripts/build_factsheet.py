"""Weekly factsheet — 2-page A4 PDF for the weekly investor read.

Designed against world-class fund-letter conventions (BlackRock /
Vanguard / AQR weekly client communications). Two-page layout so each
section gets the space it needs:

  PAGE 1 — AT A GLANCE
    Header band:   title, base currency, as-of date
    Hero strip:    4 big numbers (week, YTD, since-inception, max DD)
    Multi-period:  1W / 1M / 3M / 6M / YTD / 1Y / 3Y / ITD vs SPY
    Equity chart:  cumulative return vs benchmark, RISK_OFF bands
    Drawdown:      underwater curve

  PAGE 2 — POSITIONING + WEEKLY ACTIVITY
    Live state:    Phase 19 regime + Phase 22 EEM tilt + current blend
    Holdings:      all current positions with target weights + signals
    Trades:        this week's ENTER / EXIT / RESIZE per sleeve
    Attribution:   sleeve YTD contribution
    Watchlist:     signal thresholds approaching
    Footer:        compact disclosure

Personal research artefact — no fund / manager branding.
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
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Rectangle, FancyBboxPatch
from matplotlib.backends.backend_pdf import PdfPages

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

# Refined palette — minimal accent use, lots of soft greys
INK = "#0f1217"
INK_SOFT = "#3a4148"
INK_FAINT = "#7c8590"
BG_PANEL = "#f7f8fa"
BG_HEADER = "#1a2333"
BORDER = "#e1e4e8"
BORDER_STRONG = "#c8ccd2"
ACCENT = "#1351b4"
GOOD = "#1d7a3a"
BAD = "#b3261e"
WARN = "#b76e00"
ZEBRA = "#fafbfc"

# Typography baseline
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Inter", "Helvetica", "Arial", "DejaVu Sans"]
plt.rcParams["pdf.fonttype"] = 42  # searchable TrueType
plt.rcParams["axes.unicode_minus"] = False


# ----- Data loading --------------------------------------------------------

def load_all():
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    overlay_path = DATA_DIR / "risk_overlay.json"
    overlay = json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path.exists() else None
    sleeves = {}
    for key, path in [("a", DATA_DIR / "topk_robustness.json"),
                       ("b", DATA_DIR / "asset_class_rotation.json"),
                       ("c", DATA_DIR / "thematic_rotation.json"),
                       ("d", DATA_DIR / "europe_rotation.json")]:
        if path.exists():
            sleeves[key] = json.loads(path.read_text(encoding="utf-8"))
    return multi, overlay, sleeves


def get_deployed(multi, overlay):
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


# ----- Math helpers --------------------------------------------------------

def window_ret(series, start, end=None):
    s = series.loc[start:end].dropna() if end else series.loc[start:].dropna()
    if len(s) < 2: return None
    return s.iloc[-1] / s.iloc[0] - 1


def window_stats(series, start=None, end=None):
    s = series.loc[start:end].dropna() if (start or end) else series.dropna()
    if len(s) < 5: return None
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


def fmt_pct(x, signed=True, dp=1):
    if x is None: return "—"
    sign = "+" if (signed and x >= 0) else ""
    return f"{sign}{x*100:.{dp}f}%"


def fmt_num(x, signed=True, dp=2):
    if x is None: return "—"
    sign = "+" if (signed and x >= 0) else ""
    return f"{sign}{x:.{dp}f}"


def colour_pn(x):
    if x is None or x == 0: return INK_SOFT
    return GOOD if x > 0 else BAD


# ----- Page elements -------------------------------------------------------

def render_page_header(fig, title, asof_str, page_n, page_total):
    """Dark band at top of each page with title + page number."""
    # Header strip uses figure-coordinate axes
    ax = fig.add_axes([0, 0.965, 1, 0.035])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_HEADER,
                            edgecolor="none", transform=ax.transAxes))
    ax.text(0.04, 0.55, title, fontsize=10, fontweight="600",
            color="white", va="center", transform=ax.transAxes)
    ax.text(0.96, 0.55, f"As of {asof_str}  ·  Page {page_n} of {page_total}",
            fontsize=8.5, color="#c8ccd2", va="center", ha="right",
            transform=ax.transAxes)


def render_page_footer(fig, computed_at):
    """Compliance footer at bottom of each page."""
    ax = fig.add_axes([0, 0, 1, 0.025])
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_PANEL,
                            edgecolor="none", transform=ax.transAxes))
    ax.plot([0, 1], [1, 1], color=BORDER, linewidth=0.4,
             transform=ax.transAxes)
    ax.text(0.04, 0.50,
            "Personal research artefact · NOT investment advice · "
            "Past simulated performance is not indicative of future returns · "
            "Walk-forward K refit only · Costs 2–9 bps per unit weight",
            fontsize=6, color=INK_FAINT, va="center",
            transform=ax.transAxes)
    ax.text(0.96, 0.50, f"Generated {computed_at}  ·  github.com/phuazz/breadth-thrust-etf",
            fontsize=6, color=INK_FAINT, ha="right", va="center",
            transform=ax.transAxes)


def render_hero_panel(ax, label, value, value_colour=INK, sub=None,
                       sub_colour=INK_FAINT):
    """Single hero stat panel — large number + small label."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    # Top label band (small caps)
    ax.text(0.5, 0.85, label, fontsize=7, color=INK_FAINT,
            fontweight="600",
            ha="center", va="center", transform=ax.transAxes)
    # Hero value (large)
    ax.text(0.5, 0.45, value, fontsize=24, color=value_colour,
            fontweight="700", ha="center", va="center",
            transform=ax.transAxes, family="sans-serif")
    if sub:
        ax.text(0.5, 0.12, sub, fontsize=8, color=sub_colour,
                ha="center", va="center", transform=ax.transAxes)
    # Right border separator (drawn only if NOT the last cell — handled by caller)


def render_returns_table(ax, deployed_series, spy_series):
    """Multi-period returns table — strategy vs SPY vs delta. Clean
    institutional layout: small-caps headers, zebra rows, right-aligned
    numbers in mono."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.97, "PERFORMANCE — TOTAL RETURN BY PERIOD",
            fontsize=9, color=INK, fontweight="700", va="top", transform=ax.transAxes)
    ax.text(0.0, 0.91, "Strategy returns vs SPY (US large-cap) benchmark, USD-denominated",
            fontsize=7.5, color=INK_FAINT, va="top",
            transform=ax.transAxes, fontstyle="italic")

    last_date = deployed_series.index[-1]
    windows = [
        ("Week to date",   last_date - pd.Timedelta(days=7)),
        ("Month to date",  last_date - pd.DateOffset(months=1)),
        ("3 months",       last_date - pd.DateOffset(months=3)),
        ("6 months",       last_date - pd.DateOffset(months=6)),
        ("Year to date",   pd.Timestamp(last_date.year, 1, 1)),
        ("1 year",         last_date - pd.DateOffset(years=1)),
        ("3 years",        last_date - pd.DateOffset(years=3)),
        ("Inception",      deployed_series.index[0]),
    ]

    # Layout: 4 columns
    col_x = [0.02, 0.42, 0.62, 0.82]  # period | strategy | spy | delta
    row_h = 0.085
    y_header = 0.82

    # Header row
    ax.text(col_x[0], y_header, "PERIOD",
            fontsize=7, color=INK_FAINT, fontweight="600", va="center", transform=ax.transAxes)
    for i, lbl in enumerate(["STRATEGY", "SPY", "Δ vs SPY"]):
        ax.text(col_x[i+1] + 0.16, y_header, lbl,
                fontsize=7, color=INK_FAINT, fontweight="600", va="center", ha="right",
                transform=ax.transAxes)

    # Header underline
    ax.plot([0.0, 1.0], [y_header - 0.045, y_header - 0.045],
             color=BORDER_STRONG, linewidth=0.8, transform=ax.transAxes)

    # Body rows
    y = y_header - row_h
    for i, (label, start) in enumerate(windows):
        strat = window_ret(deployed_series, start)
        spy = window_ret(spy_series, start) if spy_series is not None else None
        delta = (strat - spy) if (strat is not None and spy is not None) else None
        # Zebra band
        if i % 2 == 1:
            ax.add_patch(Rectangle((0.0, y - 0.035), 1.0, row_h * 0.95,
                                     facecolor=ZEBRA, edgecolor="none",
                                     transform=ax.transAxes, zorder=0))
        ax.text(col_x[0], y, label,
                fontsize=8.5, color=INK, fontweight="500",
                va="center", transform=ax.transAxes)
        ax.text(col_x[1] + 0.16, y, fmt_pct(strat),
                fontsize=9, color=colour_pn(strat), fontweight="700",
                va="center", ha="right", transform=ax.transAxes,
                family="monospace")
        ax.text(col_x[2] + 0.16, y, fmt_pct(spy),
                fontsize=9, color=colour_pn(spy),
                va="center", ha="right", transform=ax.transAxes,
                family="monospace")
        ax.text(col_x[3] + 0.16, y, fmt_pct(delta),
                fontsize=9, color=colour_pn(delta), fontweight="600",
                va="center", ha="right", transform=ax.transAxes,
                family="monospace")
        y -= row_h


def render_equity_chart(ax, deployed_series, spy_series, overlay):
    """Cumulative return chart — primary visual of the page."""
    s = deployed_series / deployed_series.iloc[0]
    ax.plot(s.index, (s - 1) * 100, color=ACCENT, linewidth=1.8,
            label="Strategy", zorder=3)
    if spy_series is not None:
        spy = spy_series.reindex(s.index, method="ffill").dropna()
        if len(spy) > 5:
            spy = spy / spy.iloc[0]
            ax.plot(spy.index, (spy - 1) * 100, color=INK_FAINT,
                     linewidth=1.0, linestyle=(0, (4, 3)),
                     label="SPY benchmark", zorder=2)
    # RISK_OFF bands
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

    ax.set_title("Cumulative return since inception (USD)",
                  fontsize=9, fontweight="600", color=INK, loc="left",
                  pad=10)
    ax.tick_params(labelsize=8, colors=INK_SOFT)
    ax.grid(True, color=BORDER, linewidth=0.5, axis="y", alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color(BORDER_STRONG)
    ax.spines["left"].set_linewidth(0.6)
    ax.spines["bottom"].set_linewidth(0.6)
    # Y-axis as percentage with % suffix
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.legend(loc="upper left", fontsize=8, frameon=False,
              handlelength=2.0)


def render_drawdown_chart(ax, deployed_series, spy_series):
    """Drawdown underwater curve."""
    s = deployed_series / deployed_series.iloc[0]
    dd = ((s - s.cummax()) / s.cummax()) * 100
    ax.fill_between(dd.index, dd.values, 0,
                      color=BAD, alpha=0.18, linewidth=0)
    ax.plot(dd.index, dd.values, color=BAD, linewidth=1.2,
             label="Strategy DD")
    if spy_series is not None:
        spy = spy_series.reindex(s.index, method="ffill").dropna()
        if len(spy) > 5:
            spy = spy / spy.iloc[0]
            spy_dd = ((spy - spy.cummax()) / spy.cummax()) * 100
            ax.plot(spy_dd.index, spy_dd.values, color=INK_FAINT,
                     linewidth=0.9, linestyle=(0, (4, 3)),
                     label="SPY DD")
    ax.set_title("Drawdown from peak",
                  fontsize=9, fontweight="600", color=INK, loc="left",
                  pad=10)
    ax.tick_params(labelsize=8, colors=INK_SOFT)
    ax.grid(True, color=BORDER, linewidth=0.5, axis="y", alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color(BORDER_STRONG)
    ax.spines["left"].set_linewidth(0.6); ax.spines["bottom"].set_linewidth(0.6)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.legend(loc="lower left", fontsize=8, frameon=False, handlelength=2.0)


# ----- Page 2 panels -------------------------------------------------------

def render_regime_panel(ax, overlay):
    """Live state of both overlays + current live blend."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.94, "PORTFOLIO STATE — TODAY",
            fontsize=9, color=INK, fontweight="700", va="top", transform=ax.transAxes)

    today = datetime.now(timezone.utc).date()
    state_19 = (overlay or {}).get("current_state", "UNKNOWN")
    since_19 = (overlay or {}).get("current_state_since")
    days_19 = ((today - datetime.fromisoformat(since_19).date()).days
                if since_19 else 0)
    risk_on = state_19 == "RISK_ON"

    p22 = (overlay or {}).get("phase22_eem_tilt", {})
    p22_on = p22.get("enabled") and p22.get("current_state") == "EM_TILT_ON"
    since_22 = p22.get("current_state_since")
    days_22 = ((today - datetime.fromisoformat(since_22).date()).days
                if since_22 else 0)
    if p22_on:
        tilt = int(p22.get("parameters", {}).get("tilt_weight", 0.10) * 100)
        blend = f"35% A · {35-tilt}% B · 10% C · 20% D · {tilt}% EEM"
    else:
        blend = "35% A · 35% B · 10% C · 20% D"

    # Three info cards on one row
    cards = [
        ("BREADTH REGIME", state_19,
         GOOD if risk_on else BAD,
         f"Active since {since_19} ({days_19}d)" if since_19 else "—"),
        ("EM TILT STATE", p22.get("current_state", "—") if p22 else "—",
         WARN if p22_on else INK_FAINT,
         f"Active since {since_22} ({days_22}d)" if since_22 else
         ("Armed — awaiting golden cross" if p22 and p22.get("enabled") else "—")),
        ("LIVE BLEND TODAY", blend, ACCENT, "Target allocation"),
    ]
    card_w = (1.0 - 0.04) / 3
    for i, (label, value, col, sub) in enumerate(cards):
        x0 = i * (card_w + 0.02)
        # Card background
        ax.add_patch(Rectangle((x0, 0.10), card_w, 0.76,
                                facecolor=BG_PANEL, edgecolor=BORDER,
                                linewidth=0.6, transform=ax.transAxes))
        ax.text(x0 + 0.015, 0.74, label,
                fontsize=7, color=INK_FAINT, fontweight="600", va="center", transform=ax.transAxes)
        ax.text(x0 + 0.015, 0.50, value,
                fontsize=12 if i < 2 else 9, color=col,
                fontweight="700",
                va="center", transform=ax.transAxes)
        ax.text(x0 + 0.015, 0.22, sub,
                fontsize=7.5, color=INK_SOFT, va="center",
                transform=ax.transAxes)


def render_holdings_table(ax, sleeves, p22_active):
    """Current target portfolio — all positions sorted by effective weight."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.98, "CURRENT TARGET PORTFOLIO",
            fontsize=9, color=INK, fontweight="700", va="top", transform=ax.transAxes)
    ax.text(0.0, 0.94, "What to own today, sorted by effective weight",
            fontsize=7.5, color=INK_FAINT, va="top",
            transform=ax.transAxes, fontstyle="italic")

    # Build deployed weights
    sleeve_weights = {
        "a": 0.35, "b": 0.25 if p22_active else 0.35,
        "c": 0.10, "d": 0.20,
    }
    sleeve_letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    holdings = []
    for key, sleeve_wt in sleeve_weights.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if not trades: continue
        latest = trades[-1]
        for h in latest.get("holdings", []):
            eff = h.get("weight", 0) * sleeve_wt
            holdings.append({
                "etf": h.get("etf"),
                "sleeve": sleeve_letter[key],
                "within": h.get("weight", 0),
                "effective": eff,
                "signal": h.get("signal_pct") or h.get("breadth_pct"),
            })
    if p22_active:
        holdings.append({"etf": "EEM", "sleeve": "TILT",
                         "within": 1.0, "effective": 0.10,
                         "signal": None})
    holdings = sorted(holdings, key=lambda x: -x["effective"])

    # Layout
    col_x = [0.02, 0.18, 0.30, 0.50, 0.80]
    col_headers = ["TICKER", "SLEEVE", "SIGNAL", "TARGET WT",
                   "% of $100k"]
    y_header = 0.86
    ax.plot([0.0, 1.0], [y_header - 0.020, y_header - 0.020],
             color=BORDER_STRONG, linewidth=0.8, transform=ax.transAxes)
    for x, h in zip(col_x, col_headers):
        align = "right" if h in ("TARGET WT", "% of $100k", "SIGNAL") else "left"
        x_pos = x + (0.16 if align == "right" else 0)
        ax.text(x_pos, y_header, h,
                fontsize=7, color=INK_FAINT, fontweight="600", va="center", ha=align,
                transform=ax.transAxes)

    row_h = 0.052
    y = y_header - 0.045
    for i, h in enumerate(holdings):
        if i >= 14: break
        if i % 2 == 1:
            ax.add_patch(Rectangle((0.0, y - 0.020), 1.0, row_h * 0.95,
                                     facecolor=ZEBRA, edgecolor="none",
                                     transform=ax.transAxes, zorder=0))
        ax.text(col_x[0], y, h["etf"],
                fontsize=9, color=INK, fontweight="700",
                family="monospace", va="center",
                transform=ax.transAxes)
        ax.text(col_x[1], y, h["sleeve"],
                fontsize=8, color=INK_SOFT, va="center",
                transform=ax.transAxes)
        sig_str = (f"{h['signal']:+.1f}%" if h["signal"] is not None
                    else "—")
        ax.text(col_x[2] + 0.16, y, sig_str,
                fontsize=8, color=INK_SOFT, va="center", ha="right",
                family="monospace", transform=ax.transAxes)
        ax.text(col_x[3] + 0.16, y, fmt_pct(h["effective"], signed=False, dp=1),
                fontsize=9, color=ACCENT, fontweight="700",
                va="center", ha="right", family="monospace",
                transform=ax.transAxes)
        cash = h["effective"] * 100_000
        ax.text(col_x[4] + 0.16, y, f"${cash:,.0f}",
                fontsize=8.5, color=INK, va="center", ha="right",
                family="monospace", transform=ax.transAxes)
        y -= row_h


def render_trades_panel(ax, sleeves):
    """This week's rebalance activity — ENTER / EXIT / RESIZE per sleeve."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.96, "ACTIVITY THIS WEEK",
            fontsize=9, color=INK, fontweight="700", va="top", transform=ax.transAxes)
    ax.text(0.0, 0.90, "Position changes from the previous rebalance",
            fontsize=7.5, color=INK_FAINT, va="top",
            transform=ax.transAxes, fontstyle="italic")

    sleeve_letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    rows = []
    for key, sleeve in sleeve_letter.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if len(trades) < 2: continue
        prev_h = {h["etf"]: h["weight"] for h in trades[-2]["holdings"]}
        curr_h = {h["etf"]: h["weight"] for h in trades[-1]["holdings"]}
        for etf in curr_h:
            if etf not in prev_h:
                rows.append((sleeve, "ENTER", etf, None, curr_h[etf]))
        for etf in prev_h:
            if etf not in curr_h:
                rows.append((sleeve, "EXIT", etf, prev_h[etf], None))
        for etf in curr_h:
            if etf in prev_h:
                d = curr_h[etf] - prev_h[etf]
                if abs(d) > 0.01:
                    rows.append((sleeve, "RESIZE", etf, prev_h[etf], curr_h[etf]))

    y_header = 0.78
    if not rows:
        ax.text(0.0, 0.62,
                "No position changes this week — strategy stable.",
                fontsize=9, color=INK_SOFT,
                fontstyle="italic", va="top", transform=ax.transAxes)
        return

    col_x = [0.02, 0.12, 0.25, 0.45, 0.62, 0.80]
    headers = ["SLEEVE", "ACTION", "TICKER", "PRIOR WT", "NEW WT", "Δ"]
    ax.plot([0.0, 1.0], [y_header - 0.020, y_header - 0.020],
             color=BORDER_STRONG, linewidth=0.8, transform=ax.transAxes)
    for i, (x, h) in enumerate(zip(col_x, headers)):
        align = "right" if h in ("PRIOR WT", "NEW WT", "Δ") else "left"
        x_pos = x + (0.13 if align == "right" else 0)
        ax.text(x_pos, y_header, h,
                fontsize=7, color=INK_FAINT, fontweight="600", va="center", ha=align,
                transform=ax.transAxes)

    row_h = 0.058
    y = y_header - 0.045
    for i, (sleeve, action, etf, prev_w, new_w) in enumerate(rows[:10]):
        if i % 2 == 1:
            ax.add_patch(Rectangle((0.0, y - 0.022), 1.0, row_h * 0.92,
                                     facecolor=ZEBRA, edgecolor="none",
                                     transform=ax.transAxes, zorder=0))
        ax.text(col_x[0], y, sleeve, fontsize=8.5, color=INK_SOFT,
                va="center", transform=ax.transAxes)
        action_col = (GOOD if action == "ENTER" else BAD if action == "EXIT"
                       else WARN)
        ax.text(col_x[1], y, action, fontsize=7.5, color=action_col,
                fontweight="700",
                va="center", transform=ax.transAxes)
        ax.text(col_x[2], y, etf, fontsize=9, color=INK, fontweight="700",
                family="monospace", va="center", transform=ax.transAxes)
        ax.text(col_x[3] + 0.13, y,
                fmt_pct(prev_w, signed=False) if prev_w is not None else "—",
                fontsize=8.5, color=INK_SOFT, va="center", ha="right",
                family="monospace", transform=ax.transAxes)
        ax.text(col_x[4] + 0.13, y,
                fmt_pct(new_w, signed=False) if new_w is not None else "—",
                fontsize=8.5, color=INK_SOFT, va="center", ha="right",
                family="monospace", transform=ax.transAxes)
        d = (new_w or 0) - (prev_w or 0)
        ax.text(col_x[5] + 0.13, y, fmt_pct(d, dp=1),
                fontsize=8.5, color=colour_pn(d), fontweight="600",
                va="center", ha="right", family="monospace",
                transform=ax.transAxes)
        y -= row_h


def render_sleeve_attribution(ax, sleeves, deployed_series, p22_active):
    """Per-sleeve YTD return × weight = pp contribution to blend."""
    ax.set_title("SLEEVE CONTRIBUTION TO YTD RETURN",
                  fontsize=9, color=INK, fontweight="700",
                  loc="left", pad=8)
    last_date = deployed_series.index[-1]
    ytd_start = pd.Timestamp(last_date.year, 1, 1)
    sleeve_meta = [
        ("a", "US Sectors (A)",    ACCENT,    0.35),
        ("b", "Asset Class (B)",   GOOD,      0.25 if p22_active else 0.35),
        ("c", "Thematic (C)",      "#dc2626", 0.10),
        ("d", "Europe (D)",        "#0e7490", 0.20),
    ]
    names, returns, contribs, weights, colours = [], [], [], [], []
    for key, label, col, wt in sleeve_meta:
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
    if p22_active:
        names.append("EEM Tilt")
        returns.append(0)  # we don't have EEM return separately
        weights.append(0.10)
        contribs.append(0)
        colours.append(WARN)

    y_pos = np.arange(len(names))
    bars = ax.barh(y_pos, [c * 100 for c in contribs], color=colours,
                     edgecolor="white", linewidth=1, height=0.55)
    for i, (r, w_, c) in enumerate(zip(returns, weights, contribs)):
        x = c * 100
        offset = 0.4 if x >= 0 else -0.4
        ha = "left" if x >= 0 else "right"
        ax.text(x + offset, i,
                f"  {fmt_pct(r)} sleeve × {w_*100:.0f}% wt = {c*100:+.1f}pp",
                fontsize=7.5, color=INK_SOFT,
                va="center", ha=ha)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=8.5, color=INK)
    ax.invert_yaxis()
    ax.axvline(0, color=INK_FAINT, linewidth=0.6)
    ax.tick_params(axis="x", labelsize=7.5, colors=INK_FAINT)
    ax.grid(True, color=BORDER, linewidth=0.4, axis="x", alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color(BORDER)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:+.0f}pp"))


def render_watchlist(ax, overlay):
    """What thresholds are approaching — actionable forward-looking."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.0, 0.96, "WATCHLIST — APPROACHING THRESHOLDS",
            fontsize=9, color=INK, fontweight="700", va="top", transform=ax.transAxes)
    ax.text(0.0, 0.88, "Signal levels relative to next regime change",
            fontsize=7.5, color=INK_FAINT, va="top",
            transform=ax.transAxes, fontstyle="italic")

    items = []
    if overlay:
        cur_breadth = overlay.get("current_breadth", 0) * 100
        off_thresh = overlay.get("gate_parameters", {}).get("off_threshold", 0.20) * 100
        on_thresh = overlay.get("gate_parameters", {}).get("on_threshold", 0.50) * 100
        state = overlay.get("current_state")
        if state == "RISK_ON":
            margin = cur_breadth - off_thresh
            items.append({
                "label": "S&P 500 breadth",
                "value": f"{cur_breadth:.0f}%",
                "trigger": f"de-risk if < {off_thresh:.0f}%",
                "margin": f"+{margin:.0f}pp buffer",
                "status": "ARMED" if margin > 10 else "NEAR",
                "status_col": GOOD if margin > 10 else WARN,
            })
        else:
            margin = on_thresh - cur_breadth
            items.append({
                "label": "S&P 500 breadth",
                "value": f"{cur_breadth:.0f}%",
                "trigger": f"re-engage if > {on_thresh:.0f}%",
                "margin": f"needs +{margin:.0f}pp",
                "status": "DE-RISKED",
                "status_col": BAD,
            })

        p22 = overlay.get("phase22_eem_tilt", {})
        if p22.get("enabled"):
            fast = p22.get("current_fast_ma", 0)
            slow = p22.get("current_slow_ma", 0)
            spread_pct = ((fast - slow) / slow * 100) if slow else 0
            if p22.get("current_state") == "EM_TILT_ON":
                items.append({
                    "label": "EEM/SPY 50d vs 200d MA",
                    "value": f"+{spread_pct:.1f}%",
                    "trigger": "deactivate if 50d < 200d",
                    "margin": f"+{spread_pct:.1f}pp above cross",
                    "status": "TILT ON",
                    "status_col": WARN,
                })
            else:
                items.append({
                    "label": "EEM/SPY 50d vs 200d MA",
                    "value": f"{spread_pct:+.1f}%",
                    "trigger": "activate on golden cross",
                    "margin": f"needs +{abs(spread_pct):.1f}pp",
                    "status": "ARMED",
                    "status_col": INK_SOFT,
                })

    y = 0.78
    row_h = 0.18
    for it in items[:2]:
        # Indicator card
        ax.add_patch(Rectangle((0.0, y - row_h + 0.02), 1.0, row_h - 0.02,
                                facecolor=BG_PANEL, edgecolor=BORDER,
                                linewidth=0.6, transform=ax.transAxes))
        ax.text(0.02, y - 0.02, it["label"],
                fontsize=8, color=INK_FAINT, fontweight="600", va="top", transform=ax.transAxes)
        ax.text(0.02, y - 0.075, it["value"],
                fontsize=15, color=INK, fontweight="700",
                family="monospace", va="top", transform=ax.transAxes)
        ax.text(0.02, y - 0.13, it["trigger"],
                fontsize=7, color=INK_FAINT, va="top",
                transform=ax.transAxes, fontstyle="italic")
        ax.text(0.98, y - 0.02, it["status"],
                fontsize=8, color=it["status_col"], fontweight="700", ha="right", va="top",
                transform=ax.transAxes)
        ax.text(0.98, y - 0.075, it["margin"],
                fontsize=9, color=INK_SOFT, ha="right", va="top",
                family="monospace", transform=ax.transAxes)
        y -= row_h


# ----- Main build ----------------------------------------------------------

def build(out_path: Path):
    multi, overlay, sleeves = load_all()
    deployed_key, blend = get_deployed(multi, overlay)
    deployed_series = pd.Series(blend["equity"],
                                  index=pd.to_datetime(blend["dates"]))

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

    asof_date = deployed_series.index[-1]
    asof_str = asof_date.strftime("%d %B %Y")
    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p22_active = (overlay and (overlay.get("phase22_eem_tilt", {})
                                .get("current_state") == "EM_TILT_ON"))
    full_stats = window_stats(deployed_series)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out_path) as pdf:
        # ============ PAGE 1 — AT A GLANCE ============
        fig = plt.figure(figsize=(8.27, 11.69), facecolor="white")
        title = "USD MULTI-STRATEGY ETF PORTFOLIO  ·  WEEKLY FACTSHEET"
        render_page_header(fig, title, asof_str, 1, 2)

        # Layout: hero strip, returns table, equity chart, drawdown chart
        gs = gridspec.GridSpec(
            nrows=4, ncols=1,
            height_ratios=[1.1, 2.6, 2.8, 1.7],
            hspace=0.45,
            left=0.06, right=0.94, top=0.93, bottom=0.045,
        )

        # Hero strip — 4 cells in one row
        hero_outer = fig.add_subplot(gs[0])
        hero_outer.set_xlim(0, 1); hero_outer.set_ylim(0, 1); hero_outer.axis("off")
        # Outer border + zebra panel background
        hero_outer.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_PANEL,
                                         edgecolor=BORDER, linewidth=0.8,
                                         transform=hero_outer.transAxes))
        last_date = deployed_series.index[-1]
        wk_ret = window_ret(deployed_series, last_date - pd.Timedelta(days=7))
        ytd_ret = window_ret(deployed_series, pd.Timestamp(last_date.year, 1, 1))
        hero_cells = [
            ("THIS WEEK", fmt_pct(wk_ret), colour_pn(wk_ret),
             "Latest 7-day return"),
            ("YEAR TO DATE", fmt_pct(ytd_ret), colour_pn(ytd_ret),
             f"Since 1 Jan {last_date.year}"),
            ("SINCE INCEPTION", fmt_pct(full_stats["total"]) if full_stats else "—",
             GOOD if (full_stats and full_stats["total"] > 0) else BAD,
             f"Backtest from {deployed_series.index[0].strftime('%b %Y')}"),
            ("MAX DRAWDOWN", fmt_pct(full_stats["dd"]) if full_stats else "—",
             BAD, "Worst peak-to-trough"),
        ]
        cell_w = 1.0 / 4
        for i, (label, value, col, sub) in enumerate(hero_cells):
            x0 = i * cell_w
            if i > 0:
                hero_outer.plot([x0, x0], [0.15, 0.85],
                                 color=BORDER, linewidth=0.7,
                                 transform=hero_outer.transAxes)
            hero_outer.text(x0 + cell_w/2, 0.72, label,
                             fontsize=7, color=INK_FAINT, fontweight="600",
                             ha="center", va="center",
                             transform=hero_outer.transAxes)
            hero_outer.text(x0 + cell_w/2, 0.42, value,
                             fontsize=22, color=col, fontweight="700",
                             ha="center", va="center",
                             transform=hero_outer.transAxes)
            hero_outer.text(x0 + cell_w/2, 0.16, sub,
                             fontsize=7, color=INK_FAINT,
                             ha="center", va="center",
                             transform=hero_outer.transAxes)

        # Returns table
        render_returns_table(fig.add_subplot(gs[1]),
                              deployed_series, spy_series)
        # Equity chart
        render_equity_chart(fig.add_subplot(gs[2]),
                              deployed_series, spy_series, overlay)
        # Drawdown chart
        render_drawdown_chart(fig.add_subplot(gs[3]),
                                deployed_series, spy_series)

        render_page_footer(fig, computed_at)
        pdf.savefig(fig, bbox_inches=None, pad_inches=0)
        plt.close(fig)

        # ============ PAGE 2 — POSITIONING + ACTIVITY ============
        fig = plt.figure(figsize=(8.27, 11.69), facecolor="white")
        render_page_header(fig, title, asof_str, 2, 2)

        gs = gridspec.GridSpec(
            nrows=4, ncols=1,
            height_ratios=[1.0, 3.2, 2.2, 1.7],
            hspace=0.50,
            left=0.06, right=0.94, top=0.93, bottom=0.045,
        )
        # Row 1: Live state cards
        render_regime_panel(fig.add_subplot(gs[0]), overlay)
        # Row 2: Holdings table
        render_holdings_table(fig.add_subplot(gs[1]), sleeves, p22_active)
        # Row 3: trades + watchlist split horizontally
        bottom_split = gridspec.GridSpecFromSubplotSpec(
            1, 2, subplot_spec=gs[2], wspace=0.10)
        render_trades_panel(fig.add_subplot(bottom_split[0]), sleeves)
        render_watchlist(fig.add_subplot(bottom_split[1]), overlay)
        # Row 4: sleeve attribution
        render_sleeve_attribution(fig.add_subplot(gs[3]),
                                     sleeves, deployed_series, p22_active)

        render_page_footer(fig, computed_at)
        pdf.savefig(fig, bbox_inches=None, pad_inches=0)
        plt.close(fig)

        info = pdf.infodict()
        info["Title"] = "USD Multi-Strategy ETF Portfolio — Weekly Factsheet"
        info["Subject"] = f"Weekly factsheet, as of {asof_str}"
        info["Keywords"] = "ETF rotation, multi-strategy, USD, breadth, momentum, weekly"

    print(f"Wrote {out_path.relative_to(ROOT)}")
    print(f"  Deployed key: {deployed_key}")
    print(f"  As of:        {asof_str}")
    print(f"  PDF size:     {out_path.stat().st_size:,} bytes")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DOCS_DIR / "factsheet_latest.pdf"))
    args = p.parse_args()
    return build(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
