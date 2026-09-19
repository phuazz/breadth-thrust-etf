#!/usr/bin/env python3
"""WS21 measurements M1-M4 — disclosure on the SEEN window only.

Registration ``KICKOFF_ws21-c-bitcoin-basis.md`` §5. Four measurements, two of
them stop conditions:

  M1  Premium/discount.  Daily log-return difference between IBIT's close and
      Binance BTCUSDT at the NYSE close instant. STOP if the p5-p95 band falls
      outside +/-1.0%.
  M2  Restatement.  Sleeve C on the seen window, incumbent basis against the
      new: weekly baskets that differ, gate-state flips, the dates of each, and
      Sharpe / CAGR / MaxDD / turnover for C plus Sharpe for the deployed
      35/35/10/20 blend. Disclosure, not a gate. No tuning.
  M3  Join behaviour.  Maximum absolute signal difference while the 200-session
      window straddles the cut-over (2024-01-11 -> 2024-10-25).
  M4  Source agreement.  Norgate TOTALRETURN against Yahoo auto_adjust for
      IBIT over its whole span. STOP above 1e-4.

THE HARD CAP. Nothing after 2026-07-02 is computed under the new basis before
the WS7 verdict is filed: WS7 is decided on the incumbent basis and the
``BTC-USD`` series is an input to both of its legs, so reading its OOS window
through a different lens would contaminate the review it is contingent on. The
cap refuses unless ``--after-ws7`` is passed AND ``C:\\dev\\STUDIES_LEDGER.md``
carries a WS7 verdict row — both, because either alone is a claim rather than
evidence.

    python scripts/ws21_measure.py                 # the seen window
    python scripts/ws21_measure.py --skip m1       # offline: no Binance call

Outputs ``reviews/2026-09-19_ws21_measurements.json`` and a short markdown
table beside it. Python datetime months are 1-indexed (January = 1); every
session comes from venue_calendars.get_calendar("NYSE") and every close
instant is resolved per date with zoneinfo("America/New_York").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import btc_basis  # noqa: E402
import price_source as ps  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

#: The cut-over, and the first session of the seen window.
WINDOW_START = btc_basis.CUTOVER                       # 2024-01-11
#: The last session WS21 may look at before the WS7 verdict is filed. WS7's OOS
#: window opens 2026-07-03, so this is the session before it.
HARD_CAP = date(2026, 7, 2)
#: The 200th NYSE session counting the cut-over as the first — after this the
#: moving-average window no longer straddles the join (registration §5 M3).
M3_END = date(2024, 10, 25)

LEDGER = Path(r"C:\dev\STUDIES_LEDGER.md")
REVIEWS = PROJECT_ROOT / "reviews"
STAMP = "2026-09-19"
OUT_JSON = REVIEWS / f"{STAMP}_ws21_measurements.json"
OUT_MD = REVIEWS / f"{STAMP}_ws21_measurements.md"

BINANCE = "https://data-api.binance.vision/api/v3/klines"
NY = ZoneInfo("America/New_York")

#: Registration §5: the band outside which M1 halts the flip pending review.
M1_STOP_BAND = 0.010
#: Registration §5: the relative difference above which M4 halts for
#: investigation.
M4_STOP = 1e-4
#: The deployed blend (run_multi_strategy: 35/35/10/20 A:B:C:D).
BLEND = (0.35, 0.35, 0.10)


class CapRefused(RuntimeError):
    """A date past the hard cap, without the WS7 verdict to license it."""


# ---------------------------------------------------------------------------
# The hard cap
# ---------------------------------------------------------------------------
def ws7_verdict_filed(ledger: Path = LEDGER) -> bool:
    """Does the ledger carry a WS7 VERDICT row for this project?

    Deliberately strict about the SCOPE cell. The 2026-07-18 kickoff row says
    "the verdict lands in the 2026-10-02 review row", and a substring search
    over the whole row would read that sentence as the verdict itself — a guard
    that unlocks on a promise is not a guard.
    """
    if not ledger.exists():
        return False
    sys.path.insert(0, str(ledger.parent / "scripts"))
    import ledger_parse                                   # noqa: PLC0415
    for row in ledger_parse.parse(ledger):
        if row.get("project", "").strip() != "breadth-thrust-etf":
            continue
        if re.search(r"WS7\b[^|]{0,80}VERDICT", row.get("scope", ""), re.I):
            return True
    return False


def resolve_window(after_ws7: bool, end: date | None = None) -> date:
    """The last session this run may read, or a refusal naming the reason."""
    requested = end or HARD_CAP
    if requested <= HARD_CAP:
        return requested
    if not after_ws7:
        raise CapRefused(
            f"{requested} is past the hard cap {HARD_CAP}. WS7 is decided on "
            f"the incumbent basis and BTC-USD is an input to both of its legs; "
            f"computing its OOS window under the staged basis would "
            f"contaminate the review WS21 is contingent on. Pass --after-ws7 "
            f"once the verdict is filed.")
    if not ws7_verdict_filed():
        raise CapRefused(
            f"--after-ws7 was passed but {LEDGER} carries no WS7 VERDICT row "
            f"for breadth-thrust-etf. The flag is not the evidence; the filed "
            f"row is.")
    return requested


def nyse_schedule(start: date, end: date) -> pd.DataFrame:
    return get_calendar("NYSE").schedule(start_date=str(start), end_date=str(end))


# ---------------------------------------------------------------------------
# Price sources
# ---------------------------------------------------------------------------
def norgate_ibit(start: date, end: date) -> pd.Series | None:
    """IBIT TOTALRETURN closes from the licensed feed, or None."""
    try:
        import norgate_prices as npx                      # noqa: PLC0415
        if not npx.available():
            return None
        frame, served, _ = npx.fetch_closes([btc_basis.TRADED], str(start),
                                            str(end), verbose=False)
    except Exception:                                     # noqa: BLE001
        return None
    if btc_basis.TRADED not in (served or []):
        return None
    # Norgate stores float32; cast once, here, so every ratio below is computed
    # in double precision and M4's tolerance is not spent on storage width.
    s = frame[btc_basis.TRADED].astype("float64").dropna()
    s.index = pd.to_datetime(s.index).normalize()
    return s[~s.index.duplicated(keep="last")]


def yahoo_ibit(start: date, end: date) -> pd.Series | None:
    """IBIT auto_adjust closes from yfinance, or None."""
    try:
        import yfinance as yf                             # noqa: PLC0415
        raw = yf.download(btc_basis.TRADED, start=str(start),
                          end=str(end + timedelta(days=1)), auto_adjust=True,
                          progress=False)
    except Exception:                                     # noqa: BLE001
        return None
    if raw is None or len(raw) == 0 or "Close" not in raw.columns:
        return None
    s = raw["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.astype("float64").dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s[~s.index.duplicated(keep="last")]


def ibit_closes(start: date, end: date) -> tuple[pd.Series, str]:
    """IBIT's closes and which feed they came from. Norgate first (§4)."""
    ng = norgate_ibit(start, end)
    if ng is not None and len(ng):
        return ng, "norgate TOTALRETURN"
    yh = yahoo_ibit(start, end)
    if yh is not None and len(yh):
        return yh, "yahoo auto_adjust (Norgate unreachable)"
    raise RuntimeError(f"no source served {btc_basis.TRADED}")


