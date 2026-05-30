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

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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
    return multi, overlay, sleeves, live_track


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
        "section": ParagraphStyle(
            "section", parent=base, fontName="Helvetica-Bold",
            fontSize=9, leading=11, textColor=INK,
            spaceBefore=0, spaceAfter=2),
        "section_sub": ParagraphStyle(
            "section_sub", parent=base, fontName="Helvetica-Oblique",
            fontSize=7.5, leading=10, textColor=INK_FAINT,
            spaceBefore=0, spaceAfter=4),
        "body": ParagraphStyle("body", parent=base, fontName="Helvetica",
                                 fontSize=8.5, leading=11, textColor=INK),
        "kpi_label": ParagraphStyle(
            "kpi_label", parent=base, fontName="Helvetica-Bold",
            fontSize=7, leading=9, textColor=INK_FAINT,
            alignment=TA_CENTER, spaceBefore=0, spaceAfter=2),
        "kpi_sub": ParagraphStyle(
            "kpi_sub", parent=base, fontName="Helvetica",
            fontSize=7, leading=9, textColor=INK_FAINT,
            alignment=TA_CENTER, spaceBefore=2, spaceAfter=0),
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


def chart_cumulative(deployed_series, spy_series, overlay, width_pts):
    fig, ax = plt.subplots(figsize=(8, 3), facecolor="white")
    s = deployed_series / deployed_series.iloc[0]
    ax.plot(s.index, (s - 1) * 100, color="#1351b4", linewidth=1.8,
             label="Strategy", zorder=3)
    if spy_series is not None:
        spy = spy_series.reindex(s.index, method="ffill").dropna()
        if len(spy) > 5:
            spy = spy / spy.iloc[0]
            ax.plot(spy.index, (spy - 1) * 100, color="#7c8590",
                     linewidth=1.0, linestyle=(0, (4, 3)),
                     label="SPY benchmark", zorder=2)
    if overlay and overlay.get("events"):
        off_start = None
        for ev in overlay["events"]:
            if ev["direction"] == "RISK_OFF":
                off_start = pd.to_datetime(ev["date"])
            elif ev["direction"] == "RISK_ON" and off_start is not None:
                ax.axvspan(off_start, pd.to_datetime(ev["date"]),
                            color="#b76e00", alpha=0.10, zorder=1)
                off_start = None
        if off_start is not None:
            ax.axvspan(off_start, s.index[-1], color="#b76e00",
                        alpha=0.10, zorder=1)
    ax.set_title("Cumulative return since inception (USD)",
                  fontsize=10, fontweight="600", color="#0f1217", loc="left", pad=8)
    ax.tick_params(labelsize=8.5, colors="#3a4148")
    ax.grid(True, color="#e1e4e8", linewidth=0.5, axis="y", alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color("#c8ccd2")
    ax.spines["left"].set_linewidth(0.6); ax.spines["bottom"].set_linewidth(0.6)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    return _chart_to_image(fig, width_pts)


def chart_drawdown(deployed_series, spy_series, width_pts):
    fig, ax = plt.subplots(figsize=(8, 2.3), facecolor="white")
    s = deployed_series / deployed_series.iloc[0]
    dd = ((s - s.cummax()) / s.cummax()) * 100
    ax.fill_between(dd.index, dd.values, 0, color="#b3261e", alpha=0.18, linewidth=0)
    ax.plot(dd.index, dd.values, color="#b3261e", linewidth=1.2, label="Strategy")
    if spy_series is not None:
        spy = spy_series.reindex(s.index, method="ffill").dropna()
        if len(spy) > 5:
            spy = spy / spy.iloc[0]
            spy_dd = ((spy - spy.cummax()) / spy.cummax()) * 100
            ax.plot(spy_dd.index, spy_dd.values, color="#7c8590",
                     linewidth=0.9, linestyle=(0, (4, 3)), label="SPY")
    ax.set_title("Drawdown from peak", fontsize=10, fontweight="600",
                  color="#0f1217", loc="left", pad=8)
    ax.tick_params(labelsize=8.5, colors="#3a4148")
    ax.grid(True, color="#e1e4e8", linewidth=0.5, axis="y", alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color("#c8ccd2")
    ax.spines["left"].set_linewidth(0.6); ax.spines["bottom"].set_linewidth(0.6)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.legend(loc="lower left", fontsize=9, frameon=False)
    return _chart_to_image(fig, width_pts)


def chart_sleeve_attribution(sleeves, deployed_series, p22_active, width_pts):
    fig, ax = plt.subplots(figsize=(8, 2.5), facecolor="white")
    last_date = deployed_series.index[-1]
    ytd_start = pd.Timestamp(last_date.year, 1, 1)
    sleeve_meta = [
        ("a", "US Sectors (A)",    "#1351b4", 0.35),
        ("b", "Asset Class (B)",   "#1d7a3a", 0.25 if p22_active else 0.35),
        ("c", "Thematic (C)",      "#dc2626", 0.10),
        ("d", "Europe (D)",        "#0e7490", 0.20),
    ]
    names, contribs, colours = [], [], []
    for key, label, col, wt in sleeve_meta:
        s = sleeves.get(key, {})
        blob = s.get("headline", {})
        dates = blob.get("headline_equity_dates"); equity = blob.get("headline_equity")
        ret = window_ret(pd.Series(equity, index=pd.to_datetime(dates)),
                          ytd_start) if (dates and equity) else None
        names.append(label)
        contribs.append((ret or 0) * wt)
        colours.append(col)
    if p22_active:
        names.append("EEM Tilt"); contribs.append(0); colours.append("#b76e00")

    y_pos = np.arange(len(names))
    ax.barh(y_pos, [c * 100 for c in contribs], color=colours,
             edgecolor="white", linewidth=1, height=0.55)
    for i, c in enumerate(contribs):
        x = c * 100
        offset = 0.5 if x >= 0 else -0.5
        ha = "left" if x >= 0 else "right"
        ax.text(x + offset, i, f"{c*100:+.1f}pp",
                 fontsize=8.5, color="#3a4148", fontweight="600",
                 va="center", ha=ha)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(names, fontsize=9, color="#0f1217")
    ax.invert_yaxis()
    ax.axvline(0, color="#7c8590", linewidth=0.6)
    ax.set_title("Sleeve contribution to YTD return",
                  fontsize=10, fontweight="600", color="#0f1217", loc="left", pad=8)
    ax.tick_params(axis="x", labelsize=8, colors="#7c8590")
    ax.tick_params(axis="y", pad=4)
    ax.grid(True, color="#e1e4e8", linewidth=0.4, axis="x", alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ("top", "right"): ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"): ax.spines[sp].set_color("#e1e4e8")
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
    return [Paragraph(title, styles["section"]),
             Paragraph(sub, styles["section_sub"])]


def build_hero_strip(deployed_series, full_stats, page_w, styles):
    """Four KPI cells in one row."""
    last_date = deployed_series.index[-1]
    wk_ret = window_ret(deployed_series, last_date - pd.Timedelta(days=7))
    ytd_ret = window_ret(deployed_series, pd.Timestamp(last_date.year, 1, 1))
    cells = [
        ("THIS WEEK", fmt_pct(wk_ret), colour_for(wk_ret), "Latest 7-day return"),
        ("YEAR TO DATE", fmt_pct(ytd_ret), colour_for(ytd_ret),
         f"Since 1 Jan {last_date.year}"),
        ("SINCE INCEPTION", fmt_pct(full_stats["total"]) if full_stats else "—",
         GOOD if (full_stats and full_stats["total"] > 0) else BAD,
         f"From {deployed_series.index[0].strftime('%b %Y')}"),
        ("MAX DRAWDOWN", fmt_pct(full_stats["dd"]) if full_stats else "—",
         BAD, "Worst peak-to-trough"),
    ]
    # Each cell: 3-row stack (label, value, sub)
    cell_tables = []
    for label, value, colour, sub in cells:
        ct = Table([
            [Paragraph(label, styles["kpi_label"])],
            [col_p(value, colour, fontname="Helvetica-Bold", fontsize=20,
                    align=TA_CENTER)],
            [Paragraph(sub, styles["kpi_sub"])],
        ], colWidths=[page_w / 4 - 8], style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
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


def build_regime_panel(overlay, page_w, styles):
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

    data = [["TICKER", "SLEEVE", "SIGNAL", "TARGET WT", "$ ON $1.0M"]]
    for h in holdings[:14]:
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


def build_trades_table(sleeves, page_w, styles):
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

    if not rows:
        return Paragraph(
            "<i>No position changes this week — strategy stable.</i>",
            styles["body"])

    data = [["SLEEVE", "ACTION", "TICKER", "PRIOR", "NEW", "Δ"]]
    for sleeve, action, etf, prev_w, new_w in rows[:8]:
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
    t = Table(data, colWidths=[page_w * 0.12, page_w * 0.18, page_w * 0.18,
                                  page_w * 0.16, page_w * 0.16, page_w * 0.20])
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


def build_watchlist(overlay, page_w, styles):
    items = []
    if overlay:
        cur_breadth = overlay.get("current_breadth", 0) * 100
        off_thresh = overlay.get("gate_parameters", {}).get("off_threshold", 0.20) * 100
        on_thresh = overlay.get("gate_parameters", {}).get("on_threshold", 0.50) * 100
        state = overlay.get("current_state")
        if state == "RISK_ON":
            margin = cur_breadth - off_thresh
            items.append({
                "label": "S&P 500 breadth", "value": f"{cur_breadth:.0f}%",
                "trigger": f"De-risk if breadth < {off_thresh:.0f}%",
                "margin": f"+{margin:.0f}pp buffer",
                "status": "ARMED" if margin > 10 else "NEAR",
                "status_col": GOOD if margin > 10 else WARN,
            })
        else:
            margin = on_thresh - cur_breadth
            items.append({
                "label": "S&P 500 breadth", "value": f"{cur_breadth:.0f}%",
                "trigger": f"Re-engage if breadth > {on_thresh:.0f}%",
                "margin": f"needs +{margin:.0f}pp",
                "status": "DE-RISKED", "status_col": BAD,
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
    sleeve_rows = [
        ("a", "Strategy A — US Sectors", "strategy_a"),
        ("b", "Strategy B — Asset Class", "strategy_b"),
        ("c", "Strategy C — Thematic",    "strategy_c"),
        ("d", "Strategy D — Europe",      "strategy_d"),
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
        data.append([
            col_p(label, INK, fontsize=8.5),
            col_p(fmt_num(sharpe) if sharpe else "—", colour_for(sharpe),
                   fontname="Courier-Bold", fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(cagr) if cagr else "—", colour_for(cagr),
                   fontname="Courier", fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(dd) if dd else "—", BAD,
                   fontname="Courier", fontsize=9, align=TA_RIGHT),
            col_p(fmt_pct(ytd) if ytd is not None else "—",
                   colour_for(ytd), fontname="Courier-Bold",
                   fontsize=9, align=TA_RIGHT),
        ])
    t = Table(data, colWidths=[page_w * 0.40, page_w * 0.15,
                                  page_w * 0.15, page_w * 0.15, page_w * 0.15])
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
    multi, overlay, sleeves, live_track = load_all()
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

    # ====================== PAGE 1 ======================
    story.append(build_hero_strip(deployed_series, full_stats, body_w, styles))
    story.append(Spacer(1, 6 * mm))

    story.extend(section_header(
        "PERFORMANCE — TOTAL RETURN BY PERIOD",
        "Strategy returns vs SPY (US large-cap) benchmark, USD-denominated",
        styles))
    story.append(build_returns_table(deployed_series, spy_series, body_w, styles))
    story.append(Spacer(1, 6 * mm))

    story.append(chart_cumulative(deployed_series, spy_series, overlay, body_w))
    story.append(Spacer(1, 3 * mm))
    story.append(chart_drawdown(deployed_series, spy_series, body_w))

    story.append(PageBreak())

    # ====================== PAGE 2 ======================
    story.append(build_regime_panel(overlay, body_w, styles))
    story.append(Spacer(1, 6 * mm))

    story.extend(section_header(
        "CURRENT TARGET PORTFOLIO",
        "What to own today, sorted by effective weight — sized for a $1.0M portfolio",
        styles))
    story.append(build_holdings_table(sleeves, p22_active, body_w, styles))
    story.append(Spacer(1, 6 * mm))

    activity_left = section_header(
        "ACTIVITY THIS WEEK",
        "Position changes from the previous rebalance", styles)
    activity_left.append(build_trades_table(sleeves, body_w / 2 - 6, styles))
    watchlist_right = section_header(
        "WATCHLIST — APPROACHING THRESHOLDS",
        "Signal levels relative to next regime change", styles)
    watchlist_right.append(build_watchlist(overlay, body_w / 2 - 6, styles))
    story.append(build_section_pair(activity_left, watchlist_right, body_w))
    story.append(Spacer(1, 6 * mm))

    story.append(chart_sleeve_attribution(sleeves, deployed_series,
                                              p22_active, body_w))
    story.append(Spacer(1, 6 * mm))

    stats_left = section_header(
        "PER-SLEEVE STANDALONE STATISTICS",
        "Backtest stats for each sleeve in isolation, before blend weighting",
        styles)
    stats_left.append(build_sleeve_stats_table(sleeves, multi,
                                                   body_w / 2 - 6, styles))
    rollup_right = section_header(
        "ASSET CLASS EXPOSURE",
        "Today's deployed positions rolled up by broad asset class",
        styles)
    rollup_right.append(build_asset_class_rollup(sleeves, p22_active,
                                                     body_w / 2 - 6, styles))
    story.append(build_section_pair(stats_left, rollup_right, body_w))

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
