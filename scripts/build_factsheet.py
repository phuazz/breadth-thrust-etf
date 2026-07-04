"""Weekly factsheet — 2-page A4 PDF for the weekly investor read.

Built with reportlab Platypus (proper PDF layout primitives — Tables,
Paragraphs, Spacers, Flowables) instead of matplotlib text-at-coordinate
hacks. Charts are still rendered with matplotlib (saved to PNG bytes
and embedded as Images) but ALL tabular/text layout uses reportlab so
rows auto-size to content and never overlap.

Page 1 — At a glance:
  Header band, hero strip (4 KPIs), multi-period returns table,
  cumulative return chart, drawdown chart

Page 2 — Positioning + activity:
  Live regime state cards, current target portfolio (with $ on $1.0M),
  activity this week, watchlist, sleeve YTD contribution chart,
  per-sleeve standalone stats, asset class exposure

Personal research artefact — no fund / manager branding.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing sibling scripts/ modules when build_factsheet is run as a
# script (PROJECT_ROOT/scripts/build_factsheet.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from regime_publish import regime_publish_status  # noqa: E402

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Phase 28.7 — institutional chart styling.
# Inspired by the navigo-systematic-trend dashboard (Plotly), translated
# to matplotlib for the factsheet PDF: faint gridlines, hidden top/right
# spines, tabular-numeral axis ticks via DejaVu Sans Mono (matplotlib-
# bundled — no external font install needed), tight margins, no chart-
# internal titles (titles live in section_header() above each chart).
# Applied once at module load so every figure picks them up.
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8.5,
    "axes.facecolor": "white",
    "axes.edgecolor": "#cfcdc4",
    "axes.linewidth": 0.6,
    "axes.labelcolor": "#3a4148",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": "#ededea",
    "grid.linewidth": 0.5,
    "xtick.color": "#7c8590",
    "ytick.color": "#7c8590",
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    # DejaVu Sans Mono ships with matplotlib and renders tabular
    # numerals out of the box. Falling back to DejaVu Sans for any
    # non-numeric tick text keeps body labels readable.
    "xtick.major.size": 3,
    "ytick.major.size": 3,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "legend.frameon": False,
    "legend.fontsize": 8.5,
    "savefig.facecolor": "white",
    "savefig.edgecolor": "white",
})

# Desaturated print-safe palette (per navigo-systematic-trend audit).
PALETTE_BLEND   = "#1a8754"  # green — the deployed blend / model line
PALETTE_SPY     = "#2563eb"  # blue  — SPY / primary benchmark
PALETTE_BENCH   = "#8a8a82"  # grey  — secondary benchmarks
PALETTE_DD      = "#b91c1c"  # red   — drawdown
PALETTE_A       = "#2563eb"  # blue  — Strategy A (US sectors)
PALETTE_B       = "#7c3aed"  # purple — Strategy B (asset class)
PALETTE_C       = "#b45309"  # amber — Strategy C (thematic)
PALETTE_D       = "#0891b2"  # teal  — Strategy D (Europe)
PALETTE_GRID    = "#ededea"
PALETTE_ZERO    = "#cfcdc4"
PALETTE_FILL    = (26/255, 135/255, 84/255, 0.06)  # blend-green @ 6%

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame,
                                  Paragraph, Spacer, Table, TableStyle,
                                  Image, PageBreak, KeepTogether)
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

# Brand colour palette
INK = colors.HexColor("#0f1217")
INK_SOFT = colors.HexColor("#3a4148")
INK_FAINT = colors.HexColor("#7c8590")
BG_PANEL = colors.HexColor("#f7f8fa")
BG_HEADER = colors.HexColor("#1a2333")
BORDER = colors.HexColor("#e1e4e8")
BORDER_STRONG = colors.HexColor("#c8ccd2")
ACCENT = colors.HexColor("#1351b4")
GOOD = colors.HexColor("#1d7a3a")
BAD = colors.HexColor("#b3261e")
WARN = colors.HexColor("#b76e00")
ZEBRA = colors.HexColor("#fafbfc")
WHITE = colors.white


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
    # Live mark-to-market overlay (optional; absent on a fresh checkout
    # before any daily run). When present, get_deployed() splices the
    # extension into the returned deployed-blend series so the factsheet
    # stats and "as of" date advance through the intra-week NAV.
    lt_path = DATA_DIR / "live_track.json"
    live_track = json.loads(lt_path.read_text(encoding="utf-8")) if lt_path.exists() else None
    # Phase 28.5 — breadth_csp1 end_date drives the regime publish freshness
    # check. The risk overlay reads breadth from this panel; if the panel
    # is stale the published regime headline is silently wrong (the actual
    # 2026-03-27 de-risk was invisible for 11 weeks because nothing checked
    # this end_date at publish time).
    # Phase 28.7d — per-ETF attribution needs 1-week + YTD price returns
    # for every deployed holding. holdings_prices_1y.json is exported by
    # scripts/export_holdings_prices.py as part of the pipeline run.
    hp_path = DATA_DIR / "holdings_prices_1y.json"
    holdings_prices = (json.loads(hp_path.read_text(encoding="utf-8"))
                          if hp_path.exists() else None)
    breadth_path = DATA_DIR / "breadth_csp1.json"
    breadth_end_date = None
    if breadth_path.exists():
        breadth_end_date = json.loads(
            breadth_path.read_text(encoding="utf-8")
        ).get("end_date")
    return (multi, overlay, sleeves, live_track, breadth_end_date,
             holdings_prices)


def _extend_with_live(series_dates: list, series_equity: list,
                       live_dates: list, live_equity: list,
                       anchor_date: str, label: str) -> tuple[list, list]:
    """Append intra-week live-track points onto a Friday-anchored series.

    Skips with a printed warning if the anchor does not match the
    series' last date — never blends mismatched series."""
    if not live_dates or not live_equity or len(live_dates) != len(live_equity):
        return series_dates, series_equity
    if not series_dates or series_dates[-1] != anchor_date:
        print(f"  WARN: live anchor {anchor_date} does not match "
              f"{label} last date {series_dates[-1] if series_dates else 'EMPTY'} — "
              "skipping live splice")
        return series_dates, series_equity
    return list(series_dates) + list(live_dates), list(series_equity) + list(live_equity)


def get_deployed(multi, overlay, live_track=None):
    overlay_variants = (overlay or {}).get("gated_variants", {})
    strategies = multi.get("strategies", {})
    deployed_key = None
    deployed_blend = None
    for key in ("blend_35_35_10_20_gated_eem_tilted",
                 "blend_35_35_10_20_gated"):
        if key in overlay_variants:
            deployed_key, deployed_blend = key, overlay_variants[key]
            break
    if deployed_blend is None:
        for key in ("blend_35_35_10_20", "blend_45_45_10"):
            if key in strategies:
                deployed_key, deployed_blend = key, strategies[key]
                break
    if deployed_blend is None:
        raise RuntimeError("No deployed blend found")

    # Splice live-track extension if present and anchor matches.
    if live_track and live_track.get("deployed_key") == deployed_key:
        ext_dates, ext_equity = _extend_with_live(
            deployed_blend.get("dates", []),
            deployed_blend.get("equity", []),
            live_track.get("live_dates") or [],
            live_track.get("live_equity") or [],
            live_track.get("anchor_date") or "",
            f"deployed blend ({deployed_key})",
        )
        if len(ext_dates) > len(deployed_blend.get("dates", [])):
            # Shallow-copy so we don't mutate the loaded JSON
            deployed_blend = dict(deployed_blend)
            deployed_blend["dates"] = ext_dates
            deployed_blend["equity"] = ext_equity
            n_new = len(ext_dates) - len(overlay_variants.get(deployed_key, {}).get("dates", deployed_blend["dates"]))
            print(f"  factsheet: spliced {len(live_track.get('live_dates') or [])} "
                  f"live point(s); deployed series now ends {ext_dates[-1]}")
    return deployed_key, deployed_blend


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


def colour_for(x):
    if x is None or x == 0: return INK_SOFT
    return GOOD if x > 0 else BAD


# ----- Paragraph styles ----------------------------------------------------

