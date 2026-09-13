"""Reader-facing factsheet from a verified release, never from mutable live data.

Python datetime months are 1-indexed. No fetching, ranking or delivery here.
The email is a short dossier; the PDF is its complete reference edition.

Section 02 is organised around the portfolio, not around the fund list: what
the strategy budgets did, the largest increases and reductions, what entered
and exited, then one compact held-to-target table per strategy with that
strategy's own driver stated once. Nothing here infers a signal, a scheme or a
market explanation that the sealed book does not already record.
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

# Presentation order: strategy identity, not move size. A small Europe resize
# therefore cannot be pushed off the page by a large core move.
SLEEVE_ORDER = ("A", "B", "C", "D", "TILT", "GATE")
# A delta below this is not a proposed trade; it is arithmetic noise.
CHANGE_EPSILON = 1e-8
# The established model-held-basis rounding bound (see the HOLD rounding fix).
MODEL_ROUNDING_NAV = 1e-4
# Stored signal rows carry four decimals, which bounds any reconstruction.
SIGNAL_PRECISION = 1e-4
# The house small-move threshold, matching MATERIAL_NAV in build_email_body.
MATERIAL_NAV = 0.005
# A pathological week must not turn the email into a book; the PDF holds all.
EMAIL_CHANGE_LIMIT = 20
# Units, per the house rule: relative breadth in percentage points; breadth
# levels and price distance in percentages. A breadth level cannot be
# negative and is written unsigned; a relative or distance reading can be.
SIGNAL_UNITS = {"breadth_relative": ("breadth versus the sector average", "pp", True),
                "breadth": ("constituent breadth", "%", False),
                "ma_distance": ("price distance from the 200-day average", "%", True)}
OVERLAY_SLEEVES = ("TILT", "GATE")
ACTION_WORDS = {"ENTER": "Enter", "EXIT": "Exit", "ADD": "Increase", "TRIM": "Reduce"}


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


# ---------------------------------------------------------------- signals ---

def sleeve_record(book, sleeve):
    return next((s for s in book["sleeves"] if s["sleeve"] == sleeve), {})


def signal_terms(record):
    return SIGNAL_UNITS.get(record.get("signal_kind"), ("the recorded signal", "", True))


def signal_level(value, suffix, signed):
    if value is None:
        return "Unavailable"
    return f"{value*100:+.2f}{suffix}" if signed else f"{value*100:.2f}{suffix}"


def signal_move(etf, record):
    """Before/after level and rank, taken only from the stored signal rows."""
    signals, previous = record.get("signals") or {}, record.get("signals_prev") or {}
    if etf not in signals:
        return None
    _, suffix, signed = signal_terms(record)
    order = sorted(signals, key=lambda k: (-signals[k], k))
    move = {"now": signal_level(signals[etf], suffix, signed), "rank": order.index(etf) + 1,
            "of": len(signals), "before": None, "rank_before": None}
    if etf in previous:
        move["before"] = signal_level(previous[etf], suffix, signed)
        move["rank_before"] = sorted(previous, key=lambda k: (-previous[k], k)).index(etf) + 1
    return move


def signal_cell(etf, record):
    """Compact evidence for one line: the level it moved to and its rank.

    The unit is written once, on the figure the reader ends on.
    """
    move = signal_move(etf, record)
    if move is None:
        return "No comparable signal recorded"
    unit = signal_terms(record)[1] or ""
    before = (move["before"] or "").removesuffix(unit) if unit else move["before"]
    level = f"{before} to {move['now']}" if move["before"] else move["now"]
    rank = (f"rank {move['rank_before']} to {move['rank']} of {move['of']}"
            if move["rank_before"] and move["rank_before"] != move["rank"]
            else f"rank {move['rank']} of {move['of']}")
    return f"{level} · {rank}"


def sizing_scheme(record):
    """Name the weighting only when this book's own numbers confirm it.

    Silence is the correct answer for an unrecognised scheme: the release
    records the signals and the weights, never a declared weighting rule.
    """
    weights, signals = record.get("weights") or {}, record.get("signals") or {}
    if len(weights) < 2:
        return None
    if max(weights.values()) - min(weights.values()) <= 1e-9:
        return "equal-weighted across the qualifying members"
    if set(weights) - set(signals):
        return None
    total = math.fsum(signals[k] for k in weights)
    if total <= 0:
        return None
    # Four-decimal signal rounding bounds how exactly a share can be rebuilt.
    tolerance = SIGNAL_PRECISION * (1 + len(weights)) / total
    if all(abs(weights[k] - signals[k] / total) <= tolerance for k in weights):
        return "sized in proportion to the recorded signal levels, so a weight can fall without a rank changing"
    return None


def rationale(row, book):
    """Full-sentence driver for one line, used in the reference edition."""
    if row.get("risk_adjustment"):
        return "Portfolio-risk adjustment only; selection unchanged."
    record = sleeve_record(book, row["sleeve"])
    if record.get("status") == "HOLD":
        return "Selection held pending complete data; no new ranking."
    if row["sleeve"] in OVERLAY_SLEEVES:
        return "Allocation follows the verified portfolio overlay."
    if abs(row["delta"]) <= CHANGE_EPSILON:
        return "No change to the model-held weight."
    move = signal_move(row["etf"], record)
    if move is None:
        return "No comparable signal recorded; proposed weight shown without an inferred driver."
    name = signal_terms(record)[0]
    return f"{name[0].upper()}{name[1:]}: {signal_cell(row['etf'], record)}."


# ------------------------------------------------------------- view model ---

def action_of(row):
    if abs(row["delta"]) <= CHANGE_EPSILON:
        return "HOLD"
    if row["held"] <= 0 and row["target"] > 0:
        return "ENTER"
    if row["target"] <= 0 and row["held"] > 0:
        return "EXIT"
    return "ADD" if row["delta"] > 0 else "TRIM"


def sleeve_shifts(book):
    """Held-to-target at strategy level, against the registered risk budget."""
    weights = book["overlay_decision"]["weights"]
    budgets = {"A": weights["a"], "B": weights["b"], "C": weights["c"], "D": weights["d"],
               "TILT": weights.get("tilt_nav", 0.0), "GATE": weights.get("shy_overlay", 0.0)}
    shifts = []
    for sleeve in SLEEVE_ORDER:
        rows = [r for r in book["lines"] if r["sleeve"] == sleeve]
        if not rows and not budgets.get(sleeve):
            continue
        held = math.fsum(r["held"] for r in rows)
        target = math.fsum(r["target"] for r in rows)
        shifts.append({"sleeve": sleeve, "name": NAMES[sleeve], "budget": budgets.get(sleeve, 0.0),
                       "held": held, "target": target, "net": target - held, "rows": rows,
                       "changed": sorted([r for r in rows if abs(r["delta"]) > CHANGE_EPSILON],
                                         key=lambda r: (-abs(r["delta"]), r["etf"]))})
    return shifts


def budgets_held(shifts):
    """True when no strategy's NAV share moves beyond the model rounding bound."""
    return all(abs(s["net"]) <= MODEL_ROUNDING_NAV for s in shifts)


