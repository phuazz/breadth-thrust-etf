"""B28 — Auto-generated one-page monthly factsheet PDF.

Reads existing pipeline outputs (multi_strategy.json, risk_overlay.json,
breadth_csp1.json) and produces a one-page A4 factsheet at
docs/factsheet_latest.pdf for IM distribution / AI prospect diligence.

Layout:
  Row 1 — header band (strategy name, manager, as-of, currency, inception)
  Row 2 — KEY STATISTICS strip (Sharpe, CAGR, Total Return, Max DD,
          Vol, Best/Worst year)
  Row 3 — cumulative return chart (strategy vs SPY benchmark)
  Row 4 — split:
            Left:  sleeve allocation pie + current live composition
            Right: calendar-year returns bar chart
  Row 5 — top current holdings table (up to 10 names with weights)
  Row 6 — strategy description (2-3 sentences)
  Footer — compliance block

No new dependencies — matplotlib only (already a project dep).

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
from matplotlib.patches import Rectangle, FancyBboxPatch
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

# Standard fonts
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Inter", "Arial", "Helvetica", "DejaVu Sans"]
plt.rcParams["pdf.fonttype"] = 42  # TrueType so text is searchable in PDF


def load_data():
    multi = json.loads((DATA_DIR / "multi_strategy.json").read_text(encoding="utf-8"))
    overlay_path = DATA_DIR / "risk_overlay.json"
    overlay = json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path.exists() else None
    return multi, overlay


def get_deployed_blend(multi, overlay):
    """Resolve the deployed key with the same precedence as the dashboard.

    The gated variants live in risk_overlay.json (the pipeline merges them
    into multi.strategies at injection time, but this standalone script
    reads the raw files). Check overlay.gated_variants first, then fall
    back to multi.strategies for the ungated baseline.
    """
    overlay_variants = (overlay or {}).get("gated_variants", {})
    strategies = multi.get("strategies", {})
    for key in ("blend_35_35_10_20_gated_eem_tilted",
                 "blend_35_35_10_20_gated"):
        if key in overlay_variants:
            return key, overlay_variants[key]
    for key in ("blend_35_35_10_20", "blend_45_45_10"):
        if key in strategies:
            return key, strategies[key]
    raise RuntimeError("No deployed blend found in multi_strategy.json or risk_overlay.json")


def compute_window_stats(dates, equity, start=None, end=None):
    s = pd.Series(equity, index=pd.to_datetime(dates))
    if start or end:
        s = s.loc[start:end]
    s = s.dropna()
    if len(s) < 5:
        return None
    s = s / s.iloc[0]
    d = s.pct_change().fillna(0)
    n_years = (s.index[-1] - s.index[0]).days / 365.25
    cagr = s.iloc[-1] ** (1 / n_years) - 1 if n_years > 0 else 0
    sharpe = d.mean() / d.std() * math.sqrt(252) if d.std() > 0 else 0
    vol = d.std() * math.sqrt(252)
    dd = ((s - s.cummax()) / s.cummax()).min()
    return {
        "sharpe": sharpe, "cagr": cagr, "total": s.iloc[-1] - 1,
        "vol": vol, "dd": dd, "n_years": n_years,
    }


def compute_calendar_year_returns(dates, equity):
    s = pd.Series(equity, index=pd.to_datetime(dates))
    monthly = s.resample("ME").last()
    out = {}
    for year in sorted(set(monthly.index.year)):
        sub = monthly[monthly.index.year == year]
        if len(sub) < 2:
            continue
        out[year] = sub.iloc[-1] / sub.iloc[0] - 1
    return out


# ----------------- Section renderers ----------------------------------------

def render_header(ax, multi, blend, asof_date):
    """Top header band — title, manager, currency, inception."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    # Background band
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_SOFT,
                            edgecolor="none", transform=ax.transAxes))
    # Title
    ax.text(0.02, 0.72, "USD Multi-Strategy ETF Portfolio",
            fontsize=16, fontweight="bold", color=INK, va="center",
            transform=ax.transAxes)
    ax.text(0.02, 0.35,
            "Rules-based 4-sleeve ETF rotation · USD-denominated · Long-only · Weekly rebalance · No leverage",
            fontsize=8.5, color=INK_SOFT, va="center",
            transform=ax.transAxes)
    # Right side — manager + as-of
    ax.text(0.98, 0.78, "Navigo Investment Management Pte. Ltd.",
            fontsize=10, fontweight="bold", color=INK, va="center",
            ha="right", transform=ax.transAxes)
    ax.text(0.98, 0.50, "Singapore · Research factsheet",
            fontsize=8, color=INK_FAINT, va="center",
            ha="right", transform=ax.transAxes)
    ax.text(0.98, 0.22, f"As of {asof_date}",
            fontsize=8, color=INK_FAINT, va="center",
            ha="right", transform=ax.transAxes)