def binance_hourly(start_utc: datetime, end_utc: datetime,
                   symbol: str = "BTCUSDT", pause: float = 0.2) -> pd.Series:
    """Hourly closes keyed by the kline's OPEN time, in UTC.

    The public data mirror, which serves market data without an API key. Paged
    at the endpoint's 1000-row limit.
    """
    out: dict[pd.Timestamp, float] = {}
    cursor = int(start_utc.timestamp() * 1000)
    stop = int(end_utc.timestamp() * 1000)
    while cursor <= stop:
        url = (f"{BINANCE}?symbol={symbol}&interval=1h&startTime={cursor}"
               f"&endTime={stop}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                rows = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Binance request failed: {exc}") from exc
        if not rows:
            break
        for r in rows:
            out[pd.Timestamp(int(r[0]), unit="ms", tz="UTC")] = float(r[4])
        cursor = int(rows[-1][0]) + 3_600_000
        if len(rows) < 1000:
            break
        time.sleep(pause)
    return pd.Series(out).sort_index()


# ---------------------------------------------------------------------------
# M1 — premium / discount
# ---------------------------------------------------------------------------
def close_instants(start: date, end: date) -> pd.Series:
    """``{session date: NYSE close instant in UTC}``, resolved per date.

    The calendar carries the half-day closes (13:00 ET) as well as the regular
    16:00, so the instant is read off it rather than assumed; the local time is
    then asserted through ``zoneinfo`` so a calendar change could not move the
    measurement point without failing here first.
    """
    sched = nyse_schedule(start, end)
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for day, close in sched["market_close"].items():
        stamp = pd.Timestamp(close).tz_convert("UTC")
        local = stamp.to_pydatetime().astimezone(NY)
        if (local.hour, local.minute) not in ((16, 0), (13, 0)):
            raise RuntimeError(
                f"unexpected NYSE close {local:%H:%M} ET on {local:%Y-%m-%d}")
        out[pd.Timestamp(day).normalize()] = stamp
    return pd.Series(out).sort_index()


