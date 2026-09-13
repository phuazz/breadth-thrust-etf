"""Reader-facing factsheet from a verified release, never from mutable live data.

Python datetime months are 1-indexed. No fetching, ranking or delivery here.
The email is a short dossier; the PDF is its complete reference edition.
"""
from datetime import datetime
from html import escape
from io import BytesIO
import json
import math
import subprocess

from component_publication import email_wording

NAMES = {"A": "US sectors", "B": "Asset classes", "C": "Thematic",
         "D": "Europe sectors", "TILT": "EM tilt", "GATE": "Defensive allocation"}
DISCLAIMER = ("Personal research artefact. Not investment advice and not affiliated with any regulated fund or manager. "
              "Past simulated performance is not indicative of future returns. Model results are not broker execution records.")


def pct(value, signed=False, dp=2):
    if value is None:
        return "Unavailable"
    return f"{value * 100:+.{dp}f}%" if signed else f"{value * 100:.{dp}f}%"


def pp(value):
    # Do not label a non-zero, sub-display-precision change as a zero trade.
    return f"{value*100:+.6f}".rstrip('0') + "pp" if 0 < abs(value) < .00005 else f"{value*100:+.2f}pp"


def long_date(iso):
    return datetime.fromisoformat(iso).strftime("%a %d %b %Y") if iso else "Unavailable"


def exact_return(dates, values, start, end):
    """Never shorten a window or forward-fill a missing endpoint."""
    if not dates or not values or len(dates) != len(values) or len(set(dates)) != len(dates):
        return None
    if not start or not end or start >= end or start not in dates or end not in dates:
        return None
    a, b = values[dates.index(start)], values[dates.index(end)]
    if a is None or b is None or not all(math.isfinite(float(v)) and float(v) > 0 for v in (a, b)):
        return None
    return float(b) / float(a) - 1


def context_from_sources(release, reader):
    """Supporting facts are read from the same hashed sources as the release."""
    from build_email_body import _sleeve_series
    book, stats = release["book"], release["performance"]
    start, end = stats["wtd_start"], stats["as_of"]
    weights = book["overlay_decision"]["weights"]
    files = dict(A="topk_robustness.json", B="asset_class_rotation.json",
                 C="thematic_rotation.json", D="europe_rotation.json")
    rows = []
    for sleeve, filename in files.items():
        series = _sleeve_series(reader("data/" + filename))
        ret = None if series is None else exact_return(
            [str(d.date()) for d in series.index], list(series.values), start, end)
        weight = weights[sleeve.lower()]
        rows.append(dict(sleeve=sleeve, weight=weight, ret=ret,
                         contribution=None if ret is None else weight * ret))
    prices = reader("data/holdings_prices_1y.json").get("prices", {})
    for sleeve, key, weight_key in (("TILT", "EEM", "tilt_nav"), ("GATE", "SHY", "shy_overlay")):
        weight = weights.get(weight_key, 0)
        if not weight:
            continue
        entry = prices.get(key, {})
        ret = exact_return(entry.get("dates"), entry.get("prices"), start, end)
        rows.append(dict(sleeve=sleeve, weight=weight, ret=ret,
                         contribution=None if ret is None else weight * ret))
    covered = all(r["contribution"] is not None or r["weight"] == 0 for r in rows)
    total = math.fsum(r["contribution"] for r in rows if r["contribution"] is not None)
    overlay = reader("data/risk_overlay.json")
    watchlist=[]
    parameters=overlay.get('gate_parameters',{})
    if all(k in parameters for k in ('off_threshold','on_threshold')):
        watchlist.append("Breadth rule: de-risk below "
                         f"{pct(parameters['off_threshold'],dp=0)}; re-engage above {pct(parameters['on_threshold'],dp=0)}.")
    tilt=overlay.get('phase22_eem_tilt',{})
    if tilt.get('signal_as_of')==book['as_of'] and all(tilt.get(k) is not None for k in ('current_fast_ma','current_slow_ma')):
        params=tilt.get('parameters',{})
        if params.get('fast_ma') and params.get('slow_ma'):
            watchlist.append(f"EM/reference ratio averages: {params['fast_ma']}-day {tilt['current_fast_ma']:.4f}; "
                             f"{params['slow_ma']}-day {tilt['current_slow_ma']:.4f}. The tilt follows their crossover.")
    from etf_registry import get_etf
    holding_returns = []
    for line in book["lines"]:
        if line["held"] <= 0:
            continue
        try:
            proxy = get_etf(line["etf"]).get("yfinance_trading_proxy") or line["etf"]
        except KeyError:
            proxy = line["etf"]
        entry = prices.get(proxy) or prices.get(line["etf"], {})
        ret = exact_return(entry.get("dates"), entry.get("prices"), start, end)
        holding_returns.append({**line, "ret": ret, "price_key": proxy})
    return {"start": start, "end": end, "attribution": rows, "coverage_complete": covered,
            "attribution_sum": total, "residual": stats["values"]["WTD"] - total
            if covered and stats["values"].get("WTD") is not None else None,
            # current_breadth can describe the blend's common history end,
            # despite gate_feed_last_bar being newer. Do not label that scalar
            # as a verified current gate observation. The book owns gate state.
            "holding_returns": holding_returns,
            "watchlist":watchlist}