def view_model(decision, release):
    book = release["book"]
    rows = sorted(book["lines"], key=lambda r: (-abs(r["delta"]), r["sleeve"], r["etf"]))
    changed = [r for r in rows if abs(r["delta"]) > CHANGE_EPSILON]
    shifts = sleeve_shifts(book)
    return {"wording": email_wording(decision), "rows": rows, "changed": changed,
            "shifts": shifts, "budgets_held": budgets_held(shifts),
            "turnover": math.fsum(abs(r["delta"]) for r in rows)/2,
            "increases": [r for r in changed if r["delta"] > 0],
            "reductions": [r for r in changed if r["delta"] < 0],
            "entering": [r for r in changed if action_of(r) == "ENTER"],
            "exiting": [r for r in changed if action_of(r) == "EXIT"],
            "entries": sum(r["held"] == 0 and r["target"] > 0 for r in changed),
            "exits": sum(r["held"] > 0 and r["target"] == 0 for r in changed)}


def position_name(row, release):
    return f"{release['labels'].get(row['etf'], row['etf'])} ({row['traded']})"


def allocation_strip(shifts):
    return " · ".join(f"{s['name']} {pct(s['target'], dp=1)}" for s in shifts if s["target"])


def budget_sentence(v):
    """State what happened to the strategy budgets, computed from the book."""
    if v["budgets_held"]:
        return ("The strategy budgets do not change. Every proposed move is a rotation inside a "
                f"strategy: {allocation_strip(v['shifts'])} of NAV.")
    moved = [s for s in v["shifts"] if abs(s["net"]) > MODEL_ROUNDING_NAV]
    return ("Strategy budgets change this week: "
            + " · ".join(f"{s['name']} {pct(s['held'], dp=1)} to {pct(s['target'], dp=1)} ({pp(s['net'])})"
                         for s in moved) + ".")