def render_key_stats(ax, multi, blend, overlay):
    """6-number stat strip."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    full_stats = compute_window_stats(blend["dates"], blend["equity"])
    if not full_stats:
        return
    # Compute YTD and 1y returns for the strip
    s = pd.Series(blend["equity"], index=pd.to_datetime(blend["dates"]))
    last_date = s.index[-1]
    ytd_start = pd.Timestamp(last_date.year, 1, 1)
    ytd = (s.loc[ytd_start:].iloc[-1] / s.loc[ytd_start:].iloc[0] - 1
           if len(s.loc[ytd_start:]) > 1 else None)
    one_yr_start = last_date - pd.DateOffset(years=1)
    one_yr_window = s.loc[one_yr_start:]
    one_yr = (one_yr_window.iloc[-1] / one_yr_window.iloc[0] - 1
              if len(one_yr_window) > 1 else None)

    def fmt_pct(x): return f"{x*100:+.1f}%" if x is not None else "—"
    def fmt_num(x): return f"{x:+.2f}" if x is not None else "—"

    stats = [
        ("Sharpe (since inception)", fmt_num(full_stats["sharpe"]), GOOD),
        ("CAGR (since inception)",   fmt_pct(full_stats["cagr"]),   GOOD if full_stats["cagr"]>0 else BAD),
        ("Total return",              fmt_pct(full_stats["total"]),  GOOD if full_stats["total"]>0 else BAD),
        ("Max drawdown",              fmt_pct(full_stats["dd"]),     BAD),
        ("YTD",                       fmt_pct(ytd),                  GOOD if (ytd or 0)>0 else BAD),
        ("1-year",                    fmt_pct(one_yr),               GOOD if (one_yr or 0)>0 else BAD),
    ]
    n = len(stats)
    cell_w = 1.0 / n
    for i, (label, val, colour) in enumerate(stats):
        x0 = i * cell_w
        # Cell border
        if i > 0:
            ax.plot([x0, x0], [0.1, 0.9], color=BORDER, linewidth=0.6,
                     transform=ax.transAxes)
        ax.text(x0 + cell_w / 2, 0.65, val,
                fontsize=15, fontweight="bold", color=colour,
                ha="center", va="center", transform=ax.transAxes)
        ax.text(x0 + cell_w / 2, 0.22, label,
                fontsize=7.5, color=INK_FAINT,
                ha="center", va="center", transform=ax.transAxes)


def render_cumulative_return(ax, blend, overlay=None):
    """Cumulative return chart, strategy + SPY benchmark if available."""
    s = pd.Series(blend["equity"], index=pd.to_datetime(blend["dates"]))
    s = s / s.iloc[0]
    ax.plot(s.index, (s - 1) * 100, color=ACCENT, linewidth=1.8,
            label="Deployed strategy", zorder=3)

    # Add SPY benchmark from asset_class cache if present
    spy_cache = DATA_DIR / "asset_class_prices_cache.parquet"
    if spy_cache.exists():
        try:
            df = pd.read_parquet(spy_cache)
            if "SPY" in df.columns:
                spy = df["SPY"].reindex(s.index, method="ffill").dropna()
                if len(spy) > 5:
                    spy = spy / spy.iloc[0]
                    ax.plot(spy.index, (spy - 1) * 100, color=INK_FAINT,
                             linewidth=1.0, linestyle="--",
                             label="SPY (US large-cap benchmark)", zorder=2)
        except Exception:
            pass

    # Shade Phase 19 RISK_OFF bands if available
    if overlay and overlay.get("events"):
        events = overlay["events"]
        state = "RISK_ON"
        off_start = None
        for ev in events:
            if ev["direction"] == "RISK_OFF":
                off_start = pd.to_datetime(ev["date"])
            elif ev["direction"] == "RISK_ON" and off_start is not None:
                off_end = pd.to_datetime(ev["date"])
                ax.axvspan(off_start, off_end, color="#b76e00",
                            alpha=0.12, zorder=1)
                off_start = None
        if off_start is not None:
            ax.axvspan(off_start, s.index[-1], color="#b76e00",
                        alpha=0.12, zorder=1)

    ax.set_title("Cumulative return (%) since inception",
                  fontsize=9.5, fontweight="bold", color=INK, loc="left")
    ax.set_ylabel("Return %", fontsize=8, color=INK_FAINT)
    ax.tick_params(labelsize=7.5, colors=INK_FAINT)
    ax.grid(True, color="#eef0f3", linewidth=0.6, axis="y")
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(BORDER)
    ax.spines["bottom"].set_color(BORDER)
    ax.legend(loc="upper left", fontsize=7.5, frameon=False)


def render_allocation_pie(ax, overlay):
    """Current live sleeve allocation pie."""
    ax.set_title("Live sleeve allocation",
                  fontsize=9, fontweight="bold", color=INK, loc="left")
    # Default 35/35/10/20; reduce B by tilt when tilt is ON
    tilt_on = (overlay and overlay.get("phase22_eem_tilt", {}).get("enabled")
               and overlay["phase22_eem_tilt"].get("current_state") == "EM_TILT_ON")
    if tilt_on:
        tilt = overlay["phase22_eem_tilt"]["parameters"].get("tilt_weight", 0.10)
        weights = [35, 35 - tilt*100, 10, 20, tilt*100]
        labels = ["A · US Sectors", f"B · Asset Class ({35-tilt*100:.0f}%)",
                  "C · Thematic", "D · Europe", f"EEM Tilt ({tilt*100:.0f}%)"]
        colours = [ACCENT, GOOD, "#dc2626", "#0e7490", "#b76e00"]
    else:
        weights = [35, 35, 10, 20]
        labels = ["A · US Sectors (35%)", "B · Asset Class (35%)",
                  "C · Thematic (10%)", "D · Europe (20%)"]
        colours = [ACCENT, GOOD, "#dc2626", "#0e7490"]
    wedges, texts = ax.pie(weights, colors=colours,
                              startangle=90, counterclock=False,
                              wedgeprops=dict(width=0.42, linewidth=1, edgecolor="white"))
    ax.legend(wedges, labels, loc="center left",
              bbox_to_anchor=(0.95, 0.5), fontsize=7,
              frameon=False)
    ax.text(0, 0, "100%\nNAV", ha="center", va="center",
            fontsize=9, fontweight="bold", color=INK_SOFT)


def render_calendar_returns(ax, blend):
    """Calendar-year returns bar chart."""
    ax.set_title("Calendar-year returns (%)",
                  fontsize=9, fontweight="bold", color=INK, loc="left")
    cal = compute_calendar_year_returns(blend["dates"], blend["equity"])
    if not cal:
        ax.text(0.5, 0.5, "Insufficient data", ha="center", va="center",
                transform=ax.transAxes, color=INK_FAINT)
        ax.axis("off"); return
    years = list(cal.keys())
    values = [cal[y] * 100 for y in years]
    colours = [GOOD if v > 0 else BAD for v in values]
    bars = ax.bar(years, values, color=colours, edgecolor="white", linewidth=0.6)
    for bar, v in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2,
                v + (1 if v > 0 else -2.5),
                f"{v:+.1f}%", ha="center",
                fontsize=6.5, color=INK_SOFT)
    ax.axhline(0, color=INK_FAINT, linewidth=0.6)
    ax.tick_params(labelsize=7.5, colors=INK_FAINT)
    ax.set_ylabel("Return %", fontsize=8, color=INK_FAINT)
    ax.grid(True, color="#eef0f3", linewidth=0.6, axis="y")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(BORDER)
    ax.spines["bottom"].set_color(BORDER)


def render_description(ax):
    """Strategy description box."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    txt = (
        "STRATEGY OBJECTIVE   Deliver equity-like returns with a materially shallower "
        "drawdown profile by rotating across four uncorrelated breadth/momentum sleeves: "
        "US sector breadth (Strategy A), broad asset-class momentum (B), thematic momentum (C), "
        "and Europe sector breadth (D). A CSP1-breadth regime gate halves equity exposure into SHY "
        "Treasury when the broad US market is structurally weak. A Phase 22 EEM/SPY relative-strength "
        "tilt allocates 10% from B to EEM during EM-favoured cycles."
    )
    ax.text(0.0, 0.9, txt, fontsize=7.5, color=INK_SOFT,
            ha="left", va="top", wrap=True, transform=ax.transAxes,
            linespacing=1.45)