def _styles():
    base = getSampleStyleSheet()["Normal"]
    return {
        # Phase 28.7f — typography pass for institutional feel.
        # - Section title bumped 9 -> 10pt and tracking via uppercase
        #   strings (already used at call sites). Helvetica-Bold is the
        #   built-in PDF default; using a TTF (e.g. Inter) would need
        #   font installation, kept out of scope for portability.
        # - Section sub 7.5 -> 8pt, italic kept for hierarchy.
        # - Body 8.5 -> 9pt with looser leading for breathing room.
        "section": ParagraphStyle(
            "section", parent=base, fontName="Helvetica-Bold",
            fontSize=10, leading=12, textColor=INK,
            spaceBefore=0, spaceAfter=1),
        "section_sub": ParagraphStyle(
            "section_sub", parent=base, fontName="Helvetica-Oblique",
            fontSize=8, leading=11, textColor=INK_FAINT,
            spaceBefore=0, spaceAfter=4),
        "body": ParagraphStyle("body", parent=base, fontName="Helvetica",
                                 fontSize=9, leading=12, textColor=INK),
        "body_small": ParagraphStyle(
            "body_small", parent=base, fontName="Helvetica",
            fontSize=8, leading=10.5, textColor=INK_SOFT,
            spaceBefore=0, spaceAfter=0),
        # Phase 28.7 — switched alignment from TA_CENTER to TA_LEFT and
        # bumped fontSize from 7 to 8 (label) and 8 (sub) so the SPY
        # benchmark line in the new hero strip is comfortably legible
        # alongside the bigger 22pt primary value. Letter-tracking via
        # ParagraphStyle is not exposed in ReportLab so we get tighter
        # type just from the explicit alignment + larger size.
        "kpi_label": ParagraphStyle(
            "kpi_label", parent=base, fontName="Helvetica-Bold",
            fontSize=8, leading=10, textColor=INK_FAINT,
            alignment=TA_LEFT, spaceBefore=0, spaceAfter=3),
        "kpi_sub": ParagraphStyle(
            "kpi_sub", parent=base, fontName="Helvetica",
            fontSize=8, leading=10, textColor=INK_SOFT,
            alignment=TA_LEFT, spaceBefore=3, spaceAfter=0),
        "card_label": ParagraphStyle(
            "card_label", parent=base, fontName="Helvetica-Bold",
            fontSize=7, leading=9, textColor=INK_FAINT,
            spaceBefore=0, spaceAfter=2),
        "card_sub": ParagraphStyle(
            "card_sub", parent=base, fontName="Helvetica",
            fontSize=7, leading=9, textColor=INK_SOFT,
            spaceBefore=1, spaceAfter=0),
    }


def col_p(text, colour, fontname="Helvetica", fontsize=8.5, align=TA_LEFT):
    """Inline coloured paragraph for varying-colour cells."""
    style = ParagraphStyle(
        "_inline", fontName=fontname, fontSize=fontsize, leading=fontsize+2,
        textColor=colour, alignment=align,
        spaceBefore=0, spaceAfter=0,
    )
    return Paragraph(text, style)


# ----- Chart renderers (matplotlib -> PNG bytes -> reportlab Image) -------

def _chart_to_image(fig, width_pts, dpi=200):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                 pad_inches=0.1, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    fig_w_in, fig_h_in = fig.get_size_inches()
    aspect = fig_h_in / fig_w_in
    return Image(buf, width=width_pts, height=width_pts * aspect)


def chart_performance_dual(deployed_series, spy_series, overlay, width_pts):
    """Phase 28.7 — equity-with-drawdown ribbon, single figure.

    Replaces the prior pair of independent ``chart_cumulative`` +
    ``chart_drawdown`` charts. The two share an x-axis and are stacked
    in a 3:1 height ratio — the single most "institutional" chart
    device on the navigo-systematic-trend dashboard. Saves vertical
    space on page 3 and visually links peak-to-trough to the running
    equity line directly above it.
    """
    fig = plt.figure(figsize=(8, 3.6), facecolor="white")
    gs = fig.add_gridspec(
        2, 1, height_ratios=[3, 1], hspace=0.08,
        left=0.06, right=0.985, top=0.97, bottom=0.10,
    )
    ax_eq = fig.add_subplot(gs[0])
    ax_dd = fig.add_subplot(gs[1], sharex=ax_eq)

    s = deployed_series / deployed_series.iloc[0]
    eq_pct = (s - 1) * 100

    # ----- top panel: cumulative return -----
    ax_eq.fill_between(s.index, eq_pct.values, 0, color=PALETTE_FILL,
                        linewidth=0, zorder=2)
    ax_eq.plot(s.index, eq_pct.values, color=PALETTE_BLEND, linewidth=1.6,
                label="Strategy", zorder=4)
    if spy_series is not None:
        spy = spy_series.reindex(s.index, method="ffill").dropna()
        if len(spy) > 5:
            spy = spy / spy.iloc[0]
            ax_eq.plot(spy.index, (spy - 1) * 100, color=PALETTE_SPY,
                        linewidth=1.0, linestyle=(0, (4, 3)),
                        label="SPY", zorder=3)
    # Risk-off shading on the equity panel (drawdown panel does not
    # need it — drawdown itself encodes the same information).
    # Phase 28.7e — clip spans to the data window. Without clipping, an
    # axvspan starting in 2019 silently pulls matplotlib's auto x-axis
    # back to 2019 even when the YTD-sliced data only covers 2026,
    # leaving a long empty stretch on the left of the chart.
    chart_start, chart_end = s.index[0], s.index[-1]
    if overlay and overlay.get("events"):
        off_start = None
        for ev in overlay["events"]:
            if ev["direction"] == "RISK_OFF":
                off_start = pd.to_datetime(ev["date"])
            elif ev["direction"] == "RISK_ON" and off_start is not None:
                span_end = pd.to_datetime(ev["date"])
                if span_end >= chart_start and off_start <= chart_end:
                    ax_eq.axvspan(max(off_start, chart_start),
                                   min(span_end, chart_end),
                                   color="#b76e00", alpha=0.08, zorder=1)
                off_start = None
        if off_start is not None and off_start <= chart_end:
            ax_eq.axvspan(max(off_start, chart_start), chart_end,
                           color="#b76e00", alpha=0.08, zorder=1)
    ax_eq.axhline(0, color=PALETTE_ZERO, linewidth=0.6, zorder=1)
    ax_eq.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v:.0f}%")
    )
    ax_eq.legend(loc="upper left", fontsize=8.5)
    # Hide x-tick labels on top panel — drawdown panel below carries them.
    plt.setp(ax_eq.get_xticklabels(), visible=False)
    ax_eq.tick_params(axis="x", length=0)

    # ----- bottom panel: drawdown ribbon -----
    dd = ((s - s.cummax()) / s.cummax()) * 100
    ax_dd.fill_between(dd.index, dd.values, 0, color=PALETTE_DD,
                        alpha=0.55, linewidth=0)
    ax_dd.plot(dd.index, dd.values, color=PALETTE_DD, linewidth=0.9)
    ax_dd.axhline(0, color=PALETTE_ZERO, linewidth=0.6)
    ax_dd.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v:.0f}%")
    )
    ax_dd.set_ylabel("Drawdown", fontsize=8, color="#3a4148", labelpad=2)
    # Auto-fit y-range to the visible drawdown — `dd.min()` reflects the
    # window the chart is actually showing (YTD only on page 2). Old
    # behaviour clamped to ~-30% which made a -2% YTD drawdown look like
    # a tiny scratch at the top of a mostly-empty panel.
    dd_min = float(dd.min()) if len(dd) else 0.0
    floor = min(-1.0, dd_min * 1.15) if dd_min < 0 else -1.0
    ax_dd.set_ylim(floor, max(0.5, abs(floor) * 0.05))

    # Phase 28.7e — explicit x-axis clamp. Without this, any artist
    # (axvspan, etc.) outside the data window can stretch the auto
    # x-axis, leaving a wide empty stretch where the line is not. Set
    # the limits AFTER plotting all artists so the clamp is final.
    ax_eq.set_xlim(chart_start, chart_end)
    ax_dd.set_xlim(chart_start, chart_end)

    return _chart_to_image(fig, width_pts)


# Phase 28.7 — backward-compatibility shim so any external caller that
# imports the old name still works while we migrate. The shim ignores
# the spy-only drawdown path of the prior chart_drawdown (it was visually
# redundant against the equity comparison and never landed on a final
# layout) and returns the new dual-panel chart instead.
def chart_cumulative(deployed_series, spy_series, overlay, width_pts):
    return chart_performance_dual(deployed_series, spy_series, overlay, width_pts)


def chart_drawdown(*args, **kwargs):
    # No-op — drawdown is now rendered inside chart_performance_dual.
    # Return a tiny spacer-sized image so any straggling caller does not
    # NoneType-crash; the canonical call site has been removed.
    fig, ax = plt.subplots(figsize=(8, 0.01), facecolor="white")
    ax.set_visible(False)
    return _chart_to_image(fig, kwargs.get("width_pts", args[-1] if args else 480))


def _collect_deployed_holdings(sleeves, p22_active):
    """Phase 28.7d — factored out of build_holdings_table so the per-ETF
    attribution chart and the holdings PDF table share one source of
    truth for effective NAV weights. Returns a list of
    ``{etf, sleeve, within, effective}`` dicts sorted by effective desc.
    """
    sleeve_weights = {"a": 0.35, "b": 0.25 if p22_active else 0.35,
                      "c": 0.10, "d": 0.20}
    sleeve_letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    holdings = []
    for key, sleeve_wt in sleeve_weights.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if not trades:
            continue
        latest = trades[-1]
        for h in latest.get("holdings", []):
            eff = h.get("weight", 0) * sleeve_wt
            holdings.append({
                "etf": h.get("etf"), "sleeve": sleeve_letter[key],
                "within": h.get("weight", 0), "effective": eff,
            })
    if p22_active:
        holdings.append({"etf": "EEM", "sleeve": "TILT",
                         "within": 1.0, "effective": 0.10})
    holdings.sort(key=lambda x: -x["effective"])
    return holdings


