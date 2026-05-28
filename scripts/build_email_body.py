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
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

DEPLOYED_KEY_PREFERENCE = [
    "blend_35_35_10_20_gated_eem_tilted",
    "blend_35_35_10_20_gated",
    "blend_35_35_10_20",
]


def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _get_deployed_series(multi, overlay):
    """Return (deployed_key, dates, equity) for the live blend."""
    # Try overlay's gated_variants first (where the live deployed series lives)
    if overlay and "gated_variants" in overlay:
        for key in DEPLOYED_KEY_PREFERENCE:
            if key in overlay["gated_variants"]:
                s = overlay["gated_variants"][key]
                return key, s["dates"], s["equity"]
    # Fall back to multi_strategy
    for key in DEPLOYED_KEY_PREFERENCE:
        if key in multi["strategies"]:
            s = multi["strategies"][key]
            return key, s["dates"], s["equity"]
    # Last resort — any blend key
    key = next(iter(multi["strategies"]))
    s = multi["strategies"][key]
    return key, s["dates"], s["equity"]


def _period_return(series: pd.Series, days: int):
    """Total return over the most recent `days` trading days, or None."""
    if len(series) < days + 1:
        return None
    return series.iloc[-1] / series.iloc[-1 - days] - 1.0


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


def _fmt_pct(x, signed=True, dp=2):
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return "n/a"
    fmt = f"{{:+.{dp}f}}%" if signed else f"{{:.{dp}f}}%"
    return fmt.format(x * 100)


def _regime_state(overlay):
    if not overlay:
        return ("UNKNOWN", "—")
    state = overlay.get("current_state", "UNKNOWN")
    since = overlay.get("current_state_since", "—")
    return (state, since)


def _eem_tilt_state(overlay):
    if not overlay or "phase22_eem_tilt" not in overlay:
        return ("DISABLED", "—", None)
    p22 = overlay["phase22_eem_tilt"]
    state = p22.get("current_state", "UNKNOWN")
    since = p22.get("current_state_since", "—")
    ratio = p22.get("current_ratio")
    return (state, since, ratio)


def _collect_holdings(sleeves, p22_active):
    """Mirror build_factsheet.build_holdings_table — return top-N list."""
    sleeve_weights = {
        "a": 0.35,
        "b": 0.25 if p22_active else 0.35,
        "c": 0.10,
        "d": 0.20,
    }
    letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    holdings = []
    for key, sleeve_wt in sleeve_weights.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if not trades:
            continue
        for h in trades[-1].get("holdings", []):
            eff = h.get("weight", 0) * sleeve_wt
            holdings.append({
                "etf": h.get("etf"),
                "sleeve": letter[key],
                "effective": eff,
            })
    if p22_active:
        holdings.append({"etf": "EEM", "sleeve": "TILT", "effective": 0.10})
    return sorted(holdings, key=lambda x: -x["effective"])


def _collect_activity(sleeves):
    """Identify ENTER/EXIT/RESIZE moves from prior rebalance to current."""
    letter = {"a": "A", "b": "B", "c": "C", "d": "D"}
    rows = []
    for key, sleeve in letter.items():
        s = sleeves.get(key, {})
        trades = s.get("headline", {}).get("trade_history", [])
        if len(trades) < 2:
            continue
        prev_h = {h["etf"]: h["weight"] for h in trades[-2].get("holdings", [])}
        curr_h = {h["etf"]: h["weight"] for h in trades[-1].get("holdings", [])}
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
    return rows


def _regime_colour(state):
    return {
        "RISK_ON": "#1d7a3a",
        "RISK_OFF": "#b3261e",
        "DERISK": "#b76e00",
        "EM_TILT_ON": "#1351b4",
        "EM_TILT_OFF": "#7c8590",
    }.get(state, "#3a4148")