def render_footer(ax, computed_at):
    """Compliance footer band."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor=BG_SOFT,
                            edgecolor="none", transform=ax.transAxes))
    txt = (
        "DISCLOSURE   This factsheet is a research artefact published by Navigo Investment "
        "Management Pte. Ltd. for Accredited Investor diligence. It is NOT an offer to subscribe "
        "to any fund product and does not constitute investment advice. Returns shown are backtest "
        "results — past simulated performance is not indicative of future returns. Walk-forward "
        "parameter selection (annual refit on prior-period data only); transaction costs of 2-5 bps "
        "per unit weight change applied per sleeve; no leverage; signals lagged one trading day. "
        "Live track record will be reported separately once the strategy is deployed in a regulated vehicle."
    )
    ax.text(0.02, 0.85, txt, fontsize=6.0, color=INK_SOFT,
            ha="left", va="top", wrap=True, transform=ax.transAxes,
            linespacing=1.4)
    ax.text(0.98, 0.10, f"Generated {computed_at}",
            fontsize=6.0, color=INK_FAINT, ha="right", va="center",
            transform=ax.transAxes)


# ----------------- Main -----------------------------------------------------

def build(out_path: Path):
    multi, overlay = load_data()
    deployed_key, blend = get_deployed_blend(multi, overlay)

    asof = pd.to_datetime(blend["dates"][-1]).strftime("%d %B %Y")
    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # A4 portrait: 8.27 x 11.69 inches
    fig = plt.figure(figsize=(8.27, 11.69), facecolor="white")
    gs = gridspec.GridSpec(
        nrows=7, ncols=2,
        height_ratios=[0.7, 0.65, 2.4, 1.8, 1.2, 0.7, 0.7],
        width_ratios=[1, 1],
        hspace=0.55, wspace=0.30,
        left=0.06, right=0.96, top=0.97, bottom=0.04,
    )

    # Row 1: header (full width)
    ax_header = fig.add_subplot(gs[0, :])
    render_header(ax_header, multi, blend, asof)

    # Row 2: key stats (full width)
    ax_stats = fig.add_subplot(gs[1, :])
    render_key_stats(ax_stats, multi, blend, overlay)

    # Row 3: cumulative return chart (full width)
    ax_perf = fig.add_subplot(gs[2, :])
    render_cumulative_return(ax_perf, blend, overlay)

    # Row 4: allocation pie + calendar returns
    ax_alloc = fig.add_subplot(gs[3, 0])
    render_allocation_pie(ax_alloc, overlay)
    ax_cal = fig.add_subplot(gs[3, 1])
    render_calendar_returns(ax_cal, blend)

    # Row 5: description (full width)
    ax_desc = fig.add_subplot(gs[4, :])
    render_description(ax_desc)

    # Row 6: spacer
    fig.add_subplot(gs[5, :]).axis("off")

    # Row 7: footer
    ax_footer = fig.add_subplot(gs[6, :])
    render_footer(ax_footer, computed_at)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out_path) as pdf:
        pdf.savefig(fig, bbox_inches="tight", pad_inches=0.2)
        info = pdf.infodict()
        info["Title"] = "USD Multi-Strategy ETF Portfolio — Factsheet"
        info["Author"] = "Navigo Investment Management Pte. Ltd."
        info["Subject"] = f"Research factsheet, as of {asof}"
        info["Keywords"] = "ETF rotation, multi-strategy, USD, breadth, momentum"
    plt.close(fig)

    print(f"Wrote {out_path.relative_to(ROOT)}")
    print(f"  Deployed key: {deployed_key}")
    print(f"  As of:        {asof}")
    print(f"  PDF size:     {out_path.stat().st_size:,} bytes")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DOCS_DIR / "factsheet_latest.pdf"))
    args = p.parse_args()
    return build(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
