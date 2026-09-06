"""Target weights for the NEXT fill, ranked on each sleeve's own signal.

WHY A SEPARATE STEP FROM THE ENGINES.

The engines are backtests. They emit a rebalance only where an execution BAR
exists, because a backtest has to mark the trade to a price. That is right for
a backtest and useless on a Friday morning: the fill has not happened, so the
bar does not exist, so no engine can tell you what to trade.

Worse, it silently diverges from what was tested. Historical vendor data is
essentially complete (514 of 516 Xetra sessions over two years), so a backtest
Friday always has Thursday beside it and ranks on Thursday. Live, the Xetra
.DE lines publish roughly a session late — Thursday 13 Aug 2026 was absent at
Friday decision time and had backfilled by Saturday. So the backtest ranks on
Thursday while the live book, waiting for a bar, cannot rank at all. Twenty per
cent of NAV, every week.

THE FIX, WHICH IS THE WHOLE POINT OF THIS MODULE.

Sleeve D's SIGNAL was never late. It is the share of European constituents
above their 200-day average, and those constituent prices carried Thursday
throughout. Only the ETF wrapper's price was missing, and the wrapper's price
is needed to ACCOUNT for a trade, not to DECIDE one — live, you fill at the
real market, not at the vendor's bar.

So this ranks each sleeve on its own signal at the last completed session on
its own venue, using the engines' own weight functions, and never collapses
the signal onto the execution calendar. run_portfolio._build_panels_for does
collapse it (align_breadth_to_index onto closes.index), which is exactly how
Thursday goes missing; this reads the constituent breadth directly instead.

WHAT IT REFUSES TO DO. If a sleeve's signal does not reach the last completed
session on its venue, it is reported HOLD rather than ranked on whatever came
before. Ranking a session early is how EXH3/EXV3 flipped on a 1.3pp margin.

And reaching is not carrying. A decision row below ROW_COVERAGE_FLOOR of its
panel's names is HOLD too, because top-K of whatever published is a different
signal, not a smaller one: on 2026-08-28 sleeve A's row arrived with 5 of its
14 names and would have put 35% of NAV into one ETF at 50.06% one-way
turnover, while sleeve D's 3-of-5 row silently ejected EXV3. Caught by hand,
discarded by hand — this guard exists so the catching is not manual.

And a HOLD sleeve's intended book is its held book (owner decision 2026-09-02).
Its lines carry target = held, so nothing in it moves and nothing of it counts
towards turnover; the rank it could not use stays in sleeves[].weights. Before
this, a HOLD with no weights printed every held position as SELL ALL under a
hold pill, beside a banner saying do not trade.

Usage:
    python scripts/live_targets.py
    python scripts/live_targets.py --json data/live_targets.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_factsheet import _collect_deployed_holdings, load_all  # noqa: E402
from etf_registry import (  # noqa: E402
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
    display_ticker,
    get_etf,
)
from run_ma200_sweep import (  # noqa: E402
    MA_PERIOD,
    compute_ma200_breadth,
    load_constituent_prices,
)
from session_bounds import last_completed_session_on  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# A decision row must carry EVERY name in its panel before it ranks. 1.0 is
# deliberate, not a placeholder: one missing name already re-ranks the sleeve
# on a different universe, and the repository's other coverage floor (0.85,
# compute_breadth's WARN level, enforced by G6) is known to be too loose --
# it was calibrated while the ITWN coverage bug was depressing the
# measurement, and the bug's fix (2026-08-16, coverage 89.7% -> 98.7%) left
# it far below anything a healthy panel produces. Loosening this floor is an
# owner decision, not a tuning knob.
ROW_COVERAGE_FLOOR = 1.0


def _breadth_panel(universe: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Constituent-derived breadth, on its OWN index.

    Deliberately not _build_panels_for: that aligns breadth onto the execution
    calendar, which deletes a signal the vendor did publish whenever the ETF
    wrapper's own bar is missing.
    """
    cols, used = {}, []
    for etf in universe:
        try:
            cols[etf] = compute_ma200_breadth(load_constituent_prices(etf), MA_PERIOD)
        except FileNotFoundError:
            continue
        used.append(etf)
    return pd.DataFrame(cols).sort_index(), used


def _venue(universe: list[str]) -> str:
    cals = {get_etf(e).get("trading_calendar", "NYSE") for e in universe}
    return sorted(cals)[0] if len(cals) == 1 else "NYSE"