def _etf_return_over_window(holdings_prices, etf, days_back):
    """Compute an ETF's total return over the last ``days_back`` calendar
    days using the holdings_prices_1y panel. Returns None if the ETF or
    enough history is not available.
    """
    if not holdings_prices:
        return None
    prices_block = (holdings_prices.get("prices") or {}).get(etf)
    if not prices_block:
        return None
    arr = prices_block.get("prices") or []
    dates = prices_block.get("dates") or []
    if len(arr) < 2 or len(dates) != len(arr):
        return None
    end_idx = len(arr) - 1
    end_date = pd.Timestamp(dates[end_idx])
    target = end_date - pd.Timedelta(days=days_back)
    # Walk back to the first date <= target. arr.index requires exact
    # match, which is not safe for non-trading-day targets.
    start_idx = 0
    for i, d in enumerate(dates):
        if pd.Timestamp(d) <= target:
            start_idx = i
    p0 = arr[start_idx]
    p1 = arr[end_idx]
    if p0 is None or p1 is None or p0 == 0:
        return None
    return (p1 / p0) - 1.0


def _etf_return_from_date(holdings_prices, etf, start_date):
    """Total return from a specific Timestamp to the latest available."""
    if not holdings_prices:
        return None
    prices_block = (holdings_prices.get("prices") or {}).get(etf)
    if not prices_block:
        return None
    arr = prices_block.get("prices") or []
    dates = prices_block.get("dates") or []
    if len(arr) < 2 or len(dates) != len(arr):
        return None
    target = pd.Timestamp(start_date)
    start_idx = 0
    for i, d in enumerate(dates):
        if pd.Timestamp(d) <= target:
            start_idx = i
    p0 = arr[start_idx]
    p1 = arr[-1]
    if p0 is None or p1 is None or p0 == 0:
        return None
    return (p1 / p0) - 1.0


# Map sleeve letter -> palette colour for the per-ETF chart.
_SLEEVE_PALETTE = {"A": PALETTE_A, "B": PALETTE_B, "C": PALETTE_C,
                    "D": PALETTE_D, "TILT": "#b45309"}


def chart_per_etf_attribution(sleeves, p22_active, holdings_prices,
                                width_pts, *, days_back=None, ytd_start=None,
                                top_n=12):
    """Per-ETF contribution to the deployed blend's return over a window.

    Returns a horizontal-bar chart sorted by absolute contribution; bars
    coloured by sleeve so the reader can see at a glance which sleeve's
    positions drove the move. Pass exactly one of ``days_back`` (e.g.
    7 for this week's attribution) or ``ytd_start`` (a Timestamp).

    Each bar = effective NAV weight × ETF total return over the window.
    The sum across all positions reconciles to the blend return for the
    same window when weights are static (true week-over-week between
    rebalances; YTD is approximate because rebalances change weights).
    """
    if days_back is None and ytd_start is None:
        raise ValueError("pass days_back or ytd_start")
    holdings = _collect_deployed_holdings(sleeves, p22_active)

    rows = []
    for h in holdings:
        etf = h["etf"]
        wt = h["effective"]
        ret = (_etf_return_over_window(holdings_prices, etf, days_back)
                if days_back is not None
                else _etf_return_from_date(holdings_prices, etf, ytd_start))
        if ret is None:
            continue
        rows.append({"etf": etf, "sleeve": h["sleeve"],
                      "weight": wt, "ret": ret, "contrib": wt * ret})

    if not rows:
        fig, ax = plt.subplots(figsize=(8, 1.0), facecolor="white")
        ax.text(0.5, 0.5, "Per-ETF returns unavailable — "
                          "run scripts/export_holdings_prices.py",
                 ha="center", va="center", fontsize=9, color="#7c8590",
                 transform=ax.transAxes)
        ax.axis("off")
        return _chart_to_image(fig, width_pts)

    # Sort by absolute contribution; show the top_n that move the needle.
    rows.sort(key=lambda r: -abs(r["contrib"]))
    rows = rows[:top_n]
    # Within the kept rows, re-sort so largest positive sits at top,
    # negative at bottom (reading order).
    rows.sort(key=lambda r: -r["contrib"])

    # Dynamic height — give each row ~0.32 inches; clamp to a sensible
    # range so the chart is neither cramped nor wastefully tall.
    rows_height = max(2.0, min(4.5, 0.4 + 0.32 * len(rows)))
    fig, ax = plt.subplots(figsize=(8, rows_height), facecolor="white")
    fig.subplots_adjust(left=0.16, right=0.96, top=0.97, bottom=0.18)

    y_pos = np.arange(len(rows))
    bars_pct = [r["contrib"] * 100 for r in rows]
    colours = [_SLEEVE_PALETTE.get(r["sleeve"], "#7c8590") for r in rows]
    ax.barh(y_pos, bars_pct, color=colours, edgecolor="white",
             linewidth=0.8, height=0.62)
    for i, r in enumerate(rows):
        x = r["contrib"] * 100
        # Each bar's label: "+0.45pp" plus a faint suffix showing the
        # raw ETF return so the reader can separate "big weight × small
        # move" from "small weight × big move".
        offset = 0.5 if x >= 0 else -0.5
        ha = "left" if x >= 0 else "right"
        ax.text(x + offset, i,
                 f"{x:+.2f}pp  ({r['ret']*100:+.1f}%)",
                 fontsize=8, color="#3a4148", va="center", ha=ha)
    # Tick labels: "TICKER (sleeve)" e.g. "SOXX (A)" — the sleeve tag
    # helps the eye associate colour with origin without a legend.
    ax.set_yticks(y_pos)
    ax.set_yticklabels([f"{r['etf']} ({r['sleeve']})" for r in rows],
                        fontsize=9, color="#0f1217")
    ax.invert_yaxis()
    ax.axvline(0, color=PALETTE_ZERO, linewidth=0.7)
    ax.grid(False, axis="y")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:+.1f}pp"))
    xmin, xmax = ax.get_xlim()
    pad = max(abs(xmin), abs(xmax)) * 0.45 + 0.5
    ax.set_xlim(xmin - pad if xmin < 0 else -pad * 0.4,
                  xmax + pad if xmax > 0 else pad * 0.4)
    return _chart_to_image(fig, width_pts)


def chart_sleeve_attribution(sleeves, deployed_series, p22_active, width_pts,
                              period_start=None, period_label="ytd"):
    """Phase 28.7 — horizontal bars sorted by contribution, palette
    consolidated against PALETTE_*, chart-internal title removed (the
    section_header() above the chart carries it instead).

    Phase 28.7c — generalised to any return window. ``period_start``
    defaults to the start of the calendar year (YTD); pass an earlier
    Timestamp to widen, or ``last_date - 7d`` for the weekly view on
    page 1 (what drove this week's blend return).
    """
    fig, ax = plt.subplots(figsize=(8, 2.5), facecolor="white")
    fig.subplots_adjust(left=0.18, right=0.96, top=0.96, bottom=0.18)

    last_date = deployed_series.index[-1]
    ytd_start = (period_start
                  if period_start is not None
                  else pd.Timestamp(last_date.year, 1, 1))
    sleeve_meta = [
        ("a", "US Sectors (A)",    PALETTE_A, 0.35),
        ("b", "Asset Class (B)",   PALETTE_B, 0.25 if p22_active else 0.35),
        ("c", "Thematic (C)",      PALETTE_C, 0.10),
        ("d", "Europe (D)",        PALETTE_D, 0.20),
    ]
    rows = []
    for key, label, col, wt in sleeve_meta:
        s = sleeves.get(key, {})
        blob = s.get("headline", {})
        dates = blob.get("headline_equity_dates")
        equity = blob.get("headline_equity")
        ret = (window_ret(pd.Series(equity, index=pd.to_datetime(dates)),
                          ytd_start) if (dates and equity) else None)
        rows.append({"name": label, "colour": col,
                      "contrib": (ret or 0) * wt})
    if p22_active:
        rows.append({"name": "EEM Tilt", "colour": "#b45309",
                      "contrib": 0.0})

    # Sort by contribution desc — largest positive at top, largest
    # negative at bottom. Eye reads top-to-bottom and gets the
    # contribution ordering for free.
    rows.sort(key=lambda r: -r["contrib"])
    names    = [r["name"] for r in rows]
    contribs = [r["contrib"] for r in rows]
    colours  = [r["colour"] for r in rows]

    y_pos = np.arange(len(names))
    bars_pct = [c * 100 for c in contribs]
    ax.barh(y_pos, bars_pct, color=colours, edgecolor="white",
             linewidth=1, height=0.62)
    for i, x in enumerate(bars_pct):
        offset = 0.5 if x >= 0 else -0.5
        ha = "left" if x >= 0 else "right"
        ax.text(x + offset, i, f"{x:+.1f}pp",
                 fontsize=8.5, color="#3a4148", fontweight="600",
                 va="center", ha=ha)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=9, color="#0f1217")
    ax.invert_yaxis()
    ax.axvline(0, color=PALETTE_ZERO, linewidth=0.7)
    # Only x-gridlines for a horizontal-bar chart.
    ax.grid(False, axis="y")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:+.0f}pp"))
    xmin, xmax = ax.get_xlim()
    pad = max(abs(xmin), abs(xmax)) * 0.35 + 1
    ax.set_xlim(xmin - pad if xmin < 0 else -pad * 0.3,
                  xmax + pad if xmax > 0 else pad * 0.3)
    return _chart_to_image(fig, width_pts)


# ----- Header / footer canvas hooks ---------------------------------------