def sleeve_story(shift, release):
    """One grounded sentence per strategy that moved. No market commentary."""
    book = release["book"]
    changed = shift["changed"]
    if not changed:
        return None
    record = sleeve_record(book, shift["sleeve"])
    if shift["sleeve"] in OVERLAY_SLEEVES:
        return "Set by the verified portfolio overlay, not by a ranking."
    if record.get("status") == "HOLD":
        return "Selection held pending complete data; no new ranking was run."
    if all(r.get("risk_adjustment") for r in changed):
        return "Portfolio-risk resize only; the selection is unchanged."
    name = signal_terms(record)[0]
    up = max(changed, key=lambda r: r["delta"])
    down = min(changed, key=lambda r: r["delta"])
    parts = [f"Re-ranked on {name}."]
    if up["delta"] > 0:
        parts.append(f"Largest addition: {position_name(up, release)}, {pp(up['delta'])} to "
                     f"{pct(up['target'])} ({signal_cell(up['etf'], record)}).")
    if down["delta"] < 0:
        parts.append(f"Largest reduction: {position_name(down, release)}, {pp(down['delta'])} to "
                     f"{pct(down['target'])} ({signal_cell(down['etf'], record)}).")
    return " ".join(parts)


def sizing_note(shifts, book):
    """State the weighting once, and only when every ranked strategy agrees."""
    schemes = set()
    for shift in shifts:
        if not shift["changed"] or shift["sleeve"] in OVERLAY_SLEEVES:
            continue
        schemes.add(sizing_scheme(sleeve_record(book, shift["sleeve"])))
    if len(schemes) != 1:
        return None
    scheme = schemes.pop()
    return None if scheme is None else f"Within each strategy shown, members are {scheme}."


def group_heading(shift, book, decision=None):
    budget = "unchanged budget" if abs(shift["net"]) <= MODEL_ROUNDING_NAV else f"budget {pp(shift['net'])}"
    record = sleeve_record(book, shift["sleeve"])
    note = ""
    if shift["sleeve"] == "D" and record.get("status") == "READY":
        note = " · newly verified this week"
    elif (decision or {}).get("action") == "d_update":
        # A D follow-up is only authorised while the core identity matches the
        # one already distributed, so these lines are provably the same lines.
        note = " · already sent in the initial email"
    return f"Strategy {shift['sleeve']} · {shift['name']} · {pct(shift['target'], dp=1)} of NAV, {budget}{note}"


def unchanged_sentence(v, book):
    """What a reader does not need to act on, stated rather than left implicit."""
    parts = []
    for shift in v["shifts"]:
        if shift["changed"] or not shift["rows"]:
            continue
        count = len(shift["rows"])
        parts.append(f"{shift['name']} {pct(shift['target'], dp=1)}"
                     + (f" across {count} positions" if count > 1 else ""))
    overlay = book["overlay_decision"]
    parts.append(f"breadth gate {'RISK OFF' if overlay['gate_on'] else 'RISK ON'}")
    if not overlay["weights"].get("shy_overlay"):
        parts.append("no defensive allocation")
    return " · ".join(parts)


def highlight_rows(v, release):
    """Label/value pairs: the one-glance answer to what moves and what does not."""
    def listing(rows, phrase):
        return " · ".join(f"{position_name(r, release)} {pp(r['delta'])} {phrase} {pct(r['target'])}"
                          for r in rows)
    out = []
    if v["increases"]:
        out.append(("Increased", listing(v["increases"][:3], "to")))
    if v["reductions"]:
        out.append(("Reduced", listing(v["reductions"][:3], "to")))
    if v["entering"]:
        out.append(("Enters", " · ".join(f"{position_name(r, release)} at {pct(r['target'])} of NAV"
                                         for r in v["entering"])))
    if v["exiting"]:
        out.append(("Exits", " · ".join(f"{position_name(r, release)} from {pct(r['held'])} of NAV"
                                        for r in v["exiting"])))
    edges = v["entering"] + v["exiting"]
    if edges and all(max(r["held"], r["target"]) < MATERIAL_NAV for r in edges):
        out.append(("", f"Every entry and exit is below {pct(MATERIAL_NAV, dp=1)} of NAV: "
                        "ranking-tail positions, not a change of stance."))
    out.append(("Unchanged", unchanged_sentence(v, release["book"])))
    return out