def build_html(out_path: Path):
    multi = _load_json(DATA_DIR / "multi_strategy.json")
    overlay = _load_json(DATA_DIR / "risk_overlay.json")
    sleeves = {}
    for key, fname in [("a", "topk_robustness.json"),
                        ("b", "asset_class_rotation.json"),
                        ("c", "thematic_rotation.json"),
                        ("d", "europe_rotation.json")]:
        d = _load_json(DATA_DIR / fname)
        if d:
            sleeves[key] = d

    deployed_key, dates, equity = _get_deployed_series(multi, overlay)
    series = pd.Series(equity, index=pd.to_datetime(dates))
    asof = series.index[-1]
    asof_str = asof.strftime("%d %B %Y")
    asof_iso = asof.strftime("%Y-%m-%d")

    # Headline stats
    ytd = _ytd_return(series)
    r1y = _period_return(series, 252)
    sharpe = _sharpe_full(series)
    mdd = _max_drawdown(series)

    # Regime + tilt state
    regime_state, regime_since = _regime_state(overlay)
    tilt_state, tilt_since, tilt_ratio = _eem_tilt_state(overlay)
    p22_active = tilt_state == "EM_TILT_ON"

    # Holdings + activity
    holdings = _collect_holdings(sleeves, p22_active)
    activity = _collect_activity(sleeves)

    # Sleeve weights for allocation summary
    if p22_active:
        alloc = "A 35% · B 25% · C 10% · D 20% · EEM tilt 10%"
    else:
        alloc = "A 35% · B 35% · C 10% · D 20%"

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
        f'Weekly factsheet · signals as of <strong style="color:#fff;">'
        f'{asof_str}</strong></div></div>'
    )

    # Top-line summary
    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               'Current state</h3>')
    out.append('<table style="width:100%;border-collapse:collapse;'
               'margin-bottom:18px;font-size:13px;">')
    out.append(
        f'<tr><td style="padding:6px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;width:42%;">Breadth regime (Phase 19)</td>'
        f'<td style="padding:6px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;">'
        f'<strong style="color:{_regime_colour(regime_state)};">'
        f'{regime_state}</strong> '
        f'<span style="color:#7c8590;">since {regime_since}</span></td></tr>'
    )
    tilt_ratio_str = f" (ratio {tilt_ratio:.3f})" if tilt_ratio else ""
    out.append(
        f'<tr><td style="padding:6px 10px;border:1px solid #e1e4e8;">'
        f'EEM/SPY tilt (Phase 22)</td>'
        f'<td style="padding:6px 10px;border:1px solid #e1e4e8;">'
        f'<strong style="color:{_regime_colour(tilt_state)};">'
        f'{tilt_state}</strong> '
        f'<span style="color:#7c8590;">since {tilt_since}{tilt_ratio_str}'
        f'</span></td></tr>'
    )
    out.append(
        f'<tr><td style="padding:6px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;">Target allocation</td>'
        f'<td style="padding:6px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;font-family:Courier,monospace;'
        f'font-size:12px;">{alloc}</td></tr>'
    )
    out.append('</table>')

    # Headline stats
    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               'Headline backtest stats</h3>')
    out.append('<table style="width:100%;border-collapse:collapse;'
               'margin-bottom:18px;font-size:13px;">')
    sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "n/a"
    out.append(
        f'<tr>'
        f'<td style="padding:8px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;width:25%;text-align:center;">'
        f'<div style="color:#7c8590;font-size:10px;text-transform:uppercase;'
        f'letter-spacing:0.5px;">YTD</div>'
        f'<div style="font-weight:600;font-size:15px;color:#0f1217;">'
        f'{_fmt_pct(ytd, signed=True, dp=1)}</div></td>'
        f'<td style="padding:8px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;width:25%;text-align:center;">'
        f'<div style="color:#7c8590;font-size:10px;text-transform:uppercase;'
        f'letter-spacing:0.5px;">1Y</div>'
        f'<div style="font-weight:600;font-size:15px;color:#0f1217;">'
        f'{_fmt_pct(r1y, signed=True, dp=1)}</div></td>'
        f'<td style="padding:8px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;width:25%;text-align:center;">'
        f'<div style="color:#7c8590;font-size:10px;text-transform:uppercase;'
        f'letter-spacing:0.5px;">Sharpe</div>'
        f'<div style="font-weight:600;font-size:15px;color:#0f1217;">'
        f'{sharpe_str}</div></td>'
        f'<td style="padding:8px 10px;background:#f7f8fa;'
        f'border:1px solid #e1e4e8;width:25%;text-align:center;">'
        f'<div style="color:#7c8590;font-size:10px;text-transform:uppercase;'
        f'letter-spacing:0.5px;">Max DD</div>'
        f'<div style="font-weight:600;font-size:15px;color:#b3261e;">'
        f'{_fmt_pct(mdd, signed=True, dp=1)}</div></td>'
        f'</tr></table>'
    )

    # Top holdings
    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               'Top holdings on $1.0M</h3>')
    out.append('<table style="width:100%;border-collapse:collapse;'
               'margin-bottom:18px;font-size:13px;">')
    out.append(
        '<tr style="color:#7c8590;font-size:10px;text-transform:uppercase;'
        'letter-spacing:0.5px;">'
        '<th style="text-align:left;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">Ticker</th>'
        '<th style="text-align:left;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">Sleeve</th>'
        '<th style="text-align:right;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">Weight</th>'
        '<th style="text-align:right;padding:4px 10px;'
        'border-bottom:1px solid #c8ccd2;">$ Notional</th></tr>'
    )
    for h in holdings[:8]:
        cash = h["effective"] * 1_000_000
        out.append(
            f'<tr><td style="padding:5px 10px;font-family:Courier,monospace;'
            f'font-weight:600;border-bottom:1px solid #f0f2f4;">{h["etf"]}</td>'
            f'<td style="padding:5px 10px;color:#3a4148;'
            f'border-bottom:1px solid #f0f2f4;">{h["sleeve"]}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:#1351b4;'
            f'font-weight:600;border-bottom:1px solid #f0f2f4;">'
            f'{h["effective"] * 100:.1f}%</td>'
            f'<td style="padding:5px 10px;text-align:right;'
            f'font-family:Courier,monospace;border-bottom:1px solid #f0f2f4;">'
            f'${cash:,.0f}</td></tr>'
        )
    out.append('</table>')

    # Activity
    out.append('<h3 style="margin:0 0 10px 0;font-size:14px;'
               'color:#3a4148;text-transform:uppercase;letter-spacing:1px;">'
               'Activity since last rebalance</h3>')
    if not activity:
        out.append('<p style="color:#7c8590;font-style:italic;'
                   'margin-bottom:18px;">'
                   'No position changes — strategy stable since last week.</p>')
    else:
        out.append('<table style="width:100%;border-collapse:collapse;'
                   'margin-bottom:18px;font-size:13px;">')
        out.append(
            '<tr style="color:#7c8590;font-size:10px;text-transform:uppercase;'
            'letter-spacing:0.5px;">'
            '<th style="text-align:left;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Sleeve</th>'
            '<th style="text-align:left;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Action</th>'
            '<th style="text-align:left;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Ticker</th>'
            '<th style="text-align:right;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">Prior</th>'
            '<th style="text-align:right;padding:4px 10px;'
            'border-bottom:1px solid #c8ccd2;">New</th></tr>'
        )
        action_colour = {"ENTER": "#1d7a3a", "EXIT": "#b3261e",
                          "RESIZE": "#b76e00"}
        for sleeve, action, etf, prev_w, new_w in activity[:10]:
            prev_str = f"{prev_w * 100:.0f}%" if prev_w is not None else "—"
            new_str = f"{new_w * 100:.0f}%" if new_w is not None else "—"
            out.append(
                f'<tr><td style="padding:5px 10px;color:#3a4148;'
                f'border-bottom:1px solid #f0f2f4;">{sleeve}</td>'
                f'<td style="padding:5px 10px;font-weight:600;'
                f'color:{action_colour.get(action, "#3a4148")};'
                f'border-bottom:1px solid #f0f2f4;">{action}</td>'
                f'<td style="padding:5px 10px;font-family:Courier,monospace;'
                f'font-weight:600;border-bottom:1px solid #f0f2f4;">{etf}</td>'
                f'<td style="padding:5px 10px;text-align:right;'
                f'font-family:Courier,monospace;color:#7c8590;'
                f'border-bottom:1px solid #f0f2f4;">{prev_str}</td>'
                f'<td style="padding:5px 10px;text-align:right;'
                f'font-family:Courier,monospace;'
                f'border-bottom:1px solid #f0f2f4;">{new_str}</td></tr>'
            )
        out.append('</table>')

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
        f'font-size:10px;">{deployed_key}</code>.'
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
    print(f"  Activity:     {len(activity)} changes")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(DOCS_DIR / "email_body.html"))
    args = p.parse_args()
    return build_html(Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