def verified_context(root, release, committed=False):
    from component_release import MANIFEST, digest, read
    revision = None
    if committed:
        revision = subprocess.run(["git", "log", "-1", "--format=%H", "--", MANIFEST],
                                  cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        if not revision:
            raise ValueError("presentation requires a committed release")
    def reader(name):
        value = (json.loads(subprocess.run(["git", "show", f"{revision}:{name}"], cwd=root,
                 check=True, capture_output=True).stdout) if revision else read(root / name))
        if digest(value) != release["sources"].get(name):
            raise ValueError(f"presentation source differs from sealed snapshot: {name}")
        return value
    return context_from_sources(release, reader)


def rationale(row, book):
    if row.get("risk_adjustment"):
        return "Portfolio-risk adjustment only; selection unchanged."
    sleeve = next((s for s in book["sleeves"] if s["sleeve"] == row["sleeve"]), {})
    if sleeve.get("status") == "HOLD":
        return "Selection held pending complete data; no new ranking."
    if row["sleeve"] in ("TILT", "GATE"):
        return "Allocation follows the verified portfolio overlay."
    if abs(row["delta"]) <= 1e-8:
        return "No change to the model-held weight."
    signals, previous = sleeve.get("signals", {}), sleeve.get("signals_prev", {})
    key = row["etf"]
    if key not in signals:
        return "No comparable signal recorded; proposed weight shown without an inferred driver."
    ordered = sorted(signals, key=lambda k: (-signals[k], k))
    prev_order = sorted(previous, key=lambda k: (-previous[k], k))
    rank = str(ordered.index(key) + 1)
    if key in previous and prev_order.index(key)!=ordered.index(key):
        rank = f"{prev_order.index(key)+1} to {rank}"
    unit = {"breadth_relative": "Breadth versus sector average (pp)",
            "breadth": "Constituent breadth (%)", "ma_distance": "Price versus 200-day average (%)"}.get(
                sleeve.get("signal_kind"), "Recorded signal")
    before = f"{previous[key]*100:+.2f} to " if key in previous else ""
    return f"{unit}: {before}{signals[key]*100:+.2f}; rank {rank} of {len(signals)}."


def view_model(decision, release):
    book = release["book"]
    rows = sorted(book["lines"], key=lambda r: (-abs(r["delta"]), r["sleeve"], r["etf"]))
    changed = [r for r in rows if abs(r["delta"]) > 1e-8]
    return {"wording": email_wording(decision), "rows": rows, "changed": changed,
            "turnover": math.fsum(abs(r["delta"]) for r in rows)/2,
            "entries": sum(r["held"] == 0 and r["target"] > 0 for r in changed),
            "exits": sum(r["held"] > 0 and r["target"] == 0 for r in changed)}


def position_name(row, release):
    return f"{release['labels'].get(row['etf'], row['etf'])} ({row['traded']})"


def render_html(decision, release, include_unchanged=False):
    e, book, stats = escape, release["book"], release["performance"]
    v = view_model(decision, release)
    w, context = v["wording"], release.get("presentation", {})
    stage = "INITIAL REVIEW" if decision["action"] == "preview" else "D UPDATE" if decision["action"] == "d_update" else "WEEKLY FACTSHEET"
    parts = [f"<p class='eyebrow'>{stage} · {e(long_date(release['anchor']))}</p>",
             "<h1>USD Multi-Strategy ETF Portfolio</h1>",
             f"<div class='status'><h2>{e(w['heading'])}</h2><p>{e(w['difference'])}</p></div>"]
    if stats["series"].startswith("synthetic"):
        parts.insert(0, "<p><strong>SYNTHETIC NO-SEND REHEARSAL — not a live instruction.</strong></p>")
    if release.get("preview_only"):
        parts.insert(0, "<p><strong>DESIGN PREVIEW — already-sent snapshot; no new email or trade instruction.</strong></p>")
    parts += ["<h2>01 · The week in numbers</h2><div class='metrics'>"]
    for key in ("WTD", "YTD", "1Y", "Sharpe", "Max drawdown"):
        value = stats["values"].get(key)
        text = "Unavailable" if value is None else f"{value:.2f}" if key == "Sharpe" else pct(value, True)
        label = {"WTD": "This week", "1Y": "One year"}.get(key, key)
        parts.append(f"<div class='metric'><span>{e(label)}</span><strong>{e(text)}</strong></div>")
    parts += ["</div>", f"<p class='note'>Model valuation: {e(long_date(stats['as_of']))}. "
              f"Week: {e(stats['wtd_start'] or 'Unavailable')} to {e(stats['as_of'])}. "
              "YTD starts at the prior year-end close; 1Y is the trailing calendar year. "
              "Sharpe and maximum drawdown cover the full deployed-model history. Proposed trades are not included.</p>"]
    if decision['d_hold']:
        parts.append("<p><strong>Performance is provisional while D data is incomplete.</strong> "
                     "The full-portfolio figures may change when D completes; the A–C and overlay instructions are verified.</p>")
    if context.get("attribution"):
        parts.append("<h3>Return drivers by strategy</h3>")
        max_abs = max((abs(r["contribution"] or 0) for r in context["attribution"]), default=0) or 1
        for row in context["attribution"]:
            c = row["contribution"]
            text = "Unavailable" if c is None else pp(c)
            bar = 0 if c is None else abs(c)/max_abs*100
            parts.append(f"<div class='driver'><p>{e(NAMES[row['sleeve']])} <strong>{e(text)}</strong></p>"
                         f"<div class='track'><div style='height:6px;width:{bar:.2f}%;background:#55718e'></div></div></div>")
        residual = "Not calculated: at least one endpoint is missing." if context["residual"] is None else pp(context["residual"])
        parts.append(f"<p class='note'>Approximation: decision-date sleeve allocation × sleeve model return over "
                     f"{e(context['start'] or 'Unavailable')} to {e(context['end'])}. Not realised attribution. "
                     f"Difference from the blend: {e(residual)} No missing endpoint is filled.</p>")
    else:
        parts.append("<p class='note'>Return-driver detail is unavailable in this snapshot.</p>")
    priced = [r for r in context.get("holding_returns", []) if r["ret"] is not None]
    if priced:
        parts.append("<h3>Holding-price moves</h3>")
        for label, candidates in (("Strongest", sorted([r for r in priced if r['ret']>0], key=lambda r:-r['ret'])[:2]),
                                  ("Weakest", sorted([r for r in priced if r['ret']<0], key=lambda r:r['ret'])[:2])):
            if candidates:
                parts.append(f"<p><strong>{label}:</strong> " + "; ".join(
                    f"{e(position_name(r,release))}: {pct(r['ret'],True)}; model-held {pct(r['held'])} of NAV" for r in candidates) + "</p>")
        parts.append(f"<p class='note'>{len(priced)} of {len(context['holding_returns'])} model-held lines have both weekly endpoints. "
                     "Quote/proxy returns, not portfolio contributions; Europe FX is not added. The PDF identifies each price proxy.</p>")
    overlay = book["overlay_decision"]
    parts += ["<h2>02 · What changes next</h2>",
              f"<p><strong>{len(v['changed'])} changes · {pct(v['turnover'])} one-way turnover</strong><br>"
              f"New positions: {v['entries']} · Exits: {v['exits']}. Turnover is half the sum of absolute NAV-weight changes.</p>",
              "<p>Proposed positions, not executed trades. Held and target figures are percentages of total NAV; changes are percentage points. "
              "The held baseline is the model portfolio, not confirmation of broker holdings.</p>",
              f"<p><strong>Strategy D:</strong> {e(w['d_instruction'])}</p>"]
    shown = (sorted(v["rows"], key=lambda r: (-r["target"], r["sleeve"], r["etf"])) if include_unchanged else v["changed"][:6])
    if include_unchanged:
        parts.append("<h3>Complete proposed book</h3>")
    for row in shown:
        parts.append(f"<section class='position'><h3>{e(position_name(row,release))}</h3>"
                     f"<p>Strategy {e(row['sleeve'])} · Held {pct(row['held'])} → Target {pct(row['target'])} "
                     f"· Change {pp(row['delta'])}</p><p class='note'>{e(rationale(row,book))}</p></section>")
    if not shown:
        parts.append("<p>No position changes.</p>")
    if not include_unchanged:
        parts.append(f"<p class='note'>{len(shown)} of {len(v['changed'])} changes shown, ordered by size. "
                     "Every change and unchanged position is in the attached PDF and complete HTML book.</p>")
        if not decision['d_hold']:
            parts.append("<h3>D confirmation</h3>")
            d_changes = [r for r in v['changed'] if r['sleeve']=='D']
            if not d_changes:
                parts.append("<p>D is verified with no proposed weight changes.</p>")
            for r in d_changes:
                parts.append(f"<p>{e(position_name(r,release))}: held {pct(r['held'])} → target {pct(r['target'])} "
                             f"({pp(r['delta'])}).</p>")
            parts.append("<p class='note'>All D changes are shown here, including any repeated in the largest moves above. "
                         "If you received the initial email, A–C and overlays are unchanged; do not treat them as a second set of orders.</p>")
    if decision["d_hold"]:
        parts.append("<p>D remains on HOLD for selection. No Thursday-close substitute or new D ranking is used.</p>")
    if abs(book.get("rounding_residual_nav", 0.0)) > 1e-12:
        parts.append("<p>D holdings are unchanged; small rounding differences in totals are not trades.</p>")
    parts += ["<h2>03 · Positioning and review</h2>",
              f"<p><strong>Breadth gate: {'RISK OFF' if overlay['gate_on'] else 'RISK ON'} · "
              f"EM tilt: {'ON' if overlay['tilt_on'] else 'OFF'}</strong><br>Both inputs verified to {e(release['anchor'])}.</p>"]
    for text in context.get('watchlist',[]):
        parts.append(f"<p class='note'>{e(text)}</p>")
    allocation = []
    for sleeve, label in NAMES.items():
        total = math.fsum(r["target"] for r in v["rows"] if r["sleeve"] == sleeve)
        if total:
            allocation.append(f"{label} {pct(total, dp=1)}")
    parts.append(f"<p>{e(' · '.join(allocation))}</p>")
    for venue in sorted({s["venue"] for s in book["sleeves"]}):
        rows = [s for s in book["sleeves"] if s["venue"] == venue]
        parts.append(f"<p><strong>{e(venue)}: {e(long_date(rows[0]['fill_date']))} closing auction</strong><br>"
                     + e(' · '.join(f"Strategy {s['sleeve']}: {s['status']}" for s in rows)) + "</p>")
    parts += ["<p>Confirm broker submission times in the dashboard’s Execution Timing tab. "
              "An email review checkpoint is not an order cutoff. Review against actual holdings before submitting orders.</p>",
              "<p><a class='button' href='https://phuazz.github.io/breadth-thrust-etf/'>Open dashboard and Execution Timing</a></p>",
              f"<p class='note'>Source: sealed model release {e(release['identity'][:12])}; performance series {e(stats['series'])}. "
              "PDF: complete weekly brief and proposed book. HTML: accessible full book. JSON: exact proposed model weights.</p>",
              f"<p class='note'>{e(DISCLAIMER)}</p>"]
    css = """html{-webkit-text-size-adjust:100%;color-scheme:light}body{margin:0;background:#fff;color:#17212f;font:16px/1.6 Arial,sans-serif;padding:20px}
main{max-width:60ch;margin:auto}p{max-width:60ch;overflow-wrap:anywhere;margin:10px 0}h1{font-size:26px;line-height:1.25;margin:12px 0 24px}h2{font-size:20px;line-height:1.4;margin:28px 0 12px}h3{font-size:16px;margin:0}
.eyebrow{font-size:13px;color:#475569;letter-spacing:.04em}.status{border-left:4px solid #245c94;background:#eef4fa;padding:14px 18px}.status h2{margin:0;font-size:19px}.status p{margin-bottom:0}
.metrics{display:flex;flex-wrap:wrap;gap:8px}.metric{flex:1 1 90px;min-width:0;padding:12px;background:#f3f6f9;border:1px solid #d5dce5}.metric span{display:block;font-size:13px}.metric strong{display:block;font-size:23px}
.note{font-size:13px;color:#475569}.position{border-top:1px solid #d5dce5;padding:12px 0;break-inside:avoid}.position p{margin:4px 0}.driver{margin:8px 0}.driver p{margin:0}.driver strong{float:right}.track{height:6px;background:#eef2f6}
a{color:#164cb2}.button{display:inline-block;padding:12px 16px;background:#eef4fa;font-weight:bold;border:1px solid #b5c9dd}
@media(max-width:480px){body{padding:16px}h1{font-size:23px}.metric{flex-basis:80px}.metric strong{font-size:21px}}
html[data-theme=dark]{color-scheme:dark}html[data-theme=dark] body{background:#111827;color:#f3f4f6}html[data-theme=dark] .note,html[data-theme=dark] .eyebrow{color:#cbd5e1}
html[data-theme=dark] .status,html[data-theme=dark] .metric,html[data-theme=dark] .button{background:#1e293b;color:#f3f4f6}html[data-theme=dark] a{color:#93c5fd}
"""
    # Critical email styling is inline as well as in the stylesheet. No scripts,
    # remote images or fonts are required; information survives stripped CSS.
    html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(w['subject'])}</title><style>{css}</style></head>"
            "<body><main style='max-width:540px;margin:auto;font-family:Arial,sans-serif;line-height:1.6'>" + "".join(parts) + "</main></body></html>")
    # Inline essential layout and type for mail clients that strip the head.
    replacements = {"<body>": "<body style='margin:0;padding:16px;font:16px/1.6 Arial,sans-serif'>",
                    "<h1>": "<h1 style='font-size:26px;line-height:1.25'>",
                    "<h2>": "<h2 style='font-size:20px;line-height:1.4;margin-top:28px'>",
                    "<h3>": "<h3 style='font-size:16px'>",
                    "<p class='note'>": "<p class='note' style='font-size:13px;line-height:1.6'>",
                    "<div class='metric'>": "<div class='metric' style='display:inline-block;padding:12px;border:1px solid #d5dce5'>",
                    "<section class='position'>": "<section class='position' style='padding:12px 0;border-top:1px solid #d5dce5'>"}
    for old,new in replacements.items():
        html=html.replace(old,new)
    return html