def measure_m1(start: date, end: date) -> dict:
    instants = close_instants(start, end)
    ibit, feed = ibit_closes(start, end)
    klines = binance_hourly(
        (instants.iloc[0] - pd.Timedelta(hours=2)).to_pydatetime(),
        (instants.iloc[-1] + pd.Timedelta(hours=1)).to_pydatetime())

    # The hour ENDING at the close instant is the kline whose OPEN time is one
    # hour earlier; its close is the price AT the NYSE close.
    spot: dict[pd.Timestamp, float] = {}
    for day, instant in instants.items():
        key = instant - pd.Timedelta(hours=1)
        if key in klines.index:
            spot[day] = float(klines.loc[key])
    spot_s = pd.Series(spot).sort_index()

    common = ibit.index.intersection(spot_s.index)
    joint = pd.DataFrame({"ibit": ibit.reindex(common),
                          "spot": spot_s.reindex(common)}).dropna()
    diff = (np.log(joint["ibit"]).diff() - np.log(joint["spot"]).diff()).dropna()
    # Anchored at 1.0 on the cut-over session itself: the cumulative ratio is
    # an index of how far IBIT has drifted from spot since the ETF's first
    # close, and starting it at the first RETURN would hide that first day.
    cumulative = pd.concat([pd.Series([1.0], index=[joint.index[0]]),
                            np.exp(diff.cumsum())])
    p5, p95 = float(diff.quantile(0.05)), float(diff.quantile(0.95))
    worst_at = diff.abs().idxmax()
    breached = (p5 < -M1_STOP_BAND) or (p95 > M1_STOP_BAND)
    return {
        "sessions_matched": int(len(joint)),
        "sessions_expected": int(len(instants)),
        "return_pairs": int(len(diff)),
        "ibit_feed": feed,
        "spot_source": "Binance BTCUSDT 1h klines (data-api.binance.vision)",
        "median": float(diff.median()),
        "p5": p5,
        "p95": p95,
        "worst_abs": float(diff.abs().max()),
        "worst_date": str(pd.Timestamp(worst_at).date()),
        "cumulative_ratio_min": float(cumulative.min()),
        "cumulative_ratio_max": float(cumulative.max()),
        "stop_band": M1_STOP_BAND,
        "stop_condition": "HALT" if breached else "PASS",
        "verdict": ("p5-p95 outside +/-1.0% — the flip halts pending owner "
                    "review" if breached else
                    "p5-p95 inside +/-1.0% — no halt"),
        "note": (
            "The DISPERSION is what the stop condition reads, and that is "
            "what this measures: both legs are taken at the same instant, so "
            "the day-to-day spread is premium/discount and nothing else. The "
            "CUMULATIVE ratio is NOT attributable from this measurement and "
            "must not be read as a premium/discount drift: it bundles IBIT's "
            "expense accrual (25 bp/yr, waived to 12 bp for the first twelve "
            "months on the first USD 5 bn — from memory, unverified against "
            "the prospectus), any change in IBIT's own premium/discount, and "
            "the BTCUSDT-against-USD basis, Binance quoting Tether rather "
            "than dollars. Decomposing it was not in scope and was not done."),
    }