# ----------------------------------------------------------------- render ---

REVISION_BANNER = (
    "This message changes the presentation only. The proposed positions, signals, dates and "
    "weights are identical to the factsheet already delivered for this week. It is not a new "
    "instruction and not a second set of orders; no action is required if you have already "
    "reviewed that email.")


def _tag(action):
    """Entries and exits are named, not merely coloured.

    The colour is inline so it survives a client that strips the stylesheet;
    the dark-theme rule overrides it, because these two values fall to roughly
    2.5:1 on the dark ground and the word alone must not be the fallback.
    """
    colour = {"ENTER": "#1a6b34", "EXIT": "#a3201a"}.get(action)
    if not colour:
        return ""
    return (f" <span class='tag {action.lower()}' style='font-size:13px;font-weight:bold;"
            f"letter-spacing:.04em;color:{colour}'>{escape(action)}</span>")


def _change_table(shifts, release, decision=None, limit=None, unchanged=False):
    """One compact held-to-target table, grouped by strategy, names and units."""
    e = book_e = escape
    book = release["book"]
    cell = "padding:7px 6px;border-bottom:1px solid #e3e8ee;vertical-align:top;font-size:13px"
    right = cell + ";text-align:right;white-space:nowrap"
    head = "padding:6px;border-bottom:1px solid #b5c3d3;font-size:13px;text-align:left"
    parts = ["<table class='changes' style='width:100%;table-layout:fixed;border-collapse:collapse'>",
             "<thead><tr>"
             f"<th style='{head};width:40%'>Position</th>"
             f"<th style='{head};width:18%;text-align:right'>Held</th>"
             f"<th style='{head};width:18%;text-align:right'>Target</th>"
             f"<th style='{head};width:24%;text-align:right'>Change</th></tr></thead><tbody>"]
    shown = 0
    for shift in shifts:
        rows = shift["rows"] if unchanged else shift["changed"]
        if not rows:
            continue
        if limit is not None:
            rows = rows[:max(0, limit - shown)]
            if not rows:
                continue
        story = sleeve_story(shift, release)
        detail = f"<br><span class='note' style='font-size:13px;line-height:1.5'>{book_e(story)}</span>" if story else ""
        parts.append("<tr class='group'><td colspan='4' style='padding:16px 6px 6px;font-size:13px;"
                     f"border-bottom:1px solid #b5c3d3'><strong>{book_e(group_heading(shift, book, decision))}</strong>{detail}</td></tr>")
        for row in rows:
            shown += 1
            parts.append(
                f"<tr class='position'><td style='{cell};overflow-wrap:anywhere'>"
                f"<strong>{e(row['traded'])}</strong>{_tag(action_of(row))}<br>"
                f"<span class='note' style='font-size:13px'>{e(release['labels'].get(row['etf'], row['etf']))}</span></td>"
                f"<td style='{right}'>{e(pct(row['held']))}</td>"
                f"<td style='{right}'>{e(pct(row['target']))}</td>"
                f"<td style='{right}'>{e(pp(row['delta']))}</td></tr>")
    parts.append("</tbody></table>")
    return "".join(parts), shown