def _rank(signal: pd.DataFrame, weight_fn, venue: str, now_utc: datetime,
          label: str, coverage_floor: float = ROW_COVERAGE_FLOOR,
          signal_kind: str = "breadth", top_k: int | None = None,
          prev_session: str | None = None) -> dict:
    """Rank on the last signal session at or before the venue's last close.

    Refuses twice, for two different failures. A row that stops SHORT of the
    last close would rank an earlier session (the EXH3/EXV3 flip); a row that
    REACHES it but arrives hollow would rank top-K of whatever published (the
    2026-08-28 5-of-14 row). Both report HOLD, and the hollow row is refused
    BEFORE the weight function runs — a book ranked on a partial panel must
    not exist even transiently, because the ranked artefact is exactly the
    thing that gets trusted.
    """
    cal = mcal.get_calendar(venue)
    lcs = last_completed_session_on(cal, now_utc)
    rows = signal.dropna(how="all")
    usable = rows.index[rows.index <= pd.Timestamp(lcs)] if lcs is not None else rows.index
    if len(usable) == 0:
        return {"sleeve": label, "venue": venue, "status": "HOLD",
                "reason": "no signal at or before the last completed session",
                "decision_session": None, "last_completed_session":
                    str(lcs.date()) if lcs is not None else None, "weights": {}}
    decided = usable[-1]
    row = signal.loc[decided]
    # `decided` survived dropna(how="all"), so the panel has at least one
    # column and the share below is always well defined.
    have, total = int(row.notna().sum()), int(signal.shape[1])
    if have < total * coverage_floor:
        return {"sleeve": label, "venue": venue, "status": "HOLD",
                "reason": (
                    f"decision row carries {have} of {total} names — below "
                    f"the {coverage_floor:.0%} coverage floor; a partial row "
                    f"is a different signal, not a smaller one"),
                "decision_session": str(decided.date()),
                "last_completed_session":
                    str(lcs.date()) if lcs is not None else None,
                "weights": {}}
    reaches = lcs is not None and pd.Timestamp(decided) >= pd.Timestamp(lcs)
    w = weight_fn(row)
    w = w[w > 0].sort_values(ascending=False)
    # THE SIGNAL ITSELF, not only the weights it produced (2026-09-06). The
    # commentary says why a name moved — "breadth 61.0% → 55.2%, rank 7 → 9,
    # out of the top 7" — and it can only say so from the row the sleeve
    # ranked on and the row it ranked on last time. Both are recorded here,
    # in the sleeve's own units, for every name in the panel, so nothing
    # downstream has to recompute a signal and risk disagreeing with the one
    # that actually decided the book.
    prev_row = {}
    if prev_session:
        try:
            ts = pd.Timestamp(prev_session)
            if ts in signal.index:
                prev_row = _row_dict(signal.loc[ts])
        except Exception:  # noqa: BLE001 — an unparseable date is simply "no prior row"
            prev_row = {}
    return {
        "sleeve": label,
        "venue": venue,
        "status": "READY" if reaches else "HOLD",
        "reason": None if reaches else (
            f"signal reaches {decided.date()} but the last completed {venue} "
            f"session is {lcs.date()} — ranking now would use an "
            f"earlier session"),
        "decision_session": str(decided.date()),
        "last_completed_session": str(lcs.date()) if lcs is not None else None,
        "weights": {k: round(float(v), 6) for k, v in w.items()},
        "signal_kind": signal_kind,
        "top_k": top_k,
        "signals": _row_dict(row),
        "signals_prev": prev_row,
        "decision_session_prev": prev_session if prev_row else None,
    }


def _row_dict(row: pd.Series) -> dict[str, float | None]:
    return {str(k): (None if pd.isna(v) else round(float(v), 4))
            for k, v in row.items()}


def _prev_decisions(sleeve_data) -> dict[str, str | None]:
    """Each sleeve's PREVIOUS decision close, from the engines' own last
    rebalance record (latest_rebalance, else the last trade). Empty when
    the payloads have no record — the commentary then states the signal
    without a comparison rather than inventing one."""
    out: dict[str, str | None] = {}
    if not isinstance(sleeve_data, dict):
        return out
    for key, sl in (("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")):
        h = ((sleeve_data.get(key) or {}).get("headline") or {})
        rec = h.get("latest_rebalance") or ((h.get("trade_history") or [None])[-1])
        out[sl] = (rec or {}).get("decision_date")
    return out