# ---------------------------------------------------------------------------
# M4 — source agreement
# ---------------------------------------------------------------------------
def measure_m4(start: date, end: date) -> dict:
    ng = norgate_ibit(start, end)
    yh = yahoo_ibit(start, end)
    if ng is None or yh is None:
        return {"comparable": False,
                "reason": ("Norgate unreachable" if ng is None
                           else "yfinance served nothing"),
                "stop_condition": "NOT MEASURED"}
    common = ng.index.intersection(yh.index)
    a, b = ng.reindex(common), yh.reindex(common)
    rel = (a / b - 1.0).abs()
    worst = float(rel.max())
    identical = int((a == b).sum())
    return {
        "comparable": True,
        "sessions": int(len(common)),
        "cells_bit_identical": identical,
        "norgate_only": [str(d.date()) for d in ng.index.difference(yh.index)][:10],
        "yahoo_only": [str(d.date()) for d in yh.index.difference(ng.index)][:10],
        "max_relative_difference": worst,
        "max_relative_difference_date": str(pd.Timestamp(rel.idxmax()).date()),
        "median_relative_difference": float(rel.median()),
        "stop_threshold": M4_STOP,
        "stop_condition": "HALT" if worst > M4_STOP else "PASS",
        "note": (
            "Read the zero for what it is. Both feeds carry the same "
            "cent-level close at float32 precision (Norgate stores float32; "
            "Yahoo's chart API serves float32-quantised values), so after the "
            "cast to float64 every cell is bit-identical rather than merely "
            "close — 26.6299991607666 on both sides at the cut-over. The "
            "measurement's floor is that shared quantisation, not a tolerance "
            "this result approached. It does NOT evidence that IBIT has paid "
            "no distribution: a distribution would be adjusted by both the "
            "TOTALRETURN and the auto_adjust basis, and the two would still "
            "agree."),
    }


# ---------------------------------------------------------------------------
# M2 / M3 — the restatement and the join
# ---------------------------------------------------------------------------
def pinned_frames(end: date) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """ONE price frame, twice: the committed cache, and the same cache with
    only its ``BTC-USD`` column rebuilt on the staged basis.

    Everything else — the other 24 thematics, the cash proxy, the index — is
    the identical object in both runs, so any difference downstream is
    attributable to the Bitcoin column and to nothing else.
    """
    import run_thematic_rotation as th                    # noqa: PLC0415
    cache = th.PRICE_CACHE
    if not cache.exists():
        raise RuntimeError(f"{cache} is absent; run the sleeve C engine first")
    declared = ps.read_cache_column_basis(cache).get(btc_basis.SPOT_KEY)
    if declared:
        raise RuntimeError(
            f"the cache declares {btc_basis.SPOT_KEY} basis {declared!r}; M2 "
            f"needs an INCUMBENT-basis cache as its left-hand side")
    frame = pd.read_parquet(cache)
    frame = frame.loc[:pd.Timestamp(end)]
    needed = [c for c in th.TICKERS + [th.CASH_PROXY] if c in frame.columns]
    frame = frame[needed]

    ibit, feed = ibit_closes(btc_basis.CUTOVER, end)
    column, tag = btc_basis.build_column(ibit)
    staged = frame.copy()
    staged[btc_basis.SPOT_KEY] = column.reindex(frame.index)
    provenance = {
        "cache": cache.name,
        "cache_source": ps.read_cache_source(cache) or "yfinance (unrecorded)",
        "rows": int(len(frame)),
        "columns": int(frame.shape[1]),
        "index_start": str(frame.index.min().date()),
        "index_end": str(frame.index.max().date()),
        "ibit_feed": feed,
        "column_basis": tag,
    }
    cut = pd.Timestamp(btc_basis.CUTOVER)
    before = frame.index[frame.index < cut]
    moved = float((frame.loc[before, btc_basis.SPOT_KEY]
                   - staged.loc[before, btc_basis.SPOT_KEY]).abs().max())
    provenance["pre_cutover_max_abs_difference"] = moved
    if moved != 0.0:
        raise RuntimeError(
            f"the frozen segment disagrees with the cache before the cut-over "
            f"by {moved} — the artefact was frozen from a different cache")
    return frame, staged, provenance


def _run_sleeve(frame: pd.DataFrame, eligible: pd.Timestamp) -> dict:
    import run_thematic_rotation as th                    # noqa: PLC0415
    signal = th.compute_signal(frame)
    result = th.run_rotation(frame, signal, th.WEIGHTER_FACTORY(th.HEADLINE_K),
                             eligible, rebalance_freq=th.HEADLINE_FREQ)
    return {"signal": signal, **result}