class _PageCanvas(canvas.Canvas):
    """Custom canvas drawing header band + footer disclaimer on every page."""

    def __init__(self, *args, asof_str="", computed_at="", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages = []
        self._asof = asof_str
        self._computed_at = computed_at

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for i, state in enumerate(self._saved_pages):
            self.__dict__.update(state)
            self._draw_header(i + 1, total)
            self._draw_footer()
            super().showPage()
        super().save()

    def _draw_header(self, page_n, total):
        w, h = A4
        band_h = 12 * mm
        self.setFillColor(BG_HEADER); self.rect(0, h - band_h, w, band_h, fill=1, stroke=0)
        self.setFillColor(WHITE); self.setFont("Helvetica-Bold", 9)
        self.drawString(15 * mm, h - 7.5 * mm,
                          "USD MULTI-STRATEGY ETF PORTFOLIO   ·   WEEKLY FACTSHEET")
        self.setFillColor(colors.HexColor("#c8ccd2")); self.setFont("Helvetica", 8)
        self.drawRightString(w - 15 * mm, h - 7.5 * mm,
                                f"As of {self._asof}   ·   Page {page_n} of {total}")

    def _draw_footer(self):
        w, _ = A4
        band_h = 9 * mm
        self.setFillColor(BG_PANEL); self.rect(0, 0, w, band_h, fill=1, stroke=0)
        self.setStrokeColor(BORDER); self.setLineWidth(0.4)
        self.line(0, band_h, w, band_h)
        self.setFillColor(INK_FAINT); self.setFont("Helvetica", 6.5)
        self.drawString(15 * mm, 4.5 * mm,
                          "Personal research artefact · NOT investment advice · "
                          "Past simulated performance is not indicative of future returns")
        self.drawRightString(w - 15 * mm, 4.5 * mm,
                                f"Generated {self._computed_at}   ·   "
                                f"github.com/phuazz/breadth-thrust-etf")


# ----- Section builders ----------------------------------------------------

def section_header(title, sub, styles):
    """Phase 28.7f — section header with a subtle 0.4pt rule under the
    sub-line for a "publication" feel. The rule is a 1-row Table with
    only a LINEABOVE style so it draws a single thin line; cheap and
    keeps the API of section_header() identical for callers.
    """
    rule = Table([[""]], colWidths=["100%"], rowHeights=[0.5],
                  style=TableStyle([
                      ("LINEABOVE", (0, 0), (-1, 0), 0.4, BORDER_STRONG),
                      ("TOPPADDING", (0, 0), (-1, -1), 0),
                      ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                  ]))
    return [Paragraph(title, styles["section"]),
             Paragraph(sub, styles["section_sub"]),
             rule,
             Spacer(1, 3)]


def _kt(items):
    """Phase 28.7f — wrap a header+content pair so ReportLab never breaks
    a section header onto a different page from its chart or table.
    The 'titles apart from charts in different pages' issue was the
    default flow allowing a PageBreak between a `section_header()` and
    the next flowable. Wrapping the whole block in KeepTogether forces
    them to land on the same page or both move to the next one.
    """
    if isinstance(items, list):
        return KeepTogether(items)
    return KeepTogether([items])


def build_hero_strip(deployed_series, full_stats, page_w, styles,
                       spy_series=None):
    """Four KPI cells in one row, each card showing strategy P&L against SPY.

    Phase 28.7 — every primary KPI carries its benchmark context in-card.
    Previously the hero strip showed bare strategy numbers (THIS WEEK / YTD
    / SINCE INCEPTION / MAX DRAWDOWN) with no benchmark per cell — the
    investor had to flip to page 3's returns table to find 'vs SPY'. This
    rewrite mirrors the institutional-factsheet pattern (label · big P&L ·
    'SPY +X% · vs SPY +Y%') so the answer to 'did we beat the index this
    week?' is in the first card on the first page.

    Periods chosen for a weekly-cadence read:
      1-WEEK P&L  — what changed since the prior factsheet (priority)
      1-MONTH P&L — short-term momentum context
      YEAR TO DATE — conventional benchmark window
      SINCE INCEPTION — long-term track record

    Max-drawdown is moved to page 3's per-sleeve stats table where it sits
    next to Sharpe and CAGR (the other risk-adjusted figures).
    """
    last_date = deployed_series.index[-1]
    windows = [
        ("1-WEEK P&L",   last_date - pd.Timedelta(days=7)),
        ("1-MONTH P&L",  last_date - pd.DateOffset(months=1)),
        ("YEAR TO DATE", pd.Timestamp(last_date.year, 1, 1)),
        ("SINCE INCEPTION", deployed_series.index[0]),
    ]
    cells = []
    for label, start in windows:
        strat = window_ret(deployed_series, start)
        spy = window_ret(spy_series, start) if spy_series is not None else None
        delta = (strat - spy) if (strat is not None and spy is not None) else None
        cells.append({
            "label": label,
            "value": fmt_pct(strat),
            "colour": colour_for(strat),
            "spy_str": (f"SPY {fmt_pct(spy)}" if spy is not None else None),
            "delta_str": (f"vs SPY {fmt_pct(delta)}" if delta is not None else None),
            "delta_colour": colour_for(delta) if delta is not None else INK_FAINT,
        })

    cell_tables = []
    for c in cells:
        # Bottom row: 'SPY +X.X%  ·  vs SPY +Y.Y%' assembled as a single
        # Paragraph with inline colour spans, so the delta gets its own
        # green/red colour without breaking the layout into separate cells.
        if c["spy_str"] and c["delta_str"]:
            delta_hex = c["delta_colour"].hexval() if hasattr(c["delta_colour"], "hexval") else "#5a6068"
            # ReportLab Color.hexval() returns 0xRRGGBBAA — slice to 6 hex
            # chars after the '0x' prefix for an HTML colour.
            try:
                hx = delta_hex[2:8] if delta_hex.startswith("0x") else delta_hex
                colour_attr = f"#{hx}"
            except Exception:
                colour_attr = "#5a6068"
            sub_html = (
                f'<font color="#5a6068">{c["spy_str"]}</font>'
                f'<font color="#9aa1a8">  &middot;  </font>'
                f'<font color="{colour_attr}"><b>{c["delta_str"]}</b></font>'
            )
        else:
            sub_html = "—"
        ct = Table([
            [Paragraph(c["label"], styles["kpi_label"])],
            [col_p(c["value"], c["colour"], fontname="Helvetica-Bold",
                    fontsize=22, align=TA_LEFT)],
            [Paragraph(sub_html, styles["kpi_sub"])],
        ], colWidths=[page_w / 4 - 8], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (0, 0), 4),
            ("BOTTOMPADDING", (0, 0), (0, 0), 2),
            ("TOPPADDING", (0, 1), (0, 1), 0),
            ("BOTTOMPADDING", (0, 1), (0, 1), 2),
            ("TOPPADDING", (0, 2), (0, 2), 0),
            ("BOTTOMPADDING", (0, 2), (0, 2), 4),
        ]))
        cell_tables.append(ct)
    return Table([cell_tables], colWidths=[page_w / 4] * 4,
                   style=TableStyle([
                       ("BACKGROUND", (0, 0), (-1, -1), BG_PANEL),
                       ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                       ("LINEAFTER", (0, 0), (-2, -1), 0.6, BORDER),
                       ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                       ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                       ("TOPPADDING", (0, 0), (-1, -1), 10),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                   ]))


def build_returns_table(deployed_series, spy_series, page_w, styles):
    last_date = deployed_series.index[-1]
    # Phase 30 — these two rows compute TRAILING windows (last close minus 7
    # days / minus 1 month), so they were mislabelled "Week/Month to date"
    # (a to-date figure would anchor to Monday / the 1st of the month — a 2-day
    # number as of 02 Jul). Renamed to match the computation and the page-1
    # "1-WEEK / 1-MONTH" hero tiles, which use the identical anchors.
    windows = [
        ("1 week",         last_date - pd.Timedelta(days=7)),
        ("1 month",        last_date - pd.DateOffset(months=1)),
        ("3 months",       last_date - pd.DateOffset(months=3)),
        ("6 months",       last_date - pd.DateOffset(months=6)),
        ("Year to date",   pd.Timestamp(last_date.year, 1, 1)),
        ("1 year",         last_date - pd.DateOffset(years=1)),
        ("3 years",        last_date - pd.DateOffset(years=3)),
        ("Inception",      deployed_series.index[0]),
    ]
    data = [["PERIOD", "STRATEGY", "SPY", "Δ VS SPY"]]
    for label, start in windows:
        strat = window_ret(deployed_series, start)
        spy = window_ret(spy_series, start) if spy_series is not None else None
        delta = (strat - spy) if (strat is not None and spy is not None) else None
        data.append([
            label,
            col_p(fmt_pct(strat), colour_for(strat),
                   fontname="Helvetica-Bold", fontsize=9.5, align=TA_RIGHT),
            col_p(fmt_pct(spy), colour_for(spy), fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(delta), colour_for(delta),
                   fontname="Helvetica-Bold", fontsize=9, align=TA_RIGHT),
        ])
    t = Table(data, colWidths=[page_w * 0.40, page_w * 0.20,
                                  page_w * 0.18, page_w * 0.22])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_FAINT),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, BORDER_STRONG),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (0, -1), 9),
        ("TEXTCOLOR", (0, 1), (0, -1), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
    ]))
    return t


# =============================================================================
# Phase 28.5 — regime publish guard (FM-1 + FM-3 surfacing)
# =============================================================================
# The 2026-06-13 weekly publish printed "RISK_ON since 2025-05-02, breadth 55%,
# ARMED, +35pp buffer" while the actual market reading on 2026-03-27 had been
# 19.4% (a true de-risk trigger). The breadth panel feeding the regime gate
# had stopped advancing on 2026-05-29 and nothing in the publish path checked
# that. build_regime_block() now consults regime_publish_status — when stale,
# the entire regime block is replaced with a STALE banner; when near a
# threshold, the watchlist 'ARMED' label becomes 'NEAR'.