def render_html(decision, release, include_unchanged=False):
    e, book, stats = escape, release["book"], release["performance"]
    v = view_model(decision, release)
    w, context = v["wording"], release.get("presentation", {})
    revision = decision.get("action") == "revision"
    stage = ("REVISED PRESENTATION" if revision else
             "INITIAL REVIEW" if decision["action"] == "preview" else
             "D UPDATE" if decision["action"] == "d_update" else "WEEKLY FACTSHEET")
    parts = [f"<p class='eyebrow'>{stage} · {e(long_date(release['anchor']))}</p>",
             "<h1>USD Multi-Strategy ETF Portfolio</h1>",
             f"<div class='status'><h2>{e(w['heading'])}</h2><p>{e(w['difference'])}</p></div>"]
    if revision:
        parts.insert(0, f"<p><strong>{e(REVISION_BANNER)}</strong></p>")
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
    parts += ["<h2>02 · What changes and why</h2>",
              f"<p><strong>{len(v['changed'])} proposed changes · {pct(v['turnover'])} one-way turnover · "
              f"{v['entries']} new · {v['exits']} closed.</strong> {e(budget_sentence(v))}</p>"]
    if v["changed"]:
        rows = "".join(f"<tr><th style='text-align:left;padding:7px 8px 7px 0;width:26%;font-size:13px;"
                       f"vertical-align:top;border-bottom:1px solid #e3e8ee'>{e(label)}</th>"
                       f"<td style='padding:7px 0;font-size:13px;vertical-align:top;"
                       f"border-bottom:1px solid #e3e8ee;overflow-wrap:anywhere'>{e(text)}</td></tr>"
                       for label, text in highlight_rows(v, release))
        parts.append("<table class='shifts' style='width:100%;table-layout:fixed;border-collapse:collapse'>"
                     f"<tbody>{rows}</tbody></table>")
    parts.append("<p class='note'>Proposed positions, not executed trades. Held and target are percentages of total NAV; "
                 "changes are percentage points. The held baseline is the model portfolio, not confirmation of broker holdings.</p>")
    if include_unchanged:
        parts.append("<h3>Complete proposed book</h3>")
        table, _ = _change_table(v["shifts"], release, decision, unchanged=True)
        parts.append(table)
    elif v["changed"]:
        table, shown = _change_table(v["shifts"], release, decision, limit=EMAIL_CHANGE_LIMIT)
        parts.append(table)
        scheme = sizing_note(v["shifts"], book)
        if shown < len(v["changed"]):
            parts.append(f"<p class='note'>{shown} of {len(v['changed'])} changes shown, the largest within each strategy. "
                         "Every change and unchanged position is in the attached PDF and complete HTML book.</p>")
        else:
            parts.append(f"<p class='note'>All {len(v['changed'])} proposed changes are listed above; none is omitted. "
                         "Unchanged positions are in the attached PDF and complete HTML book.</p>")
        if scheme:
            parts.append(f"<p class='note'>{e(scheme)}</p>")
    else:
        parts.append("<p>No position changes.</p>")
    parts.append(f"<p><strong>Strategy D:</strong> {e(w['d_instruction'])}</p>")
    if not include_unchanged and not decision['d_hold']:
        parts.append("<h3>D confirmation</h3>")
        d_changes = [r for r in v['changed'] if r['sleeve']=='D']
        if not d_changes:
            parts.append("<p>D is verified with no proposed weight changes.</p>")
        for r in d_changes:
            parts.append(f"<p>{e(position_name(r,release))}: held {pct(r['held'])} → target {pct(r['target'])} "
                         f"({pp(r['delta'])}).</p>")
        # Only a D follow-up can assert an earlier email; a single all-ready
        # factsheet has no predecessor, and a revision says so in its banner.
        earlier = (" A–C and the overlays are unchanged from the initial email; do not submit them a second time."
                   if decision["action"] == "d_update" else "")
        parts.append("<p class='note'>All D changes are shown here, including any repeated in the table above."
                     + e(earlier) + "</p>")
    if decision["d_hold"]:
        parts.append("<p>D remains on HOLD for selection. No Thursday-close substitute or new D ranking is used.</p>")
    if abs(book.get("rounding_residual_nav", 0.0)) > 1e-12:
        parts.append("<p>D holdings are unchanged; small rounding differences in totals are not trades.</p>")
    parts += ["<h2>03 · Positioning and review</h2>",
              f"<p><strong>Breadth gate: {'RISK OFF' if overlay['gate_on'] else 'RISK ON'} · "
              f"EM tilt: {'ON' if overlay['tilt_on'] else 'OFF'}</strong><br>Both inputs verified to {e(release['anchor'])}.</p>"]
    for text in context.get('watchlist',[]):
        parts.append(f"<p class='note'>{e(text)}</p>")
    parts.append(f"<p>{e(allocation_strip(v['shifts']))}</p>")
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
.note{font-size:13px;color:#475569}.driver{margin:8px 0}.driver p{margin:0}.driver strong{float:right}.track{height:6px;background:#eef2f6}
table{margin:12px 0}.changes thead th{color:#475569;letter-spacing:.03em}.changes tr.group td{background:#f3f6f9}.shifts th{color:#475569;letter-spacing:.03em}
a{color:#164cb2}.button{display:inline-block;padding:12px 16px;background:#eef4fa;font-weight:bold;border:1px solid #b5c9dd}
@media(max-width:480px){body{padding:16px}h1{font-size:23px}.metric{flex-basis:80px}.metric strong{font-size:21px}}
html[data-theme=dark]{color-scheme:dark}html[data-theme=dark] body{background:#111827;color:#f3f4f6}html[data-theme=dark] .note,html[data-theme=dark] .eyebrow,html[data-theme=dark] .changes thead th,html[data-theme=dark] .shifts th{color:#cbd5e1}
html[data-theme=dark] .status,html[data-theme=dark] .metric,html[data-theme=dark] .button,html[data-theme=dark] .changes tr.group td{background:#1e293b;color:#f3f4f6}html[data-theme=dark] a{color:#93c5fd}
html[data-theme=dark] .tag.enter{color:#86efac!important}html[data-theme=dark] .tag.exit{color:#fca5a5!important}
"""
    # Critical email styling is inline as well as in the stylesheet. No scripts,
    # remote images or fonts are required; information survives stripped CSS —
    # a table without its stylesheet is still a table.
    html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(w['subject'])}</title><style>{css}</style></head>"
            "<body><main style='max-width:540px;margin:auto;font-family:Arial,sans-serif;line-height:1.6'>" + "".join(parts) + "</main></body></html>")
    # Inline essential layout and type for mail clients that strip the head.
    replacements = {"<body>": "<body style='margin:0;padding:16px;font:16px/1.6 Arial,sans-serif'>",
                    "<h1>": "<h1 style='font-size:26px;line-height:1.25'>",
                    "<h2>": "<h2 style='font-size:20px;line-height:1.4;margin-top:28px'>",
                    "<h3>": "<h3 style='font-size:16px'>",
                    "<p class='note'>": "<p class='note' style='font-size:13px;line-height:1.6'>",
                    "<div class='metric'>": "<div class='metric' style='display:inline-block;padding:12px;border:1px solid #d5dce5'>"}
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
            self.cell=0
        def handle_starttag(self,tag,attrs):
            if tag in ('style','head'):
                self.hidden=True
            if self.hidden:
                return
            if tag in ('td','th'):
                self.cell+=1
            # Inside a cell a line break is a separator, not a new line: the
            # plain-text table must keep one row on one line.
            if tag in ('p','h1','h2','h3','section','div','tr') or (tag=='br' and not self.cell):
                self.parts.append('\n')
            elif tag in ('br','span','strong'):
                self.parts.append(' ')
        def handle_endtag(self,tag):
            if tag=='head':
                self.hidden=False
            if tag in ('td','th') and not self.hidden:
                self.cell=max(0,self.cell-1)
                self.parts.append(' · ')
        def handle_data(self,data):
            if not self.hidden:
                self.parts.append(data)
    parser=Text()
    parser.feed(render_html(decision,release))
    lines=(line.strip().strip('·').strip() for line in ''.join(parser.parts).splitlines())
    return '\n'.join(line for line in lines if line)


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
    styles['sub']=ParagraphStyle('sub',parent=styles['body'],fontName="Helvetica-Bold",fontSize=11,
                                 leading=15,spaceBefore=10,spaceAfter=3,keepWithNext=True)
    styles['table']=ParagraphStyle('table',parent=styles['body'],fontSize=9.5,leading=12,spaceAfter=0)
    styles['cell']=ParagraphStyle('cell',parent=styles['table'],fontSize=8.5,leading=11)
    def p(text, style="body"):
        # Built-in PDF fonts: use printable ASCII punctuation, keep names intact.
        text = text.replace("→", " to ").replace("—", "-").replace("–", "-").replace("’", "'").replace("·", " / ").replace("×", " x ")
        return Paragraph(escape(text), styles[style])
    def table(rows, widths, style="table", align=()):
        body = [[p(str(c), style) for c in row] for row in rows]
        t = Table(body, colWidths=widths, repeatRows=1, hAlign="LEFT")
        commands = [("BACKGROUND",(0,0),(-1,0),colors.HexColor("#edf2f7")),
                    ("VALIGN",(0,0),(-1,-1),"TOP"),("BOTTOMPADDING",(0,0),(-1,-1),4),
                    ("TOPPADDING",(0,0),(-1,-1),4),("LINEBELOW",(0,0),(-1,-1),.3,colors.HexColor("#d5dce5"))]
        commands += [("ALIGN",(c,0),(c,-1),"RIGHT") for c in align]
        t.setStyle(TableStyle(commands))
        return t
    v, book, stats = view_model(decision,release), release["book"], release["performance"]
    ctx = release.get("presentation", {})
    flow = [p("USD Multi-Strategy ETF Portfolio", "title"), p(v["wording"]["heading"], "head"),
            p(f"Decision close {long_date(release['anchor'])}. Proposed model positions; not executed trades."),
            p(v["wording"]["difference"])]
    if decision.get("action") == "revision":
        flow.insert(0, p(REVISION_BANNER, "head"))
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
    # ---- 02: portfolio first, then one compact table per strategy ----------
    flow += [PageBreak(),p("02 / What changes and why", "head"),
             p(f"{len(v['changed'])} proposed changes; {pct(v['turnover'])} one-way turnover (half the sum of "
               f"absolute NAV changes). {v['entries']} new position(s); {v['exits']} closed. " + budget_sentence(v))]
    flow.append(table([["Strategy", "Held", "Target", "Net shift", "Changes"], *[
        [f"{s['sleeve']} / {s['name']}", pct(s["held"]), pct(s["target"]), pp(s["net"]), str(len(s["changed"]))]
        for s in v["shifts"]]], [175,80,80,90,86], align=(1,2,3,4)))
    flow.append(p("Net shift is the strategy's change in NAV share. A shift within the model rounding bound "
                  f"of {pct(MODEL_ROUNDING_NAV, dp=2)} is rounding, not a budget decision.", "note"))
    if v["changed"]:
        flow.append(p("The largest moves at a glance", "sub"))
        flow.append(table([[label, text] for label, text in highlight_rows(v, release)], [90, 421]))
        for shift in v["shifts"]:
            if not shift["changed"]:
                continue
            flow.append(p(group_heading(shift, book, decision), "sub"))
            story = sleeve_story(shift, release)
            if story:
                flow.append(p(story, "note"))
            record = sleeve_record(book, shift["sleeve"])
            overlay_sleeve = shift["sleeve"] in OVERLAY_SLEEVES
            name, suffix, _ = signal_terms(record)
            header = ("Basis" if overlay_sleeve else f"Signal: {name}"
                      + (f" ({'percentage points' if suffix == 'pp' else 'percentages'})" if suffix else ""))
            def evidence(r):
                if overlay_sleeve:
                    return "Verified portfolio overlay allocation"
                return "Portfolio-risk adjustment only" if r.get("risk_adjustment") else signal_cell(r["etf"], record)
            flow.append(table([["Action", "Position", "Held", "Target", "Change", header], *[
                [ACTION_WORDS.get(action_of(r), action_of(r)), position_name(r, release),
                 pct(r["held"]), pct(r["target"]), pp(r["delta"]), evidence(r)]
                for r in shift["changed"]]], [54,160,46,48,52,151], style="cell", align=(2,3,4)))
        scheme = sizing_note(v["shifts"], book)
        if scheme:
            flow.append(p(scheme, "note"))
    else:
        flow.append(p("No position changes."))
    flow.append(p("Strategy D: " + v["wording"]["d_instruction"]))
    flow += [PageBreak(),p("03 / Complete proposed book", "head"),
             p("Model-held baseline, not broker holdings. Targets are for the next fill, not trades already completed. "
               "Exit lines remain visible at zero target weight. All weights are percentages of total NAV.")]
    flow.append(table([["Fund / strategy", "Held", "Target", "Change"], *[
        [f"{position_name(r,release)} / {r['sleeve']}",pct(r['held']),pct(r['target']),pp(r['delta'])]
        for r in sorted(v["rows"],key=lambda r:(-r["target"],r["sleeve"],r["etf"]))]], [265,80,80,85], align=(1,2,3)))
    flow.append(p(f"Explicit non-trading rounding residual: {book.get('rounding_residual_nav',0):.8f} NAV. It is not a cash leg or an order.", "note"))
    flow += [PageBreak(),p("04 / Readiness, timing and provenance", "head")]
    for s in book["sleeves"]:
        flow += [p(f"Strategy {s['sleeve']} / {NAMES[s['sleeve']]}: {s['status']}", "sub"),
                 p(f"Observed decision: {s['decision_session']}; required: {s['decision_session_for_fill']}. "
                   f"Proposed fill: {long_date(s['fill_date'])}, {s['venue']} closing auction.")]
        if s["status"]=="HOLD":
            flow.append(p("No Thursday substitution or new ranking. " + (s.get("reason") or "Data pending.")))
    overlay=book["overlay_decision"]
    if ctx.get('watchlist'):
        flow.append(p("Watchlist / recorded rule thresholds",'sub'))
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