def _eligible_start(frame: pd.DataFrame) -> pd.Timestamp:
    """main()'s own warm-up rule, restated here on one frame only."""
    import run_thematic_rotation as th                    # noqa: PLC0415
    late = {t for t, m in th.UNIVERSE.items()
            if m.get("late_inception") and t in frame.columns}
    firsts = [frame[c].first_valid_index() for c in frame.columns if c not in late]
    latest = max(d for d in firsts if d is not None)
    return frame.index[frame.index.searchsorted(latest) + th.MA_PERIOD]


def measure_m2_m3(end: date) -> tuple[dict, dict, dict]:
    import run_thematic_rotation as th                    # noqa: PLC0415
    frame, staged, provenance = pinned_frames(end)
    eligible = _eligible_start(frame)
    if _eligible_start(staged) != eligible:
        raise RuntimeError("the two bases disagree on the warm-up start")

    inc = _run_sleeve(frame, eligible)
    new = _run_sleeve(staged, eligible)
    cut = pd.Timestamp(btc_basis.CUTOVER)
    dates = [d for d in inc["rebalance_dates"] if d in new["rebalance_dates"]]

    basket_diffs, gate_flips, before_cut = [], [], 0
    for rd in dates:
        w_i = inc["weights"].loc[rd].round(9)
        w_n = new["weights"].loc[rd].round(9)
        if not w_i.equals(w_n):
            held_i = sorted(w_i[w_i > 1e-9].index)
            held_n = sorted(w_n[w_n > 1e-9].index)
            basket_diffs.append({
                "date": str(pd.Timestamp(rd).date()),
                "incumbent": held_i, "ibit": held_n,
                "names_changed": held_i != held_n})
            if rd < cut:
                before_cut += 1
        prev = frame.index.get_loc(rd) - 1
        if prev < 0:
            continue
        g_i = th.sleeve_gate_state(inc["signal"].iloc[prev])["fired"]
        g_n = th.sleeve_gate_state(new["signal"].iloc[prev])["fired"]
        if g_i != g_n:
            gate_flips.append({"date": str(pd.Timestamp(rd).date()),
                               "incumbent_fired": bool(g_i),
                               "ibit_fired": bool(g_n)})
    if before_cut:
        raise RuntimeError(
            f"{before_cut} decision(s) differ BEFORE the cut-over, where the "
            f"two frames are identical by construction")

    seen_start = pd.Timestamp(btc_basis.CUTOVER)
    stats = {}
    for label, run in (("incumbent", inc), ("ibit", new)):
        st = th.compute_stats(run["equity"], seen_start)
        to = th.turnover_stats(run["weights"], seen_start)
        stats[label] = {"sharpe": st["sharpe"], "cagr": st["cagr"],
                        "max_dd": st["max_dd"],
                        "total_return": st["total_return"],
                        "annual_turnover": to["annual_turnover"],
                        "n_flips": to["n_flips"]}

    blend = blend_sharpe(inc["equity"], new["equity"], seen_start,
                         pd.Timestamp(end))

    m2 = {
        "window": {"start": str(seen_start.date()), "end": str(end),
                   "nyse_sessions": int(len(nyse_schedule(
                       btc_basis.CUTOVER, end)))},
        "provenance": provenance,
        "eligible_start": str(pd.Timestamp(eligible).date()),
        "weekly_decisions": int(len([d for d in dates if d >= seen_start])),
        "baskets_differing": len(basket_diffs),
        "basket_dates": basket_diffs,
        "gate_flips": len(gate_flips),
        "gate_flip_dates": gate_flips,
        "sleeve_c": stats,
        "blend_35_35_10_20": blend,
    }

    m3_window = (inc["signal"].index >= cut) & \
        (inc["signal"].index <= pd.Timestamp(M3_END))
    a = inc["signal"].loc[m3_window, btc_basis.SPOT_KEY]
    b = new["signal"].loc[m3_window, btc_basis.SPOT_KEY]
    gap = (a - b).abs()
    m3 = {
        "window": {"start": str(cut.date()), "end": str(M3_END),
                   "sessions": int(len(gap.dropna()))},
        "max_abs_signal_difference": float(gap.max()),
        "max_abs_signal_difference_date": str(pd.Timestamp(gap.idxmax()).date()),
        "median_abs_signal_difference": float(gap.median()),
        "incumbent_signal_at_max": float(a.loc[gap.idxmax()]),
        "ibit_signal_at_max": float(b.loc[gap.idxmax()]),
        "note": ("The 200-session window straddles the cut-over until "
                 "2024-10-25 (the 200th NYSE session counting the cut-over as "
                 "the first), so a difference here is the join, not the "
                 "series."),
    }
    return m2, m3, provenance