def build_regime_block(overlay, panel_end_date, today):
    """Return a structured verdict for the regime headline.

    Args:
        overlay: parsed ``risk_overlay.json``.
        panel_end_date: date | str | None — the ``end_date`` of the breadth
            panel that fed the overlay (typically breadth_csp1.json).
        today: date — the build's reference date.

    Returns a dict with:
        publishable: bool
        status: 'ok' | 'stale' | 'near' | 'no_data'
        message: str — banner text when not 'ok'
        breadth_state: 'RISK_ON' | 'RISK_OFF' | 'UNKNOWN'
        breadth_since: ISO date | None
        breadth_pct: float in [0,1] | None
        proximity_band: str | None
        panel_end_date: ISO date | None

    Renderers consume the dict; they do not re-implement the verdict logic.
    Test rigs inspect ``str(dict)`` to verify that confident copy is suppressed
    on stale or near-threshold inputs.
    """
    if not overlay:
        return {
            "publishable": False, "status": "no_data",
            "message": "REGIME STATE UNAVAILABLE — risk_overlay.json missing.",
            "breadth_state": "UNKNOWN", "breadth_since": None,
            "breadth_pct": None, "proximity_band": None,
            "panel_end_date": None,
        }
    if isinstance(panel_end_date, str):
        panel_end_date_obj = datetime.fromisoformat(panel_end_date).date()
    elif isinstance(panel_end_date, datetime):
        panel_end_date_obj = panel_end_date.date()
    else:
        panel_end_date_obj = panel_end_date  # already a date or None

    state = overlay.get("current_state", "UNKNOWN")
    breadth = overlay.get("current_breadth")
    since = overlay.get("current_state_since")
    gp = overlay.get("gate_parameters", {}) or {}
    off_thr = gp.get("off_threshold", 0.20)
    on_thr = gp.get("on_threshold", 0.50)

    if panel_end_date_obj is None or breadth is None:
        return {
            "publishable": False, "status": "no_data",
            "message": "REGIME STATE UNAVAILABLE — breadth panel end_date "
                        "or current_breadth missing.",
            "breadth_state": state, "breadth_since": since,
            "breadth_pct": breadth, "proximity_band": None,
            "panel_end_date": (panel_end_date_obj.isoformat()
                                 if panel_end_date_obj else None),
        }
    status_obj = regime_publish_status(
        panel_end_date=panel_end_date_obj,
        current_breadth=breadth,
        off_threshold=off_thr, on_threshold=on_thr,
        today=today,
    )
    return {
        "publishable": status_obj.publishable,
        "status": status_obj.status,
        "message": status_obj.message,
        "breadth_state": state,
        "breadth_since": since,
        "breadth_pct": breadth,
        "proximity_band": status_obj.proximity_band,
        "panel_end_date": status_obj.panel_end_date,
        "lag_trading_days": status_obj.lag_trading_days,
    }


def build_regime_panel(overlay, page_w, styles,
                         panel_end_date=None, today_override=None):
    today = today_override or datetime.now(timezone.utc).date()
    block = build_regime_block(overlay, panel_end_date, today)

    # Stale panel — replace the entire 3-card row with a single STALE banner
    # spanning the row. Same width budget so layout downstream is unchanged.
    if block["status"] in ("stale", "no_data"):
        banner_msg = block.get("message") or "REGIME STATE STALE"
        banner = Table(
            [[Paragraph(
                f"<b>REGIME STALE — DO NOT TRADE OFF THIS PANEL</b><br/>"
                f"{banner_msg}",
                styles["body"],
            )]],
            colWidths=[page_w],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff4e6")),
                ("BOX", (0, 0), (-1, -1), 1.0, BAD),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]),
        )
        return banner

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

    cards = [
        ("BREADTH REGIME", state_19,
         GOOD if risk_on else BAD,
         f"Active since {since_19} ({days_19}d)" if since_19 else "—",
         12),
        ("EM TILT STATE", p22.get("current_state", "—") if p22 else "—",
         WARN if p22_on else INK_FAINT,
         f"Active since {since_22} ({days_22}d)" if since_22 else
         ("Armed — awaiting golden cross" if p22 and p22.get("enabled") else "—"),
         12),
        ("LIVE BLEND TODAY", blend, ACCENT, "Target allocation today", 9),
    ]
    cell_tables = []
    for label, value, colour, sub, val_size in cards:
        ct = Table([
            [Paragraph(label, styles["card_label"])],
            [col_p(value, colour, fontname="Helvetica-Bold", fontsize=val_size)],
            [Paragraph(sub, styles["card_sub"])],
        ], colWidths=[page_w / 3 - 8], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (0, 0), 8),
            ("BOTTOMPADDING", (0, 0), (0, 0), 2),
            ("TOPPADDING", (0, 1), (0, 1), 2),
            ("BOTTOMPADDING", (0, 1), (0, 1), 4),
            ("TOPPADDING", (0, 2), (0, 2), 0),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
        ]))
        cell_tables.append(ct)
    return Table([cell_tables], colWidths=[page_w / 3] * 3,
                   style=TableStyle([
                       ("BACKGROUND", (0, 0), (-1, -1), BG_PANEL),
                       ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                       ("LINEAFTER", (0, 0), (-2, -1), 0.6, BORDER),
                       ("VALIGN", (0, 0), (-1, -1), "TOP"),
                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                       ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                       ("TOPPADDING", (0, 0), (-1, -1), 0),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                   ]))


def build_holdings_table(sleeves, p22_active, page_w, styles):
    sleeve_weights = {"a": 0.35, "b": 0.25 if p22_active else 0.35,
                      "c": 0.10, "d": 0.20}
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
                "etf": h.get("etf"), "sleeve": sleeve_letter[key],
                "within": h.get("weight", 0), "effective": eff,
                "signal": h.get("signal_pct") or h.get("breadth_pct"),
            })
    if p22_active:
        holdings.append({"etf": "EEM", "sleeve": "TILT",
                         "within": 1.0, "effective": 0.10, "signal": None})
    holdings = sorted(holdings, key=lambda x: -x["effective"])

    # Data-integrity guard (vault rule: cross-reference slides against source).
    # The "current target portfolio" must show EVERY deployed position — a
    # hard top-N truncation silently drops whole sleeves (the Thematic sleeve
    # sits at ~2% per name and used to fall below a top-14 cut, so LIT and the
    # rest of sleeve C never appeared and the printed weights summed to ~88%).
    # Show all holdings and warn loudly if they do not sum to ~100%.
    total_eff = sum(h["effective"] for h in holdings)
    if abs(total_eff - 1.0) > 0.015:
        print(f"  WARN: factsheet holdings sum to {total_eff*100:.1f}%, not ~100% "
              f"({len(holdings)} positions) — check sleeve weights / missing positions")

    data = [["TICKER", "SLEEVE", "SIGNAL", "TARGET WT", "$ ON $1.0M"]]
    for h in holdings:
        sig = f"{h['signal']:+.1f}%" if h["signal"] is not None else "—"
        cash = h["effective"] * 1_000_000
        data.append([
            col_p(h["etf"], INK, fontname="Courier-Bold", fontsize=9.5),
            col_p(h["sleeve"], INK_SOFT, fontsize=8.5),
            col_p(sig, INK_SOFT, fontname="Courier", fontsize=8.5, align=TA_RIGHT),
            col_p(fmt_pct(h["effective"], signed=False, dp=1), ACCENT,
                   fontname="Helvetica-Bold", fontsize=9.5, align=TA_RIGHT),
            col_p(f"${cash:,.0f}", INK, fontname="Courier", fontsize=9,
                   align=TA_RIGHT),
        ])
    t = Table(data, colWidths=[page_w * 0.17, page_w * 0.14, page_w * 0.17,
                                  page_w * 0.22, page_w * 0.30])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_FAINT),
        ("ALIGN", (0, 0), (1, 0), "LEFT"),
        ("ALIGN", (2, 0), (-1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, BORDER_STRONG),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
    ]))
    return t