def build(now_utc: datetime | None = None) -> dict:
    now = now_utc or datetime.now(timezone.utc)
    import run_asset_class_rotation as ac
    import run_europe_rotation as eu
    import run_thematic_rotation as th
    import run_topk_robustness as tk
    from run_portfolio import top_k_breadth_weight

    # Loaded first: the previous decision close per sleeve comes from the
    # engines' own records and is handed to every rank below.
    out = load_all()
    overlay, sleeve_data = out[1], out[2]
    prev = _prev_decisions(sleeve_data)

    sleeves = []

    ba, used_a = _breadth_panel(UNIVERSE_ETFS)
    sleeves.append(_rank(tk._to_signal_panel(ba),
                         top_k_breadth_weight(tk.HEADLINE_K),
                         _venue(used_a), now, "A",
                         signal_kind="breadth", top_k=tk.HEADLINE_K,
                         prev_session=prev.get("A")))

    cb = ac.download_prices()
    sleeves.append(_rank(ac.compute_signal(cb),
                         ac.top_k_by_signal(ac.HEADLINE_K), "NYSE", now, "B",
                         signal_kind="ma_distance", top_k=ac.HEADLINE_K,
                         prev_session=prev.get("B")))

    cc = th.download_prices()
    sleeves.append(_rank(th.compute_signal(cc),
                         th.top_k_equal_weight(th.HEADLINE_K), "NYSE", now, "C",
                         signal_kind="ma_distance", top_k=th.HEADLINE_K,
                         prev_session=prev.get("C")))

    bd, used_d = _breadth_panel(UNIVERSE_EUROPE_SECTORS)
    sleeves.append(_rank(bd, eu.top_k_breadth_weight(eu.HEADLINE_K),
                         _venue(used_d), now, "D",
                         signal_kind="breadth", top_k=eu.HEADLINE_K,
                         prev_session=prev.get("D")))

    # Effective NAV weights and the delta against what is currently held.
    from build_factsheet import sleeve_nav_weights
    asof = max((s["decision_session"] for s in sleeves
                if s["decision_session"]), default=None)
    st = sleeve_nav_weights(overlay, asof)
    held = {}
    for h in _collect_deployed_holdings(sleeve_data, overlay, asof):
        held[(h["sleeve"], h["etf"])] = h["effective"]

    lines = _intended_lines(sleeves, held, st)
    # WHICH fill is this, and has it happened? Both stated explicitly, because
    # a target book that does not say either is indistinguishable from a record
    # of trades already done -- which is the one way this artefact could
    # mislead. `executed` is a constant here by construction: this module only
    # ever describes an INTENDED book.
    fills = {}
    for sl in sleeves:
        fd = next_fill_date(sl["venue"], now)
        sl["fill_date"] = fd
        fills[sl["venue"]] = fd
    distinct = sorted({v for v in fills.values() if v})

    # IS THIS THE BOOK THAT WILL ACTUALLY TRADE, or a provisional one?
    #
    # The engines rank at rd-1, so a fill is decided by the session immediately
    # before it. These targets are FINAL only when the session they were ranked
    # on IS that session. Mid-week they are not: on Wed 2026-08-26 the card was
    # ranked on Tue 25 August for a Mon 31 August fill, with three more sessions
    # to come, every one of which re-ranks it. Same card, entirely different
    # standing, and nothing on the page said which.
    #
    # Computed per sleeve against each venue's own calendar, never from "the
    # previous weekday": a holiday moves the decision session and the two are
    # not the same day.
    decisions = {}
    for sl in sleeves:
        ds = decision_session_for(sl["venue"], sl["fill_date"]) if sl["fill_date"] else None
        sl["decision_session_for_fill"] = ds
        decisions[sl["venue"]] = ds
    final = _targets_final(sleeves)
    ds_distinct = sorted({v for v in decisions.values() if v})

    return {"computed_at_utc": now.isoformat(), "as_of": asof,
            "executed": False,
            "targets_final": final,
            "next_fill": {
                "by_venue": fills,
                "date": distinct[0] if len(distinct) == 1 else None,
                "venues_agree": len(distinct) == 1,
                # The close these targets WILL be ranked on, which is not
                # necessarily the one they were ranked on -- see targets_final.
                "decision_by_venue": decisions,
                "decision_session": ds_distinct[0] if len(ds_distinct) == 1 else None,
            },
            "sleeves": sleeves, "lines": lines,
            "one_way_turnover": sum(abs(x["delta"]) for x in lines) / 2}