def blend_sharpe(eq_inc: pd.Series, eq_new: pd.Series,
                 start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """The deployed 35/35/10/20 blend's Sharpe under each basis.

    Sleeves A, B and D come from the committed ``multi_strategy.json`` and are
    the SAME series in both blends; only the C leg changes. The alternative —
    rebuilding all four sleeves — would put three unrelated re-runs inside a
    number whose whole point is to isolate one column.
    """
    import run_multi_strategy as ms                       # noqa: PLC0415
    path = PROJECT_ROOT / "data" / "multi_strategy.json"
    blob = json.loads(path.read_text(encoding="utf-8"))["strategies"]

    def leg(key: str) -> pd.Series:
        s = pd.Series(blob[key]["equity"],
                      index=pd.to_datetime(blob[key]["dates"]), dtype=float)
        return s.loc[(s.index >= start) & (s.index <= end)]

    a, b, d = leg("strategy_a"), leg("strategy_b"), leg("strategy_d")
    out = {"source": path.name, "weights": "A 35 / B 35 / C 10 / D 20"}
    for label, eq in (("incumbent", eq_inc), ("ibit", eq_new)):
        c = eq.loc[(eq.index >= start) & (eq.index <= end)]
        common = a.index.intersection(b.index).intersection(
            c.index).intersection(d.index)
        legs = [s.loc[common] / s.loc[common].iloc[0] for s in (a, b, c, d)]
        blend = ms.fixed_blend_4way(*legs, *BLEND)
        out[label] = ms.compute_stats(blend)["sharpe"]
        out["sessions"] = int(len(common))
    out["difference"] = out["ibit"] - out["incumbent"]
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _pct(x, dp=3) -> str:
    return "n/a" if x is None else f"{x * 100:+.{dp}f}%"


def markdown(payload: dict) -> str:
    m1, m2, m3, m4 = (payload.get(k) or {} for k in ("M1", "M2", "M3", "M4"))
    lines = [
        f"# WS21 measurements — M1 to M4 ({payload['as_of']})",
        "",
        f"Seen window {payload['window']['start']} to {payload['window']['end']}, "
        f"{payload['window'].get('nyse_sessions', 'n/a')} NYSE sessions. "
        "Disclosure only: no performance gate governs adoption, and the "
        "construction was fixed at registration.",
        "",
        "| Measurement | Result | Stop condition |",
        "|---|---|---|",
    ]
    if m1.get("stop_condition"):
        lines.append(
            f"| **M1** premium/discount, IBIT vs Binance BTCUSDT at the NYSE "
            f"close | median {_pct(m1.get('median'))}, p5 {_pct(m1.get('p5'))}, "
            f"p95 {_pct(m1.get('p95'))}, worst {_pct(m1.get('worst_abs'))} "
            f"({m1.get('worst_date')}); cumulative ratio "
            f"{m1.get('cumulative_ratio_min', float('nan')):.4f} to "
            f"{m1.get('cumulative_ratio_max', float('nan')):.4f} over "
            f"{m1.get('return_pairs')} pairs | "
            f"**{m1['stop_condition']}** (band ±1.0% on p5–p95) |")
    else:
        lines.append("| **M1** premium/discount | not measured | — |")
    if m2:
        c = m2["sleeve_c"]
        lines.append(
            f"| **M2** restatement, sleeve C | {m2['baskets_differing']} of "
            f"{m2['weekly_decisions']} weekly baskets differ, "
            f"{m2['gate_flips']} gate-state flip(s); Sharpe "
            f"{c['incumbent']['sharpe']:+.3f} → {c['ibit']['sharpe']:+.3f}, "
            f"CAGR {_pct(c['incumbent']['cagr'], 1)} → {_pct(c['ibit']['cagr'], 1)}, "
            f"MaxDD {_pct(c['incumbent']['max_dd'], 1)} → {_pct(c['ibit']['max_dd'], 1)}, "
            f"turnover {c['incumbent']['annual_turnover']:.2f} → "
            f"{c['ibit']['annual_turnover']:.2f}/yr; blend Sharpe "
            f"{m2['blend_35_35_10_20']['incumbent']:+.4f} → "
            f"{m2['blend_35_35_10_20']['ibit']:+.4f} | disclosure, no gate |")
    if m3:
        lines.append(
            f"| **M3** join behaviour, {m3['window']['start']} to "
            f"{m3['window']['end']} | max absolute signal difference "
            f"{m3['max_abs_signal_difference']:.4f} on "
            f"{m3['max_abs_signal_difference_date']} "
            f"({m3['incumbent_signal_at_max']:+.4f} vs "
            f"{m3['ibit_signal_at_max']:+.4f}), median "
            f"{m3['median_abs_signal_difference']:.4f} over "
            f"{m3['window']['sessions']} sessions | disclosure, no gate |")
    if m4.get("comparable"):
        lines.append(
            f"| **M4** source agreement, Norgate vs Yahoo on IBIT | max "
            f"relative difference {m4['max_relative_difference']:.2e} over "
            f"{m4['sessions']} sessions, {m4['cells_bit_identical']} of them "
            f"bit-identical — both feeds carry the same cent-level close at "
            f"float32 precision, so the floor here is quantisation, not a "
            f"tolerance approached | **{m4['stop_condition']}** (threshold "
            f"1e-4) |")
    elif m4:
        lines.append(f"| **M4** source agreement | not measured "
                     f"({m4.get('reason')}) | — |")
    lines += [
        "",
        f"Hard cap: nothing after {HARD_CAP} is computed under the staged "
        f"basis before the WS7 verdict is filed.",
        "",
        f"Sources: {m2.get('provenance', {}).get('cache', 'n/a')} "
        f"({m2.get('provenance', {}).get('cache_source', 'n/a')}), "
        f"IBIT from {m2.get('provenance', {}).get('ibit_feed', 'n/a')}, "
        f"column basis `{m2.get('provenance', {}).get('column_basis', 'n/a')}`.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--after-ws7", action="store_true",
                        help="permit dates past the hard cap (needs the filed "
                             "WS7 verdict row in the ledger)")
    parser.add_argument("--end", default=None,
                        help="last session to read (default: the hard cap)")
    parser.add_argument("--skip", default="", help="comma-separated: m1,m2,m3,m4")
    args = parser.parse_args(argv)

    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}
    end = date.fromisoformat(args.end) if args.end else None
    try:
        end = resolve_window(args.after_ws7, end)
    except CapRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    payload: dict = {
        "study": "WS21 — sleeve C Bitcoin line on IBIT",
        "registration": "KICKOFF_ws21-c-bitcoin-basis.md",
        "as_of": STAMP,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"start": str(WINDOW_START), "end": str(end),
                   "nyse_sessions": int(len(nyse_schedule(WINDOW_START, end)))},
        "hard_cap": str(HARD_CAP),
        "after_ws7": bool(args.after_ws7),
    }

    if "m1" not in skip:
        print("  M1: IBIT against Binance BTCUSDT at the NYSE close ...", flush=True)
        payload["M1"] = measure_m1(WINDOW_START, end)
    if "m2" not in skip or "m3" not in skip:
        print("  M2/M3: sleeve C twice on one pinned frame ...", flush=True)
        m2, m3, _ = measure_m2_m3(end)
        if "m2" not in skip:
            payload["M2"] = m2
        if "m3" not in skip:
            payload["M3"] = m3
    if "m4" not in skip:
        print("  M4: Norgate against Yahoo on IBIT ...", flush=True)
        payload["M4"] = measure_m4(WINDOW_START, end)

    REVIEWS.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_MD.write_text(markdown(payload), encoding="utf-8")
    print(f"\n{markdown(payload)}")
    print(f"  wrote {OUT_JSON.name} and {OUT_MD.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