def render_text(decision, release):
    """Readable plain-text alternative with the same figures as the HTML."""
    from html.parser import HTMLParser
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts=[]
            self.hidden=False
        def handle_starttag(self,tag,attrs):
            if tag in ('style','head'):
                self.hidden=True
            if tag in ('p','h1','h2','h3','section','div','br') and not self.hidden:
                self.parts.append('\n')
        def handle_endtag(self,tag):
            if tag=='head':
                self.hidden=False
        def handle_data(self,data):
            if not self.hidden:
                self.parts.append(data)
    parser=Text()
    parser.feed(render_html(decision,release))
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def render_pdf(decision, release):
    """Deterministic PDF, built from the identical release as the email."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, PageBreak
    output = BytesIO()
    styles = {k: ParagraphStyle(k, fontName="Helvetica-Bold" if k in ("title", "head") else "Helvetica",
              fontSize=size, leading=size*1.4, spaceAfter=8, textColor=colors.HexColor("#17212f"))
              for k, size in (("title", 21), ("head", 14), ("body", 10), ("note", 9))}
    styles['head'].keepWithNext=True
    styles['table']=ParagraphStyle('table',parent=styles['body'],fontSize=9.5,leading=12,spaceAfter=0)
    def p(text, style="body"):
        # Built-in PDF fonts: use printable ASCII punctuation, keep names intact.
        text = text.replace("→", " to ").replace("—", "-").replace("–", "-").replace("’", "'").replace("·", " / ").replace("×", " x ")
        return Paragraph(escape(text), styles[style])
    def table(rows, widths):
        t = Table([[p(str(c),'table') for c in row] for row in rows], colWidths=widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#edf2f7")),
              ("VALIGN",(0,0),(-1,-1),"TOP"),("BOTTOMPADDING",(0,0),(-1,-1),4),
              ("TOPPADDING",(0,0),(-1,-1),4),("LINEBELOW",(0,0),(-1,-1),.3,colors.HexColor("#d5dce5"))]))
        return t
    v, book, stats = view_model(decision,release), release["book"], release["performance"]
    ctx = release.get("presentation", {})
    flow = [p("USD Multi-Strategy ETF Portfolio", "title"), p(v["wording"]["heading"], "head"),
            p(f"Decision close {long_date(release['anchor'])}. Proposed model positions; not executed trades."),
            p(v["wording"]["difference"])]
    if release.get("preview_only") or stats["series"].startswith("synthetic"):
        flow.insert(0,p("NO-SEND PREVIEW - not a new instruction", "head"))
    flow += [p("01 / Performance and return drivers", "head")]
    if decision['d_hold']:
        flow.append(p("Performance is provisional while D data is incomplete. Full-portfolio figures may change when D completes; A-C and overlay instructions are verified."))
    flow.append(table([["Measure", "Model result"], *[[k, "Unavailable" if stats['values'].get(k) is None else f"{stats['values'][k]:.2f}" if k=="Sharpe" else pct(stats['values'][k],True)] for k in ('WTD','YTD','1Y','Sharpe','Max drawdown')]], [280,230]))
    flow += [p(f"Valuation {stats['as_of']}; weekly window {stats['wtd_start']} to {stats['as_of']}. "
               "YTD: prior year-end close. One year: trailing calendar year. Sharpe and drawdown: full model history. "
               "Proposed trades are not included.", "note")]
    if ctx.get("attribution"):
        flow.append(table([["Strategy", "Allocation", "Week return", "Contribution"], *[
            [NAMES[r["sleeve"]],pct(r["weight"]),pct(r["ret"],True),"Unavailable" if r["contribution"] is None else pp(r["contribution"])]
            for r in ctx["attribution"]]], [175,100,110,125]))
        flow.append(p("Approximate decision-date allocation x sleeve model return, using exact weekly endpoints. "
                      "Not realised attribution; missing endpoints are not filled. Difference from blend: "
                      + (pp(ctx["residual"]) if ctx["residual"] is not None else "unavailable because coverage is incomplete") + ".", "note"))
    if ctx.get("holding_returns"):
        flow += [PageBreak(),p("Holding-price moves over the same week", "head"),
                 p(f"{ctx['start']} to {ctx['end']}. Quote/proxy returns, not portfolio contributions. "
                   "Held weights identify exposure, not weights held throughout the window. Europe FX is not added. "
                   "All held lines shown; missing exact endpoints are unavailable.", "note")]
        flow.append(table([["Fund / price proxy", "Model-held NAV", "Week return"], *[
            [f"{position_name(r,release)} / {r['price_key']}",pct(r['held']),pct(r['ret'],True)]
            for r in sorted(ctx['holding_returns'],key=lambda r: (r['ret'] is None,-abs(r['ret'] or 0))) ]], [310,100,100]))
    flow += [PageBreak(),p("02 / Proposed changes and their signals", "head"),
             p(f"{len(v['changed'])} changes; {pct(v['turnover'])} one-way turnover (half the sum of absolute NAV changes). "
               f"New positions: {v['entries']}; exits: {v['exits']}. All changes are listed, including small resizes."),
             p("Strategy D: " + v["wording"]["d_instruction"])]
    for r in v["changed"]:
        flow += [p(f"{position_name(r,release)} / Strategy {r['sleeve']}", "head"),
                 p(f"Held {pct(r['held'])} to target {pct(r['target'])}; change {pp(r['delta'])}. " + rationale(r,book))]
    if not v["changed"]:
        flow.append(p("No position changes."))
    flow += [PageBreak(),p("03 / Complete proposed book", "head"),
             p("Model-held baseline, not broker holdings. Targets are for the next fill, not trades already completed. "
               "Exit lines remain visible at zero target weight. All weights are percentages of total NAV.")]
    flow.append(table([["Fund / strategy", "Held", "Target", "Change"], *[
        [f"{position_name(r,release)} / {r['sleeve']}",pct(r['held']),pct(r['target']),pp(r['delta'])]
        for r in sorted(v["rows"],key=lambda r:(-r["target"],r["sleeve"],r["etf"]))]], [265,80,80,85]))
    flow.append(p(f"Explicit non-trading rounding residual: {book.get('rounding_residual_nav',0):.8f} NAV. It is not a cash leg or an order.", "note"))
    flow += [PageBreak(),p("04 / Readiness, timing and provenance", "head")]
    for s in book["sleeves"]:
        flow += [p(f"Strategy {s['sleeve']} / {NAMES[s['sleeve']]}: {s['status']}", "head"),
                 p(f"Observed decision: {s['decision_session']}; required: {s['decision_session_for_fill']}. "
                   f"Proposed fill: {long_date(s['fill_date'])}, {s['venue']} closing auction.")]
        if s["status"]=="HOLD":
            flow.append(p("No Thursday substitution or new ranking. " + (s.get("reason") or "Data pending.")))
    overlay=book["overlay_decision"]
    if ctx.get('watchlist'):
        flow.append(p("Watchlist / recorded rule thresholds",'head'))
        flow.extend(p(text) for text in ctx['watchlist'])
    flow += [p(f"Breadth gate: {'RISK OFF' if overlay['gate_on'] else 'RISK ON'}; EM tilt: {'ON' if overlay['tilt_on'] else 'OFF'}. "
               f"Inputs verified to {release['anchor']}."),
             p("Confirm broker deadlines in the dashboard's Execution Timing tab. Dates and closing-auction times are venue-specific; an email checkpoint is not an order cutoff."),
             p(f"Performance source: {stats['series']}. Sealed release: {release['identity']}. "
               f"Model-held baseline as of {release['basis']['model_as_of']}.", "note"),p(DISCLAIMER,"note")]
    def footer(canvas, doc):
        canvas.setFont("Helvetica",8)
        canvas.setFillColor(colors.HexColor("#475569"))
        canvas.drawString(42,25,f"Model research / Decision {release['anchor']} / Not executed trades")
        canvas.drawRightString(A4[0]-42,25,f"Page {doc.page}")
    doc=SimpleDocTemplate(output,pagesize=A4,rightMargin=42,leftMargin=42,topMargin=40,bottomMargin=45,
                          title="Verified weekly portfolio factsheet",author="Portfolio research",invariant=1)
    doc.build(flow,onFirstPage=footer,onLaterPages=footer)
    return output.getvalue()