def _intended_lines(sleeves: list[dict], held: dict[tuple[str, str], float],
                    sleeve_nav: dict[str, float]) -> list[dict]:
    """The intended book, line by line, against what is held.

    A READY sleeve's lines are its ranked weights scaled to the sleeve's share
    of effective NAV, plus an exit line (target 0) for any held name the rank
    dropped. TILT/GATE overlays are carried as held.

    A HOLD sleeve's intended book IS its held book (owner decision 2026-09-02).
    Before this, a HOLD with no weights fell through to the exit path and every
    held position printed as SELL ALL under a hold pill, with the notional
    liquidation counted in one-way turnover; a HOLD ranked on a stale session
    printed that rank's ADD/TRIM/BUY lines under the same pill. Both sat beside
    a banner saying do not trade, and the coverage guard makes the no-weights
    HOLD common rather than rare. So every held position of a HOLD sleeve
    carries target = held (delta 0; `within` is its share of the sleeve's held
    book, which keeps the artefact identity target == within x sum(target)),
    and a ranked-but-unheld name gets no line at all: nothing is intended to be
    bought. The rank the sleeve could not use stays in sleeves[].weights.
    """
    status_of = {s["sleeve"]: s["status"] for s in sleeves}
    ready = {sl for sl, status in status_of.items() if status == "READY"}
    lines = []
    for s in sleeves:
        if s["sleeve"] not in ready:
            continue
        nav = sleeve_nav.get(s["sleeve"].lower(), 0.0)
        for etf, w in s["weights"].items():
            lines.append({"sleeve": s["sleeve"], "etf": etf,
                          "traded": _traded(etf), "within": w,
                          "target": w * nav,
                          "held": held.get((s["sleeve"], etf), 0.0),
                          "status": s["status"]})
    held_total: dict[str, float] = defaultdict(float)
    for (sl, _etf), eff in held.items():
        held_total[sl] += eff
    for (sl, etf), eff in held.items():
        if sl in {"TILT", "GATE"}:
            lines.append({"sleeve": sl, "etf": etf, "traded": _traded(etf),
                          "within": 1.0, "target": eff, "held": eff,
                          "status": "READY"})
        elif sl in status_of and sl not in ready:
            tot = held_total[sl]
            lines.append({"sleeve": sl, "etf": etf, "traded": _traded(etf),
                          "within": eff / tot if tot > 0 else 0.0,
                          "target": eff, "held": eff,
                          "status": status_of[sl]})
        elif not any(x["sleeve"] == sl and x["etf"] == etf for x in lines):
            lines.append({"sleeve": sl, "etf": etf, "traded": _traded(etf),
                          "within": 0.0, "target": 0.0, "held": eff,
                          "status": status_of.get(sl, "READY")})
    for ln in lines:
        ln["delta"] = ln["target"] - ln["held"]
    lines.sort(key=lambda x: (x["sleeve"], -abs(x["delta"])))
    return lines


def _targets_final(sleeves: list[dict]) -> bool:
    """FINAL means every sleeve is READY and was ranked on the very close its
    fill will use.

    Every sleeve, not the latest. The artefact's top-level ``as_of`` is the
    LATEST decision session across sleeves, so on a mixed morning -- A and D
    ranked on Friday, B and C HOLD on Thursday because the vendor withheld
    their Friday row (2026-08-30) -- ``as_of`` equals the fill's decision
    session while two sleeves would trade a session-early rank. The card is
    styled off this flag, so it has to be the weakest sleeve's answer.

    A HOLD sleeve is never final even when its dates agree: a hollow-row HOLD
    keeps its decision_session so the operator can check the row against the
    vendor, but its printed lines (target 0, "leave as held") are not the
    weights that will trade, and a PLANNED pill over a do-not-trade banner is
    a contradiction the reader is left to resolve.
    """
    return bool(sleeves) and all(
        s.get("status") == "READY"
        and s.get("decision_session") and s.get("decision_session_for_fill")
        and s["decision_session"] == s["decision_session_for_fill"]
        for s in sleeves)