def build_trades_table(sleeves, page_w, styles, p22_active, as_of=None):
    """Phase 28.7b — filter to THIS WEEK's rebal activity only.

    Prior version included every sleeve's most recent rebalance regardless
    of date. In the 2026-06-19 build, A/B/C's last rebal was 06-12 (LAST
    week) and D's was 06-19 (this week) — showing all together as 'this
    week' was misleading. Now: only rows from rebalances within the past
    7 calendar days are included. The DATE column is dropped because all
    rows are in the same week and the header carries the date stamp.

    Phase 30 — PRIOR / NEW / Δ are reported as % of TOTAL portfolio NAV
    (within-sleeve weight × sleeve weight), matching the CURRENT TARGET
    PORTFOLIO table. Previously these were within-sleeve percentages, which
    made trade sizes non-comparable across sleeves and misleading for
    execution: a C ``ENTER 20%`` (2.0% of NAV) looked larger than an A
    ``EXIT 20%`` (7.0% of NAV). The RESIZE display threshold stays on the
    within-sleeve delta so the set of displayed rows is unchanged; only the
    denominator of the printed figures changes. Sleeve B is 25% while the
    EEM tilt is on (``p22_active``), 35% otherwise — mirrors the holdings
    table and asset-class rollup so all three tables agree.
    """
    from datetime import date as _date, timedelta as _td
    # Phase 30 — anchor the 7-day window to the factsheet's DATA as-of date
    # (deployed series last close), NOT wall-clock today(). Keying off today()
    # made "this week" depend on which day the build ran: regenerating the same
    # data one day later silently dropped the week's rebalance rows (the
    # 2026-06-26 rebal vanished when rebuilt on 2026-07-04). The as-of anchor is
    # reproducible and makes the filter deterministic w.r.t. the data.
    if as_of is not None:
        anchor = as_of.date() if hasattr(as_of, "date") else as_of
    else:
        anchor = _date.today()
    cutoff = (anchor - _td(days=7)).isoformat()

    sleeve_letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    sleeve_wt = {"a": 0.35, "b": 0.25 if p22_active else 0.35,
                  "c": 0.10, "d": 0.20}
    rows = []
    week_rebal_dates: set[str] = set()
    most_recent_rebal: str | None = None
    for key, sleeve in sleeve_letter.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if len(trades) < 2: continue
        rebal_date = trades[-1].get("date", "")
        if not rebal_date:
            continue
        if most_recent_rebal is None or rebal_date > most_recent_rebal:
            most_recent_rebal = rebal_date
        if rebal_date < cutoff:
            continue  # this rebal predates the past-7-day window
        week_rebal_dates.add(rebal_date)
        sw = sleeve_wt[key]  # within-sleeve weight -> effective NAV weight
        prev_h = {h["etf"]: h["weight"] for h in trades[-2]["holdings"]}
        curr_h = {h["etf"]: h["weight"] for h in trades[-1]["holdings"]}
        for etf in curr_h:
            if etf not in prev_h:
                rows.append((sleeve, "ENTER", etf, None, curr_h[etf] * sw))
        for etf in prev_h:
            if etf not in curr_h:
                rows.append((sleeve, "EXIT", etf, prev_h[etf] * sw, None))
        for etf in curr_h:
            if etf in prev_h:
                d = curr_h[etf] - prev_h[etf]  # threshold on within-sleeve Δ
                if abs(d) > 0.01:
                    rows.append((sleeve, "RESIZE", etf,
                                  prev_h[etf] * sw, curr_h[etf] * sw))

    if not rows:
        msg = ("<i>No new rebalance activity this week — strategy stable. "
                f"Most recent rebal: {most_recent_rebal}.</i>"
                if most_recent_rebal else
                "<i>No position changes this week — strategy stable.</i>")
        return Paragraph(msg, styles["body"])

    data = [["SLEEVE", "ACTION", "TICKER", "PRIOR", "NEW", "Δ"]]
    for sleeve, action, etf, prev_w, new_w in rows:
        action_col = (GOOD if action == "ENTER" else BAD if action == "EXIT"
                       else WARN)
        d = (new_w or 0) - (prev_w or 0)
        data.append([
            col_p(sleeve, INK_SOFT, fontname="Helvetica-Bold", fontsize=9),
            col_p(action, action_col, fontname="Helvetica-Bold", fontsize=7.5),
            col_p(etf, INK, fontname="Courier-Bold", fontsize=9.5),
            col_p(fmt_pct(prev_w, signed=False) if prev_w is not None else "—",
                   INK_SOFT, fontname="Courier", fontsize=8.5, align=TA_RIGHT),
            col_p(fmt_pct(new_w, signed=False) if new_w is not None else "—",
                   INK_SOFT, fontname="Courier", fontsize=8.5, align=TA_RIGHT),
            col_p(fmt_pct(d, dp=1), colour_for(d),
                   fontname="Courier-Bold", fontsize=8.5, align=TA_RIGHT),
        ])
    # Widen PRIOR/NEW/Δ columns so percentages do not wrap to 2 rows.
    t = Table(data, colWidths=[page_w * 0.10, page_w * 0.17, page_w * 0.17,
                                  page_w * 0.18, page_w * 0.18, page_w * 0.20])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_FAINT),
        ("ALIGN", (3, 0), (-1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, BORDER_STRONG),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
    ]))
    return t


def build_watchlist(overlay, page_w, styles,
                      panel_end_date=None, today_override=None):
    items = []
    if overlay:
        today = today_override or datetime.now(timezone.utc).date()
        block = build_regime_block(overlay, panel_end_date, today)
        # Phase 28.5 — when the panel is stale, the regime card upstream
        # already shows a STALE banner. Drop the breadth row from the
        # watchlist entirely rather than printing the wrong margin/status.
        if block["status"] in ("stale", "no_data"):
            cur_breadth = (overlay.get("current_breadth", 0) or 0) * 100
            items.append({
                "label": "S&P 500 breadth",
                "value": f"{cur_breadth:.0f}% (stale)",
                "trigger": "Panel not refreshed — see banner above",
                "margin": "—",
                "status": "STALE",
                "status_col": BAD,
            })
        else:
            cur_breadth = overlay.get("current_breadth", 0) * 100
            off_thresh = overlay.get("gate_parameters", {}).get("off_threshold", 0.20) * 100
            on_thresh = overlay.get("gate_parameters", {}).get("on_threshold", 0.50) * 100
            state = overlay.get("current_state")
            # Near-threshold short-circuit (Phase 28.5 FM-3): if the helper
            # flagged this reading as near a gate boundary, the watchlist
            # 'ARMED' badge becomes 'NEAR' regardless of the 10pp heuristic.
            is_near = block["status"] == "near"
            if state == "RISK_ON":
                margin = cur_breadth - off_thresh
                items.append({
                    "label": "S&P 500 breadth", "value": f"{cur_breadth:.0f}%",
                    "trigger": f"De-risk if breadth < {off_thresh:.0f}%",
                    "margin": f"+{margin:.0f}pp buffer",
                    "status": ("NEAR" if is_near or margin <= 10 else "ARMED"),
                    "status_col": WARN if (is_near or margin <= 10) else GOOD,
                })
            else:
                margin = on_thresh - cur_breadth
                items.append({
                    "label": "S&P 500 breadth", "value": f"{cur_breadth:.0f}%",
                    "trigger": f"Re-engage if breadth > {on_thresh:.0f}%",
                    "margin": f"needs +{margin:.0f}pp",
                    "status": ("NEAR" if is_near else "DE-RISKED"),
                    "status_col": WARN if is_near else BAD,
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
                    "trigger": "Deactivate on death cross (50d < 200d)",
                    "margin": f"+{spread_pct:.1f}pp above cross",
                    "status": "TILT ON", "status_col": WARN,
                })
            else:
                items.append({
                    "label": "EEM/SPY 50d vs 200d MA",
                    "value": f"{spread_pct:+.1f}%",
                    "trigger": "Activate on golden cross (50d > 200d)",
                    "margin": f"needs +{abs(spread_pct):.1f}pp",
                    "status": "ARMED", "status_col": INK_SOFT,
                })

    cards = []
    for it in items[:2]:
        # Top row: label (left) | status badge (right)
        # Middle row: big value (left) | margin (right)
        # Bottom row: trigger description (full width, italic)
        card_inner = Table([
            [Paragraph(it["label"], styles["card_label"]),
             col_p(it["status"], it["status_col"],
                    fontname="Helvetica-Bold", fontsize=8, align=TA_RIGHT)],
            [col_p(it["value"], INK, fontname="Helvetica-Bold", fontsize=14),
             col_p(it["margin"], INK_SOFT, fontname="Courier",
                    fontsize=9, align=TA_RIGHT)],
            [col_p(f"<i>{it['trigger']}</i>", INK_FAINT, fontsize=7),
             ""],
        ], colWidths=[page_w * 0.55, page_w * 0.40], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BG_PANEL),
            ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
            ("TOPPADDING", (0, 1), (-1, 1), 2),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
            ("TOPPADDING", (0, 2), (-1, 2), 0),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
            ("SPAN", (0, 2), (-1, 2)),
        ]))
        cards.append([card_inner])

    if not cards:
        return Paragraph("<i>No watchlist items.</i>", styles["body"])
    return Table(cards, colWidths=[page_w], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (0, 0), 0),
        ("BOTTOMPADDING", (0, 0), (0, 0), 5),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (0, 1), 0),
    ]))


def build_sleeve_stats_table(sleeves, multi, page_w, styles):
    # Phase 28.7d — half-width-friendly labels and tighter percentage
    # formatting. Old labels "Strategy A — US Sectors" + "+18.4%"
    # wrapped to two rows at half-width; compact form fits cleanly.
    sleeve_rows = [
        ("a", "A · US Sectors",  "strategy_a"),
        ("b", "B · Asset Class", "strategy_b"),
        ("c", "C · Thematic",    "strategy_c"),
        ("d", "D · Europe",      "strategy_d"),
    ]
    data = [["SLEEVE", "SHARPE", "CAGR", "MAX DD", "YTD"]]
    for key, label, multi_key in sleeve_rows:
        st = multi.get("strategies", {}).get(multi_key, {})
        sharpe = st.get("sharpe"); cagr = st.get("cagr"); dd = st.get("max_dd")
        blob = sleeves.get(key, {}).get("headline", {})
        dates = blob.get("headline_equity_dates"); equity = blob.get("headline_equity")
        ytd = None
        if dates and equity:
            ser = pd.Series(equity, index=pd.to_datetime(dates))
            ytd = window_ret(ser, pd.Timestamp(ser.index[-1].year, 1, 1))
        # dp=0 percentages save 3 characters per cell — enough to keep
        # the half-width table single-line at any realistic value.
        data.append([
            col_p(label, INK, fontname="Helvetica-Bold", fontsize=9),
            col_p(fmt_num(sharpe) if sharpe else "—", colour_for(sharpe),
                   fontname="Courier-Bold", fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(cagr, dp=0) if cagr else "—", colour_for(cagr),
                   fontname="Courier", fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(dd, dp=0) if dd else "—", BAD,
                   fontname="Courier", fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(ytd, dp=0) if ytd is not None else "—",
                   colour_for(ytd), fontname="Courier-Bold",
                   fontsize=9, align=TA_RIGHT),
        ])
    # Narrower label column (28%) gives the four numeric columns
    # 18% each — more breathing room for the right-aligned numbers.
    t = Table(data, colWidths=[page_w * 0.28, page_w * 0.18,
                                  page_w * 0.18, page_w * 0.18, page_w * 0.18])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_FAINT),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, BORDER_STRONG),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
    ]))
    return t


def build_parameters_footer(overlay, sleeves, p22_active, breadth_end_date,
                              page_w, styles):
    """Phase 28.7f — last-page parameters + provenance footer.

    Three small purposes in one block:
      - PARAMETERS: the rules the strategy actually runs (sleeve weights,
        rebalance cadence, gate thresholds, EEM tilt rule, cost
        assumption). Useful for the reader who hasn't memorised them.
      - UNIVERSE: a one-liner per sleeve listing universe size + key
        examples — quick reference, not an exhaustive list.
      - PROVENANCE: data dates (signals as-of, breadth panel end, build
        time). Direct continuation of Phase 28.5 — every published
        number's source date is on the page that contains it.

    Replaces the white space that previously fell on the last page when
    chart heights + page breaks did not align.
    """
    gp = (overlay or {}).get("gate_parameters") or {}
    off_thr = gp.get("off_threshold", 0.20) * 100
    on_thr = gp.get("on_threshold", 0.50) * 100
    derisk = gp.get("derisk_fraction", 0.50) * 100
    p22 = (overlay or {}).get("phase22_eem_tilt") or {}
    tilt_active = p22.get("enabled") and p22.get("current_state") == "EM_TILT_ON"
    blend_line = ("35% A · 25% B · 10% C · 20% D · 10% EEM tilt"
                   if tilt_active
                   else "35% A · 35% B · 10% C · 20% D")

    def _last_rebal(key):
        th = (sleeves.get(key, {}).get("headline") or {}).get(
            "trade_history") or []
        return th[-1].get("date") if th else "—"

    parameters_data = [
        ["BLEND",       blend_line],
        ["REBALANCE",   "Weekly Friday close (per-sleeve cadence noted in dashboard)"],
        ["RISK OVERLAY", f"De-risk to {derisk:.0f}% NAV when S&P 500 breadth < "
                          f"{off_thr:.0f}%; re-engage when breadth > {on_thr:.0f}%"],
        ["EM TILT",     "Activates on EEM/SPY 50d &gt; 200d MA golden cross "
                         "(funded by reducing Strategy B's 35% &rarr; 25%)"],
        ["COST MODEL",  "5 bps per unit of weight change (10 bps round-trip)"],
        ["UNIVERSE",    "A · 14 US sector ETFs (SOXX, CSP1, CNDX, IUES, IUFS, "
                         "IUHC, IUIS, IUCS, IUCD, IUUS, IUMS, IUCM, "
                         "IUSP, IDP6; IUIT pruned) · B · 12 asset-class ETFs "
                         "(SPY, IJR, QQQ, EFA, VGK, EWJ, VNQ, GLD, DBC, TLT, "
                         "IEF, TIP; EEM via the EM tilt only since Phase 29) "
                         "· C · 25 thematic ETFs · D · 5 Stoxx Europe "
                         "600 sector UCITS"],
    ]
    pt = Table(
        [[col_p(k, INK_FAINT, fontname="Helvetica-Bold", fontsize=7.5),
          Paragraph(v, styles["body_small"])]
         for k, v in parameters_data],
        colWidths=[page_w * 0.16, page_w * 0.84],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
        ]),
    )

    sigs = {k: _last_rebal(k) for k in ("a", "b", "c", "d")}
    built_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    provenance_data = [
        ["SIGNALS AS OF",
         f"A {sigs['a']}  ·  B {sigs['b']}  ·  C {sigs['c']}  ·  D {sigs['d']}"],
        ["BREADTH PANEL", f"{breadth_end_date or '—'} (drives the Risk Overlay regime gate)"],
        ["BUILT",         built_iso],
    ]
    pv = Table(
        [[col_p(k, INK_FAINT, fontname="Helvetica-Bold", fontsize=7.5),
          col_p(v, INK_SOFT, fontname="Courier", fontsize=8)]
         for k, v in provenance_data],
        colWidths=[page_w * 0.16, page_w * 0.84],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
        ]),
    )

    return [
        Spacer(1, 4 * mm),
        *section_header("FUND PARAMETERS",
                         "The rules the strategy actually runs",
                         styles),
        pt,
        Spacer(1, 4 * mm),
        *section_header("DATA PROVENANCE",
                         "Every published number's source date",
                         styles),
        pv,
    ]


def build_asset_class_rollup(sleeves, p22_active, page_w, styles):
    AC_MAP = {
        **{e: "US Equity" for e in [
            "SOXX", "CSP1", "CNDX", "IUES", "IUFS", "IUHC", "IUIS",
            "IUCS", "IUCD", "IUUS", "IUMS", "IUCM", "IUSP", "IDP6",
            "SPY", "IJR", "QQQ"]},
        **{e: "Intl Developed Equity" for e in ["EFA", "VGK", "EWJ"]},
        **{e: "Emerging Mkts Equity" for e in ["EEM", "CQQQ", "159801.SZ"]},
        "VNQ": "Real Estate",
        **{e: "Commodities / Miners" for e in [
            "GLD", "DBC", "GDX", "COPX", "MOO", "XME", "WOOD", "REMX"]},
        **{e: "Bonds" for e in ["TLT", "IEF", "TIP", "SHY"]},
        **{e: "Intl Developed Equity" for e in [
            "EXV1", "EXH1", "EXV3", "EXH3", "EXH9"]},
        **{e: "Thematic" for e in [
            "ARKK", "CIBR", "SKYY", "BOTZ", "BLOK", "ICLN", "TAN",
            "LIT", "URA", "XBI", "ARKG", "JETS", "PAVE", "ITA"]},
        "BTC-USD": "Crypto",
    }
    sleeve_weights = {"a": 0.35, "b": 0.25 if p22_active else 0.35,
                      "c": 0.10, "d": 0.20}
    rollup = {}
    for key, sleeve_wt in sleeve_weights.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if not trades: continue
        for h in trades[-1].get("holdings", []):
            ac = AC_MAP.get(h["etf"], "Other")
            rollup[ac] = rollup.get(ac, 0) + h["weight"] * sleeve_wt
    if p22_active:
        rollup["Emerging Mkts Equity"] = rollup.get("Emerging Mkts Equity", 0) + 0.10
    items = sorted(rollup.items(), key=lambda x: -x[1])

    colour_map = {
        "US Equity": ACCENT,
        "Intl Developed Equity": colors.HexColor("#0e7490"),
        "Emerging Mkts Equity": colors.HexColor("#dc2626"),
        "Real Estate": colors.HexColor("#0d9488"),
        "Commodities / Miners": colors.HexColor("#ca8a04"),
        "Bonds": GOOD,
        "Thematic": colors.HexColor("#7c3aed"),
        "Crypto": colors.HexColor("#f59e0b"),
        "Other": INK_FAINT,
    }

    data = [["ASSET CLASS", "WEIGHT"]]
    for name, w in items:
        col = colour_map.get(name, INK_FAINT)
        # Coloured square swatch + name in a 2-col inline table
        swatch = Table([[""]], colWidths=[8], rowHeights=[8],
                         style=TableStyle([
                             ("BACKGROUND", (0, 0), (-1, -1), col),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 0),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                         ]))
        label_row = Table([[swatch, col_p(name, INK, fontsize=8.5)]],
                            colWidths=[14, page_w * 0.65 - 20],
                            style=TableStyle([
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                            ]))
        data.append([
            label_row,
            col_p(fmt_pct(w, signed=False, dp=1), ACCENT,
                   fontname="Courier-Bold", fontsize=9.5, align=TA_RIGHT),
        ])
    t = Table(data, colWidths=[page_w * 0.65, page_w * 0.30])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_FAINT),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, BORDER_STRONG),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ZEBRA]),
    ]))
    return t


def build_section_pair(left_flows, right_flows, page_w, gap=12):
    """Two side-by-side sections — left and right are lists of flowables."""
    half = (page_w - gap) / 2
    left_cell = Table([[f] for f in left_flows], colWidths=[half],
                        style=TableStyle([
                            ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 0),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ]))
    right_cell = Table([[f] for f in right_flows], colWidths=[half],
                         style=TableStyle([
                             ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 0),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                         ]))
    return Table([[left_cell, "", right_cell]],
                   colWidths=[half, gap, half],
                   style=TableStyle([
                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                       ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                       ("TOPPADDING", (0, 0), (-1, -1), 0),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                       ("VALIGN", (0, 0), (-1, -1), "TOP"),
                   ]))