def decision_session_for(venue: str, fill_date: str,
                         lookback_days: int = 15) -> str | None:
    """The close a fill on ``fill_date`` is ranked on: the session before it.

    Mirrors the engines, which rank at ``get_loc(rd) - 1``. Taken from the
    venue's real calendar rather than "the previous weekday", because a holiday
    moves it and the two are not the same day: a Monday fill normally ranks on
    Friday, but the Monday after a Friday holiday ranks on the Thursday.

    ``lookback_days`` of 15 calendar days comfortably spans any real holiday
    run while keeping the schedule fetch small. Returns None rather than
    guessing when the window somehow contains no prior session.

    Python datetime months are 1-indexed; no weekday is ever derived by hand.
    """
    start = (pd.Timestamp(fill_date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    sched = mcal.get_calendar(venue).schedule(start_date=start, end_date=fill_date)
    if not len(sched):
        return None
    target = pd.Timestamp(fill_date).date()
    prior = [d for d in pd.DatetimeIndex(sched.index) if d.date() < target]
    return str(prior[-1].date()) if prior else None


def next_fill_date(venue: str, now_utc: datetime,
                   horizon_days: int = 30) -> str | None:
    """The next session on which this venue's sleeves are scheduled to trade.

    Derived from the SAME function the engines use (engine_rebalance_dates
    under the active DEFAULT_MODE), not from "the next Monday". Those are not
    the same thing: a holiday Monday rolls FORWARD under holiday_aware_next,
    and the venues diverge when they do -- 2026-09-07 is a NYSE holiday that
    rolls the US sleeves to the 8th while Xetra trades the 7th. Computing it
    per venue is the only way that stays right.

    Python datetime months are 1-indexed; the weekday is never derived by hand.
    """
    import rebalance_calendar as rc
    import run_topk_robustness as tk
    start = (now_utc - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = (now_utc + pd.Timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    sched = mcal.get_calendar(venue).schedule(start_date=start, end_date=end)
    if not len(sched):
        return None
    idx = pd.DatetimeIndex(sched.index)
    dates = rc.engine_rebalance_dates(idx, idx[0], freq=tk.HEADLINE_FREQ,
                                      calendar=venue)
    today = pd.Timestamp(now_utc.date())
    # TODAY COUNTS (2026-08-31, owner instruction, reversing the original
    # call). This was `d > today`, on the reasoning that on the fill day the
    # trade is already being placed, so showing it as upcoming would mislead.
    # That holds AFTER the trade is placed and not before it, and the card is
    # read before: asked on Monday morning the operator's question is "what do
    # I trade today", and the strict test answered with NEXT week's provisional
    # book while saying nothing about the fill actually due. On 2026-08-31 the
    # card showed a 7/8 September fill on the morning of a 31 August one.
    #
    # `executed` is False by construction here — this module only ever
    # describes an INTENDED book — so including today cannot make a completed
    # trade look pending. It also lets targets_final speak: the decision
    # session for today's fill IS the Friday close these were ranked on, so
    # the card can say "final, trade these" instead of "provisional".
    upcoming = [d for d in dates if d >= today]
    return str(upcoming[0].date()) if upcoming else None


def _traded(etf: str) -> str:
    try:
        return display_ticker(etf)
    except Exception:  # noqa: BLE001
        return etf


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default=str(DATA_DIR / "live_targets.json"))
    args = ap.parse_args(argv)
    r = build()

    nf = r.get("next_fill", {})
    when = nf.get("date") or "/".join(
        f"{k} {v}" for k, v in (nf.get("by_venue") or {}).items())
    print(f"\nTARGETS for the next fill — NOT YET EXECUTED, intended for "
          f"{when}\n  computed {r['computed_at_utc'][:16]}Z from the "
          f"{r.get('as_of')} close\n")
    print(f"  {'sleeve':7s} {'venue':6s} {'decided on':12s} {'vs last close':13s} status")
    for s in r["sleeves"]:
        print(f"  {s['sleeve']:7s} {s['venue']:6s} "
              f"{str(s['decision_session']):12s} "
              f"{str(s['last_completed_session']):13s} {s['status']}")
        if s["reason"]:
            print(f"          {s['reason']}")

    holds = [s["sleeve"] for s in r["sleeves"] if s["status"] != "READY"]
    print(f"\n  {'S':2s} {'key':10s} {'traded':10s} {'held':>7s} {'target':>7s} "
          f"{'delta':>8s}")
    for ln in r["lines"]:
        if abs(ln["delta"]) < 5e-5 and ln["held"] == 0:
            continue
        tag = ""
        if ln["status"] != "READY":
            tag = "  HOLD"
        elif ln["held"] == 0:
            tag = "  <== BUY (new)"
        elif ln["target"] == 0:
            tag = "  <== SELL ALL"
        print(f"  {ln['sleeve']:2s} {ln['etf']:10s} {ln['traded']:10s} "
              f"{ln['held']*100:6.2f}% {ln['target']*100:6.2f}% "
              f"{ln['delta']*100:+7.2f}%{tag}")
    print(f"\n  one-way turnover {r['one_way_turnover']*100:.2f}% of NAV")
    if holds:
        # Not "(signal short of the last close)": with the coverage floor
        # there are two ways to HOLD, and each sleeve's own reason is already
        # printed beside it above.
        print(f"  HOLD: {', '.join(holds)} — do not trade these; leave them "
              f"as held (each sleeve's reason is printed above).")

    Path(args.json).write_text(json.dumps(r, indent=1), encoding="utf-8")
    print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