# ----- Main build ----------------------------------------------------------

def build(out_path: Path):
    (multi, overlay, sleeves, live_track, breadth_end_date,
      holdings_prices) = load_all()
    deployed_key, blend = get_deployed(multi, overlay, live_track)
    deployed_series = pd.Series(blend["equity"],
                                  index=pd.to_datetime(blend["dates"]))

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

    page_w_pts, page_h_pts = A4
    margin = 15 * mm
    body_w = page_w_pts - 2 * margin
    styles = _styles()

    story = []

    # Phase 28.6 — page hierarchy reorder.
    #
    # Old order put 8-year backtest charts on page 1 and the only content
    # that drives weekly action (activity, watchlist) on page 3. That
    # answers the FOURTH investor question ("is the strategy still
    # working?") before the FIRST ("do I need to do anything?"). Pure
    # structural reorder; no content changes in this phase.
    #
    # New order (CPM-grade weekly read):
    #   Page 1 — THE WEEKLY READ
    #     Hero strip (WTD, YTD, Sharpe, DD)
    #     ACTION THIS WEEK + WATCHLIST (the reason to open the file)
    #     REGIME PANEL (anchors the action items)
    #   Page 2 — POSITIONING
    #     CURRENT TARGET PORTFOLIO (holdings sized for $1M)
    #     ASSET CLASS EXPOSURE roll-up
    #     PER-SLEEVE YTD ATTRIBUTION chart
    #   Page 3 — BACKTEST CONTEXT (long-window conviction check)
    #     PERFORMANCE — TOTAL RETURN BY PERIOD table
    #     Cumulative equity vs SPY chart
    #     Drawdown chart
    #     PER-SLEEVE STANDALONE STATISTICS

    # ====================== PAGE 1 — THE WEEKLY READ ======================
    story.append(build_hero_strip(deployed_series, full_stats, body_w, styles,
                                    spy_series=spy_series))
    story.append(Spacer(1, 6 * mm))

    activity_left = section_header(
        "REBALANCE THIS WEEK",
        "Position changes from rebalances in the past 7 days, as % of total portfolio NAV. Trades from earlier weeks should already have been executed.",
        styles)
    activity_left.append(build_trades_table(sleeves, body_w / 2 - 6, styles,
                                              p22_active,
                                              as_of=deployed_series.index[-1]))
    watchlist_right = section_header(
        "WATCHLIST — APPROACHING THRESHOLDS",
        "Signal levels relative to the next regime change",
        styles)
    watchlist_right.append(build_watchlist(overlay, body_w / 2 - 6, styles,
                                              panel_end_date=breadth_end_date))
    story.append(build_section_pair(activity_left, watchlist_right, body_w))
    story.append(Spacer(1, 6 * mm))

    # Phase 28.7f — KeepTogether on every header+chart/table pair so
    # ReportLab never breaks a section title onto a page where its
    # content lives. Resolves the "title on one page, chart on the
    # next" artefact the user flagged.
    story.append(_kt(section_header(
        "WHAT DROVE THIS WEEK'S RETURN",
        "Per-position contribution to the deployed blend's 1-week move "
        "(effective NAV weight × ETF return; top 12 by absolute "
        "contribution; bars coloured by sleeve).",
        styles) + [
        chart_per_etf_attribution(sleeves, p22_active, holdings_prices,
                                    body_w, days_back=7),
    ]))
    story.append(Spacer(1, 6 * mm))

    story.append(_kt([
        build_regime_panel(overlay, body_w, styles,
                            panel_end_date=breadth_end_date),
    ]))

    story.append(PageBreak())

    # ====================== PAGE 2 — POSITIONING + BACKTEST ===============
    story.append(_kt(section_header(
        "CURRENT TARGET PORTFOLIO",
        "What to own today, sorted by effective weight — sized for a $1.0M portfolio",
        styles) + [
        build_holdings_table(sleeves, p22_active, body_w, styles),
    ]))
    story.append(Spacer(1, 6 * mm))

    rollup_left = section_header(
        "ASSET CLASS EXPOSURE",
        "Today's positions rolled up by broad asset class",
        styles)
    rollup_left.append(build_asset_class_rollup(sleeves, p22_active,
                                                    body_w / 2 - 6, styles))
    stats_right = section_header(
        "PER-SLEEVE STANDALONE STATISTICS",
        "Backtest stats for each sleeve in isolation, before blend weighting",
        styles)
    stats_right.append(build_sleeve_stats_table(sleeves, multi,
                                                    body_w / 2 - 6, styles))
    story.append(_kt([build_section_pair(rollup_left, stats_right, body_w)]))
    story.append(Spacer(1, 6 * mm))

    ytd_start = pd.Timestamp(deployed_series.index[-1].year, 1, 1)
    story.append(_kt(section_header(
        "WHAT DROVE YTD RETURN",
        "Per-position contribution to the deployed blend's year-to-date "
        "move (effective NAV weight × ETF YTD return; top 12). "
        "Approximation — weights change at weekly rebalances.",
        styles) + [
        chart_per_etf_attribution(sleeves, p22_active, holdings_prices,
                                    body_w, ytd_start=ytd_start),
    ]))
    story.append(Spacer(1, 6 * mm))

    story.append(_kt(section_header(
        "PERFORMANCE — TOTAL RETURN BY PERIOD",
        "Strategy returns vs SPY (US large-cap) benchmark, USD-denominated",
        styles) + [
        build_returns_table(deployed_series, spy_series, body_w, styles),
    ]))
    story.append(Spacer(1, 6 * mm))

    # Phase 28.7d — dual-panel sliced to YTD to match the YTD attribution
    # above. Long-window (since-inception) view remains on the dashboard
    # for conviction checks; the weekly factsheet now leads with the
    # "this year" narrative end-to-end on page 2.
    ytd_series = deployed_series[deployed_series.index >= ytd_start]
    ytd_spy = (spy_series[spy_series.index >= ytd_start]
                if spy_series is not None else None)
    story.append(_kt([
        chart_performance_dual(ytd_series, ytd_spy, overlay, body_w),
    ]))

    # Phase 28.7f — fund parameters + data provenance footer fills any
    # last-page whitespace with useful reference content (rules the
    # strategy actually runs + the date stamps every published number
    # traces back to). Pre-fix, the last page was mostly empty.
    story.extend(build_parameters_footer(
        overlay, sleeves, p22_active, breadth_end_date, body_w, styles,
    ))

    # Build doc with custom canvas for headers/footers
    doc = BaseDocTemplate(str(out_path), pagesize=A4,
                            leftMargin=margin, rightMargin=margin,
                            topMargin=18 * mm, bottomMargin=14 * mm,
                            title="USD Multi-Strategy ETF Portfolio — Weekly Factsheet",
                            author="phuazz/breadth-thrust-etf",
                            subject=f"Weekly factsheet, as of {asof_str}")
    frame = Frame(margin, 14 * mm,
                    page_w_pts - 2 * margin,
                    page_h_pts - 18 * mm - 14 * mm,
                    leftPadding=0, rightPadding=0,
                    topPadding=0, bottomPadding=0,
                    showBoundary=0)
    doc.addPageTemplates([PageTemplate(id="default", frames=[frame])])

    def canvas_maker(*args, **kwargs):
        return _PageCanvas(*args, asof_str=asof_str,
                             computed_at=computed_at, **kwargs)

    doc.build(story, canvasmaker=canvas_maker)

    print(f"Wrote {out_path.relative_to(ROOT)}")
    print(f"  Deployed key: {deployed_key}")
    print(f"  As of:        {asof_str}")
    print(f"  PDF size:     {out_path.stat().st_size:,} bytes")

    # ----- Dated archive copy ---------------------------------------------
    # The stable file (default factsheet_latest.pdf) is what is committed
    # for the public Pages URL. We ALSO emit a date-stamped sibling using
    # the signals as-of date (not today's date — what matters is which
    # trading day the positions reflect). This dated copy is what the
    # weekly email attaches so recipients get a properly-named archive
    # in their inbox.
    asof_iso = asof_date.strftime("%Y-%m-%d")
    dated_path = out_path.with_name(f"factsheet_{asof_iso}.pdf")
    if dated_path != out_path:
        dated_path.write_bytes(out_path.read_bytes())
        print(f"  Dated copy:   {dated_path.relative_to(ROOT)}")

    # ----- Single-source-of-truth meta file ------------------------------
    # The GitHub Actions email step needs to know WHICH dated PDF to
    # attach. Computing the asof date in the workflow (by re-reading the
    # JSONs) is fragile — the live-track splice changes the answer in a
    # way the YAML cannot easily replicate. So write the truth here.
    meta_path = out_path.with_name("factsheet_meta.json")
    meta = {
        "asof_iso": asof_iso,
        "asof_pretty": asof_str,
        "deployed_key": deployed_key,
        "computed_at_utc": computed_at,
        "latest_pdf": out_path.name,
        "dated_pdf": dated_path.name,
        "dated_pdf_path": str(dated_path.relative_to(ROOT)).replace("\\", "/"),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  Meta:         {meta_path.relative_to(ROOT)}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DOCS_DIR / "factsheet_latest.pdf"))
    args = p.parse_args()
    return build(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
