"""ETF cross-sectional scanner — daily build.

Fetches prices for the 54-instrument scanner universe, computes every
column and alert, and writes ``data/scanner_latest.json`` for the page
builder. Nothing here touches a strategy engine or a strategy output: the
scanner is a monitoring panel, and its failure must never be able to
disturb the book.

Design points worth knowing before editing:

**Each ticker keeps its own calendar.** Indicators and lookbacks are
positional on the instrument's own bars. Forcing 54 instruments across
NYSE, Xetra and Shenzhen onto one index would either fabricate bars on
foreign holidays or drop real ones, and the cross-section is a ranking of
latest-known values, not a synchronised snapshot. The consequence is that
rows legitimately carry different as-of dates, so every row publishes its
own, and any row behind the panel maximum is flagged — a one-day lag under
a single confident date stamp is a failure this repository has already had
twice.

**FX before indicators.** Xetra lines convert EUR->USD and the Shenzhen
line CNY->USD before anything is computed, so ranks and percentiles are
comparable and consistent with the main site's USD basis. Direction comes
from the resolver, which records it explicitly, because inverting a rate
is a silent scale error rather than a crash.

**Guards abort, they do not warn.** Universe reconciliation, the
cross-sectional invariants, and a naive-recompute divergence check all
raise. A monitoring page has no downstream consumer to notice a wrong
number, so the build failing is the only signal available.

**Parameters are frozen.** Every threshold lives in
``scanner_indicators`` or the ALERT block below, and none has been
validated on this universe. Do not nudge one because a sample day looks
better; that is an out-of-sample question.

Usage:
    python scripts/run_scanner.py
    python scripts/run_scanner.py --skip-snapshots      # no Ticker.info calls
    python scripts/run_scanner.py --full                # ignore the cache
    python scripts/run_scanner.py --end 2026-08-01      # EXCLUSIVE end date
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scanner_indicators as si  # noqa: E402
from overlay_state import tilt_display_state  # noqa: E402
from scanner_universe import (  # noqa: E402
    FX_DIVIDE,
    FX_MULTIPLY,
    ScannerRow,
    assert_reconciled,
)

DATA_DIR = ROOT / "data"
CACHE_PATH = DATA_DIR / "scanner_prices_cache.parquet"
SNAPSHOT_PATH = DATA_DIR / "scanner_snapshots.csv"
NAMES_PATH = DATA_DIR / "etf_names.json"
OUT_PATH = DATA_DIR / "scanner_latest.json"
OVERLAY_PATH = DATA_DIR / "risk_overlay.json"
BREADTH_PATH = DATA_DIR / "breadth_csp1.json"

HISTORY_START = "2014-01-01"     # ~10y, comfortably over the 504d window
FETCH_RETRIES = 3
CACHE_OVERLAP_DAYS = 10          # re-fetch a little history to catch restatements
MAX_FETCH_FAILURES = 5           # spec §7: >=5 failures abort the build
STALE_TRADING_DAYS = 3           # spec §7: grey out beyond this
BENCHMARK = "SPY"                # spec §3.13: one benchmark for every row

# --- Alert thresholds (frozen, unvalidated — spec §4 and §8) --------------
ALERT_SIGMA_MOVE = 2.0           # |return| > 2 sigma of 20d daily returns
ALERT_VOLUME_MULTIPLE = 3.0      # volume > 3x its 20d average
ALERT_RSI_HIGH = 75.0
ALERT_RSI_LOW = 25.0
SQUEEZE_RV_PCTL = 25.0           # squeeze needs BOTH low
SQUEEZE_BBW_PCTL = 10.0
SQUEEZE_RELEASE_SIGMA = 1.5
PD_SIGMA_MIN_OBS = 120           # sessions of snapshots before the P/D alert arms
SO_FLOW_ALERT = 0.01             # |daily change in shares outstanding| > 1%

# Chip priority when more than 12 fire (spec §4)
ALERT_PRIORITY = {
    "etf_layer": 0, "squeeze": 1, "squeeze_release": 1,
    "ma200_cross": 2, "52w": 2, "sigma_move": 3, "rsi": 4, "volume": 5,
}
MAX_CHIPS = 12

# Gate thresholds are the deployed ones, imported not restated (spec §5)
from run_risk_overlay import OFF_THRESHOLD, ON_THRESHOLD  # noqa: E402


class ScannerBuildError(RuntimeError):
    """Raised when a guard fails. The build stops; the page is not written."""


@dataclass
class TickerData:
    """One instrument's USD-converted OHLCV on its own calendar."""

    ticker: str
    frame: pd.DataFrame          # date-indexed open/high/low/close/volume
    fx_applied: str | None = None

    @property
    def as_of(self) -> pd.Timestamp:
        return self.frame.index[-1]

    @property
    def close(self) -> pd.Series:
        return self.frame["close"]


@dataclass
class Alert:
    ticker: str
    kind: str
    label: str
    value: str = ""

    @property
    def priority(self) -> int:
        return ALERT_PRIORITY.get(self.kind, 9)


@dataclass
class BuildReport:
    """What the run did, for the footer and the operator log."""

    failures: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# =========================================================================
# Price fetch and cache
# =========================================================================
def _download(symbols: list[str], start: str, end: str | None) -> pd.DataFrame:
    """Adjusted OHLCV for ``symbols``, long format. yfinance end is EXCLUSIVE."""
    import yfinance as yf

    raw = yf.download(
        symbols, start=start, end=end, auto_adjust=True,
        progress=False, threads=False, group_by="column",
    )
    if raw is None or raw.empty:
        return pd.DataFrame()

    frames = []
    fields = ("Open", "High", "Low", "Close", "Volume")
    single = not isinstance(raw.columns, pd.MultiIndex)
    for symbol in symbols:
        cols = {}
        for f in fields:
            try:
                cols[f.lower()] = raw[f] if single else raw[(f, symbol)]
            except KeyError:
                cols = {}
                break
        if not cols:
            continue
        frame = pd.DataFrame(cols).dropna(how="all")
        if frame.empty:
            continue
        frame.insert(0, "ticker", symbol)
        frames.append(frame)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).reset_index()
    out = out.rename(columns={out.columns[0]: "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    return out


def fetch_prices(
    symbols: list[str], end: str | None, full: bool, report: BuildReport
) -> pd.DataFrame:
    """Incremental fetch against the parquet cache, returned long format.

    Re-fetches ``CACHE_OVERLAP_DAYS`` of already-cached history so vendor
    restatements land rather than being permanently shadowed by the cache.
    """
    cached = pd.DataFrame()
    if CACHE_PATH.exists() and not full:
        cached = pd.read_parquet(CACHE_PATH)
        cached = cached[cached["ticker"].isin(symbols)]

    if cached.empty:
        start = HISTORY_START
        print(f"  no usable cache — full fetch from {start}")
    else:
        last = cached["date"].max()
        start = (last - pd.Timedelta(days=CACHE_OVERLAP_DAYS)).strftime("%Y-%m-%d")
        print(f"  cache to {last.date()} — incremental fetch from {start}")

    fresh = pd.DataFrame()
    for attempt in range(1, FETCH_RETRIES + 1):
        fresh = _download(symbols, start, end)
        if not fresh.empty:
            break
        print(f"  fetch attempt {attempt}/{FETCH_RETRIES} returned nothing")

    combined = pd.concat([cached, fresh]) if not cached.empty else fresh
    if combined.empty:
        raise ScannerBuildError(
            "no price data at all — refusing to build a page from nothing"
        )
    # Fresh rows win on a duplicate date, which is how restatements land.
    combined = (
        combined.sort_values(["ticker", "date"])
        .drop_duplicates(subset=["ticker", "date"], keep="last")
        .reset_index(drop=True)
    )

    # A row with no close is not a bar. The vendor emits placeholder rows for
    # a session it has not yet populated — on 2026-08-04 every US ticker came
    # back with a 2026-08-03 row of pure NaN — and carrying one poisons
    # everything downstream: it becomes the ticker's as_of, and every
    # indicator reading for the current bar goes NaN, which the percentile
    # helper then correctly withholds and the invariant check then correctly
    # rejected. Dropping them here fixes all of that at the source, and keeps
    # placeholder rows out of the cache too.
    before = len(combined)
    combined = combined[combined["close"].notna()].reset_index(drop=True)
    dropped = before - len(combined)
    if dropped:
        print(f"  dropped {dropped} placeholder row(s) with no close")

    missing = sorted(set(symbols) - set(combined["ticker"].unique()))
    for m in missing:
        report.failures.append(f"{m}: no price data")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(CACHE_PATH, index=False)
    return combined


def apply_fx(
    row: ScannerRow, frame: pd.DataFrame, fx_frames: dict[str, pd.Series]
) -> tuple[pd.DataFrame, str | None]:
    """Convert one instrument's OHLC to USD on its own calendar.

    Volume is NOT scaled — it is a share count, not a price. The FX series
    is aligned with the repository's freshness-capped helper, so a stalled
    rate degrades to NaN instead of silently freezing the last rate.
    """
    if not row.fx_ticker:
        return frame, None

    from alignment import align_series_to_index

    fx = fx_frames.get(row.fx_ticker)
    if fx is None or fx.empty:
        raise ScannerBuildError(
            f"{row.scan_ticker}: {row.fx_ticker} unavailable — refusing to "
            f"publish a {row.currency} series as if it were USD"
        )
    aligned = align_series_to_index(fx, frame.index, max_stale_days=10)
    out = frame.copy()
    price_cols = ["open", "high", "low", "close"]
    if row.fx_direction == FX_MULTIPLY:
        out[price_cols] = out[price_cols].multiply(aligned, axis=0)
    elif row.fx_direction == FX_DIVIDE:
        out[price_cols] = out[price_cols].divide(aligned, axis=0)
    else:  # pragma: no cover — resolver guarantees one of the two
        raise ScannerBuildError(f"{row.scan_ticker}: unknown FX direction")
    out = out.dropna(subset=["close"])
    if out.empty:
        raise ScannerBuildError(
            f"{row.scan_ticker}: FX conversion left no usable bars"
        )
    return out, f"{row.currency}->USD via {row.fx_ticker} ({row.fx_direction})"


# =========================================================================
# Per-ticker columns
# =========================================================================
def horizon_returns(data: TickerData, offset: int = 0) -> dict[int, float]:
    """Total return over each rank horizon, on this ticker's own bars.

    ``offset`` steps the whole calculation back N of the ticker's own
    sessions, which is how delta-R is derived without persisting ranks.
    """
    close = data.close if offset == 0 else data.close.iloc[:-offset]
    return {h: si.total_return(close, h) for h in si.RANK_HORIZONS}


def build_columns(data: TickerData, benchmark: pd.Series) -> dict:
    """Every non-rank column for one row (spec §3)."""
    frame, close = data.frame, data.close
    high, low, volume = frame["high"], frame["low"], frame["volume"]

    rv = si.percentile_of_latest(si.realised_vol(close))
    bbw = si.percentile_of_latest(si.bollinger_bandwidth(close))

    return {
        "trend": si.trend_state(close),
        "mom_12_1": si.momentum_12_1(close),
        "vs_52w_high": si.vs_52w_high(close),
        "rv_pctl": rv.value,
        "rv_truncated": rv.truncated,
        "bbw_pctl": bbw.value,
        "bbw_truncated": bbw.truncated,
        "atr_pct": si.atr_pct(high, low, close),
        "ret_1d": si.total_return(close, 1),
        "vol_ratio": si.volume_ratio(volume),
        # Both emitted: with a single benchmark, RS is the row's own 1M
        # return shifted by one constant, so the page picks which to show.
        "ret_1m": si.total_return(close, si.MOMENTUM_SKIP),
        "rs_1m": (
            None if data.ticker == BENCHMARK
            else si.relative_strength_1m(close, benchmark)
        ),
        "dev_200d": si.dev_from_ma(close),
        "rsi14": float(si.rsi(close).iloc[-1]),
        "n_bars": int(len(close)),
    }


# =========================================================================
# Alerts
# =========================================================================
def build_alerts(data: TickerData, cols: dict) -> list[Alert]:
    """Event chips for one row (spec §4). ETF-layer rules are added later."""
    out: list[Alert] = []
    close = data.close
    t = data.ticker

    window = close.iloc[-si.TRADING_DAYS_YEAR:]
    if len(window) >= si.TRADING_DAYS_YEAR:
        if close.iloc[-1] >= window.max():
            out.append(Alert(t, "52w", "52-week high"))
        elif close.iloc[-1] <= window.min():
            out.append(Alert(t, "52w", "52-week low"))

    ma200 = si.sma(close, si.MA_LONG)
    if len(close) > si.MA_LONG and np.isfinite(ma200.iloc[-2]):
        was_below = close.iloc[-2] < ma200.iloc[-2]
        is_below = close.iloc[-1] < ma200.iloc[-1]
        if was_below and not is_below:
            out.append(Alert(t, "ma200_cross", "Crossed above MA200"))
        elif not was_below and is_below:
            out.append(Alert(t, "ma200_cross", "Crossed below MA200"))

    daily = close.pct_change()
    sigma = daily.iloc[-si.RV_WINDOW - 1:-1].std(ddof=1)
    last = daily.iloc[-1]
    if np.isfinite(sigma) and sigma > 0 and abs(last) > ALERT_SIGMA_MOVE * sigma:
        out.append(
            Alert(t, "sigma_move",
                  f"{abs(last) / sigma:.1f}-sigma move", f"{last * 100:+.1f}%")
        )

    vr = cols.get("vol_ratio")
    if vr is not None and np.isfinite(vr) and vr > ALERT_VOLUME_MULTIPLE:
        out.append(Alert(t, "volume", f"Volume {vr:.1f}x 20D"))

    rv_p, bbw_p = cols.get("rv_pctl"), cols.get("bbw_pctl")
    in_squeeze = (
        rv_p is not None and bbw_p is not None
        and np.isfinite(rv_p) and np.isfinite(bbw_p)
        and rv_p < SQUEEZE_RV_PCTL and bbw_p < SQUEEZE_BBW_PCTL
    )
    if in_squeeze:
        if np.isfinite(sigma) and sigma > 0 and abs(last) > SQUEEZE_RELEASE_SIGMA * sigma:
            out.append(Alert(t, "squeeze_release", "Squeeze release"))
        else:
            out.append(
                Alert(t, "squeeze", "Squeeze (RV & BBW low)",
                      f"RV p{rv_p:.0f} / BBW p{bbw_p:.0f}")
            )

    rsi = cols.get("rsi14")
    if rsi is not None and np.isfinite(rsi):
        if rsi >= ALERT_RSI_HIGH:
            out.append(Alert(t, "rsi", f"RSI {rsi:.0f} overbought"))
        elif rsi <= ALERT_RSI_LOW:
            out.append(Alert(t, "rsi", f"RSI {rsi:.0f} oversold"))
    return out


def rank_alerts(alerts: list[Alert]) -> tuple[list[Alert], int]:
    """Priority-order the chips and report how many were truncated."""
    ordered = sorted(alerts, key=lambda a: (a.priority, a.ticker))
    return ordered[:MAX_CHIPS], max(0, len(ordered) - MAX_CHIPS)


# =========================================================================
# ETF layer — NAV / shares-outstanding snapshots
# =========================================================================
def fetch_snapshots(tickers: list[str], observed_on: str) -> pd.DataFrame:
    """Same-day navPrice / sharesOutstanding for each ticker.

    yfinance exposes no history for either field, so the panel accrues one
    row per ticker per run. Failures are expected and silent by design:
    spec §6 forbids filling a missing NAV with a proxy. In practice only
    the Shenzhen line comes back empty — the Xetra funds DO report
    navPrice, contrary to the spec's expectation.

    ``observed_on`` is the real calendar date of the ``info`` call, NOT the
    price panel's as-of. Those differ whenever the build runs before a
    venue's data lands or with a backdated ``--end``, and stamping an
    observation with the panel's date would silently pair a NAV from one
    session with a close from another. The NAV and the close in each row
    come from the SAME info call, so the premium/discount is internally
    consistent regardless of where the price panel sits — which is exactly
    why the row must be labelled by when it was observed.
    """
    import yfinance as yf

    rows = []
    for ticker in tickers:
        nav = so = close = None
        try:
            info = yf.Ticker(ticker).info or {}
            nav = info.get("navPrice")
            so = info.get("sharesOutstanding")
            close = info.get("previousClose")
        except Exception:  # noqa: BLE001 — a snapshot miss must not fail the build
            pass
        rows.append({
            "date": observed_on, "ticker": ticker,
            "nav": nav, "so": so, "close": close,
        })
    return pd.DataFrame(rows)


def append_snapshots(fresh: pd.DataFrame) -> pd.DataFrame:
    """Append to the snapshot CSV idempotently (spec §9.5).

    Re-running on the same day replaces that day's rows rather than
    duplicating them, so an operator can run the build twice without
    corrupting the accruing history.
    """
    existing = pd.DataFrame()
    if SNAPSHOT_PATH.exists():
        existing = pd.read_csv(SNAPSHOT_PATH)
    combined = pd.concat([existing, fresh]) if not existing.empty else fresh
    combined = (
        combined.sort_values(["ticker", "date"])
        .drop_duplicates(subset=["date", "ticker"], keep="last")
        .reset_index(drop=True)
    )
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(SNAPSHOT_PATH, index=False)
    return combined


def etf_layer(ticker: str, snapshots: pd.DataFrame) -> dict:
    """Premium/discount and 5-day share-count flow for one row (spec §3, §6)."""
    blank = {"pd_pct": None, "flow_5d": None, "pd_alert": None, "flow_alert": None}
    if snapshots.empty:
        return blank
    own = snapshots[snapshots["ticker"] == ticker].sort_values("date")
    if own.empty:
        return blank

    out = dict(blank)
    latest = own.iloc[-1]
    nav, close = latest.get("nav"), latest.get("close")
    if pd.notna(nav) and pd.notna(close) and float(nav) != 0:
        pd_pct = float(close) / float(nav) - 1.0
        out["pd_pct"] = pd_pct
        history = own["nav"].notna().sum()
        if history >= PD_SIGMA_MIN_OBS:
            # Sound even against a stale NAV: a persistent measurement bias
            # becomes part of this ticker's own mean, so only a genuine
            # deviation from its usual reading fires.
            series = (own["close"] / own["nav"] - 1.0).dropna()
            mean, sd = series.mean(), series.std(ddof=1)
            if sd and np.isfinite(sd) and abs(pd_pct - mean) > 2 * sd:
                out["pd_alert"] = f"P/D {pd_pct * 100:+.2f}% beyond 2 sigma of 1Y"
        # No interim absolute-threshold alert. Spec §6 proposed one until 120
        # sessions accrue, and the first live run showed it is not fit for
        # purpose: it fired for six of twelve chips on values from -0.4% to
        # -1.2% for BOTZ, GLD, ICLN, LIT, TIP and URA, crowding the genuine
        # events out of the strip. Those are not real premiums. yfinance
        # publishes navPrice with NO as-of date, and the pattern tracks fund
        # SIZE rather than market stress: SPY reads +0.057% and QQQ +0.023%,
        # both plausible, while the smaller funds show large "discounts" that
        # are the price move between a stale NAV strike and a current close.
        # SPY's NAV also arrives as a four-decimal strike (746.6043) where
        # BOTZ's is exactly 35.65, which looks like a price, not a NAV.
        #
        # Since the field carries no date, staleness cannot be detected, only
        # absorbed — which is what the sigma test above does once there is
        # history. So the value is still published, with the footer stating
        # that it is unverifiable, but no ALERT is raised from it. A displayed
        # number with a caveat is data; a chip is an assertion, and this
        # measurement cannot support one.

    so = own["so"].dropna()
    if len(so) >= 6:
        prior, current = float(so.iloc[-6]), float(so.iloc[-1])
        if prior != 0:
            out["flow_5d"] = current / prior - 1.0
    if len(so) >= 2:
        prior, current = float(so.iloc[-2]), float(so.iloc[-1])
        if prior != 0 and abs(current / prior - 1.0) > SO_FLOW_ALERT:
            out["flow_alert"] = (
                f"Shares outstanding {(current / prior - 1.0) * 100:+.1f}% in a day"
            )
    return out


# =========================================================================
# Overlay chips — read deployed state, never recompute (spec §5)
# =========================================================================
def overlay_chips() -> list[dict]:
    chips: list[dict] = []

    if OVERLAY_PATH.exists():
        overlay = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
        breadth = overlay.get("current_breadth")
        chips.append({
            "kind": "breadth_gate",
            "state": overlay.get("current_state"),
            "value": breadth,
            "label": (
                f"Breadth gate · {overlay.get('current_state')} "
                f"(SPX breadth {breadth * 100:.0f}%)"
                if breadth is not None else "Breadth gate · unavailable"
            ),
            "since": overlay.get("current_state_since"),
            "thresholds": {"off": OFF_THRESHOLD, "on": ON_THRESHOLD},
            "as_of": overlay.get("panel_end_date"),
        })
        # tilt_display_state already forces OFF and says so when the
        # EEM/SPY feed is stale past its cap, so the chip inherits the
        # deployed staleness policy instead of inventing one.
        as_of = overlay.get("panel_end_date") or ""
        tilt = tilt_display_state(overlay, as_of)
        chips.append({
            "kind": "em_tilt",
            "state": "ON" if tilt.get("active") else "OFF",
            "stale": bool(tilt.get("stale")),
            "label": f"EM tilt · {tilt.get('label')}",
            "as_of": as_of,
        })
    else:
        chips.append({"kind": "breadth_gate", "state": None,
                      "label": "Breadth gate · data unavailable"})
    return chips


def breadth_panel_date() -> str | None:
    if not BREADTH_PATH.exists():
        return None
    return json.loads(BREADTH_PATH.read_text(encoding="utf-8")).get("end_date")


# =========================================================================
# Guards
# =========================================================================
def assert_invariants(rows: list[dict], expected: int) -> None:
    """Cross-sectional properties the page cannot be allowed to violate."""
    problems: list[str] = []

    if len(rows) != expected:
        problems.append(f"{len(rows)} rows built, resolver expected {expected}")

    ranks = [r["rank"] for r in rows if r["rank"] is not None]
    if ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
        dupes = {r for r in ranks if ranks.count(r) > 1}
        problems.append(
            f"ranks are not a permutation of 1..{len(ranks)} "
            f"(duplicates: {sorted(dupes) or 'none'}, max {max(ranks)})"
        )

    # A withheld percentile is legitimate; an impossible one is not. The
    # helper returns NaN by contract when history is too short to rank
    # against, so failing on every NaN — as this did until 2026-08-04 —
    # sets two of our own guards against each other. The strictness that
    # matters is kept: NaN is only excused where the row genuinely lacks the
    # observations, and any present value must still be in range.
    minimum_bars = {
        "rv_pctl": si.MIN_PCTL_OBS + si.RV_WINDOW,
        "bbw_pctl": si.MIN_PCTL_OBS + si.BBW_WINDOW,
    }
    for row in rows:
        for key, needed in minimum_bars.items():
            value = row.get(key)
            if value is None or not np.isfinite(value):
                if (row.get("n_bars") or 0) >= needed:
                    problems.append(
                        f"{row['ticker']}: {key} is missing despite "
                        f"{row.get('n_bars')} bars (needs {needed})"
                    )
                continue
            if not (0.0 <= value <= 100.0):
                problems.append(f"{row['ticker']}: {key} = {value} outside [0,100]")

    if problems:
        raise ScannerBuildError(
            "cross-sectional invariants failed:\n  - " + "\n  - ".join(problems)
        )


def assert_no_naive_divergence(
    panel: dict[str, TickerData], as_of: str, sample_size: int = 3
) -> list[str]:
    """Vectorised path against the deliberately naive one, on a rotating sample.

    The sample rotates by date rather than at random, so a run is
    reproducible and a resumed or re-run build checks the same tickers.
    This is the only check that can catch a rolling-window regression:
    it changes every number at once and breaks no test that shares the
    implementation under test.
    """
    tickers = sorted(panel)
    if not tickers:
        return []
    seed = sum(ord(c) for c in as_of)
    picked = [tickers[(seed + i) % len(tickers)] for i in range(min(sample_size, len(tickers)))]

    problems: list[str] = []
    for ticker in picked:
        frame = panel[ticker].frame
        close = frame["close"].tolist()
        checks = {
            "sma20": (
                si.sma(frame["close"], 20).iloc[-1],
                si.naive_sma_latest(close, 20),
            ),
            "rsi14": (
                float(si.rsi(frame["close"]).iloc[-1]),
                si.naive_rsi_latest(close),
            ),
            "atr_pct": (
                si.atr_pct(frame["high"], frame["low"], frame["close"]),
                si.naive_atr_pct_latest(
                    frame["high"].tolist(), frame["low"].tolist(), close
                ),
            ),
        }
        for name, (fast, naive) in checks.items():
            if not np.isfinite(fast) and not np.isfinite(naive):
                continue
            if not np.isclose(fast, naive, rtol=1e-9, atol=0):
                problems.append(
                    f"{ticker} {name}: vectorised {fast!r} != naive {naive!r}"
                )
    if problems:
        raise ScannerBuildError(
            "naive-recompute divergence:\n  - " + "\n  - ".join(problems)
        )
    return picked


# =========================================================================
# Build
# =========================================================================
def build(
    end: str | None = None, full: bool = False, skip_snapshots: bool = False
) -> dict:
    report = BuildReport()

    print("Resolving universe ...")
    universe = assert_reconciled()
    scan_tickers = [r.scan_ticker for r in universe]
    fx_tickers = sorted({r.fx_ticker for r in universe if r.fx_ticker})
    print(f"  {len(universe)} instruments, FX series: {', '.join(fx_tickers) or 'none'}")

    print("Fetching prices ...")
    long = fetch_prices(scan_tickers + fx_tickers, end, full, report)

    by_ticker = {
        str(t): g.drop(columns=["ticker"]).set_index("date").sort_index()
        for t, g in long.groupby("ticker")
    }
    fx_series = {
        t: by_ticker[t]["close"] for t in fx_tickers if t in by_ticker
    }

    print("Converting to USD and computing indicators ...")
    panel: dict[str, TickerData] = {}
    for row in universe:
        frame = by_ticker.get(row.scan_ticker)
        if frame is None or frame.empty:
            report.failures.append(f"{row.scan_ticker}: no bars")
            continue
        try:
            converted, note = apply_fx(row, frame, fx_series)
        except ScannerBuildError as exc:
            report.failures.append(str(exc))
            continue
        panel[row.scan_ticker] = TickerData(row.scan_ticker, converted, note)

    if len(report.failures) >= MAX_FETCH_FAILURES:
        raise ScannerBuildError(
            f"{len(report.failures)} instruments failed (limit "
            f"{MAX_FETCH_FAILURES}); refusing to publish a partial panel:\n  - "
            + "\n  - ".join(report.failures)
        )

    panel_as_of = max(d.as_of for d in panel.values())
    as_of_iso = panel_as_of.strftime("%Y-%m-%d")

    benchmark = panel[BENCHMARK].close if BENCHMARK in panel else pd.Series(dtype=float)
    if benchmark.empty:
        report.notes.append(
            f"{BENCHMARK} unavailable — RS column withheld for every row"
        )

    # Ranks: horizon returns per own calendar, then one cross-section.
    now_returns = pd.DataFrame(
        {t: horizon_returns(d) for t, d in panel.items()}
    ).T
    rank_now = si.rank_from_horizon_returns(now_returns)
    prior_returns = pd.DataFrame(
        {
            t: horizon_returns(d, offset=si.SLOPE_LOOKBACK)
            for t, d in panel.items()
            if len(d.close) > si.SLOPE_LOOKBACK + max(si.RANK_HORIZONS)
        }
    ).T
    rank_prior = (
        si.rank_from_horizon_returns(prior_returns)
        if not prior_returns.empty else None
    )

    snapshots = pd.DataFrame()
    # The ETF layer is observed now, not as of the price panel — see
    # fetch_snapshots. Kept as a separate date so the page can label the
    # P/D column with its own as-of rather than borrowing the prices one.
    observed_on = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not skip_snapshots:
        print(f"Fetching NAV / shares-outstanding snapshots for {len(panel)} ...")
        snapshots = append_snapshots(fetch_snapshots(sorted(panel), observed_on))
        today = snapshots[snapshots["date"] == observed_on]
        print(f"  {today['nav'].notna().sum()} of {len(panel)} returned a NAV, "
              f"{today['so'].notna().sum()} a share count")
        if observed_on != as_of_iso:
            report.notes.append(
                f"ETF layer observed {observed_on}, prices as of {as_of_iso}"
            )
    elif SNAPSHOT_PATH.exists():
        snapshots = pd.read_csv(SNAPSHOT_PATH)
        report.notes.append("snapshots not refreshed this run (--skip-snapshots)")

    names = json.loads(NAMES_PATH.read_text(encoding="utf-8")) if NAMES_PATH.exists() else {}

    rows: list[dict] = []
    alerts: list[Alert] = []
    for row in universe:
        data = panel.get(row.scan_ticker)
        if data is None:
            continue
        cols = build_columns(data, benchmark)
        alerts.extend(build_alerts(data, cols))

        layer = etf_layer(row.scan_ticker, snapshots)
        for key in ("pd_alert", "flow_alert"):
            if layer.get(key):
                alerts.append(Alert(row.scan_ticker, "etf_layer", layer[key]))

        lag = int(np.busday_count(data.as_of.date(), panel_as_of.date()))
        if lag > STALE_TRADING_DAYS:
            report.stale.append(f"{row.scan_ticker}: {lag} sessions behind")

        rank = rank_now.ranks.get(row.scan_ticker)
        prior = rank_prior.ranks.get(row.scan_ticker) if rank_prior else None
        rows.append({
            "ticker": row.scan_ticker,
            "name": row.name or names.get(row.scan_ticker) or row.scan_ticker,
            "long_name": names.get(row.scan_ticker),
            "sleeves": list(row.sleeves),
            "engine_tickers": sorted({o.engine_ticker for o in row.origins}),
            "proxy_notes": list(row.proxy_notes),
            "is_proxied": row.is_proxied,
            "currency": row.currency,
            "fx": data.fx_applied,
            # Every row carries its OWN as-of and its lag behind the panel.
            # One confident date over a mixed panel is the failure mode this
            # repository has already hit twice.
            "as_of": data.as_of.strftime("%Y-%m-%d"),
            "sessions_behind": lag,
            "stale": lag > STALE_TRADING_DAYS,
            "rank": int(rank) if rank is not None else None,
            "rank_delta": (
                int(prior - rank) if (rank is not None and prior is not None) else None
            ),
            "rank_truncated": bool(rank_now.truncated.get(row.scan_ticker, False)),
            **cols,
            **layer,
        })

    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))
    assert_invariants(rows, expected=len(panel))
    checked = assert_no_naive_divergence(panel, as_of_iso)
    report.notes.append(f"naive-recompute check passed on {', '.join(checked)}")

    if rank_now.unrankable:
        report.notes.append(
            f"unrankable (insufficient history): {', '.join(rank_now.unrankable)}"
        )

    chips, truncated = rank_alerts(alerts)
    per_market = {
        market: max(
            d.as_of for t, d in panel.items() if _market_of(t) == market
        ).strftime("%Y-%m-%d")
        for market in sorted({_market_of(t) for t in panel})
    }

    # The headline date needs care. as_of is the panel MAXIMUM, which is the
    # right basis for measuring how far any row lags — but it is the wrong
    # thing to print as "the" date. On 2026-08-04 the maximum was that day,
    # set by two still-open foreign venues, while 48 of 54 rows sat at
    # 2026-07-31 because the vendor had not yet populated the US session. A
    # single confident label over a mixed panel is the failure this repository
    # has already had twice, so the modal date and its share are published
    # alongside and the page leads with those.
    asof_counts = Counter(r["as_of"] for r in rows)
    modal_asof, modal_count = asof_counts.most_common(1)[0]

    return {
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of_iso,
        "as_of_modal": modal_asof,
        "as_of_modal_share": f"{modal_count}/{len(rows)}",
        "as_of_mixed": len(asof_counts) > 1,
        "as_of_per_market": per_market,
        "etf_layer_observed_at": (
            None if snapshots.empty else str(snapshots["date"].max())
        ),
        "breadth_panel_as_of": breadth_panel_date(),
        "n_rows": len(rows),
        "benchmark": BENCHMARK,
        "percentile_window": si.PCTL_WINDOW,
        "parameters_validated": False,
        "parameter_note": (
            "All parameters are industry defaults (RSI 14, MA 20/50/200, ATR 14, "
            "BBW 20/2sigma, percentile window 504d, equal-weight four-horizon "
            "momentum composite) and NONE has been validated on this universe."
        ),
        "overlays": overlay_chips(),
        "alerts": [
            {"ticker": a.ticker, "kind": a.kind, "label": a.label, "value": a.value}
            for a in chips
        ],
        "alerts_truncated": truncated,
        "rows": rows,
        "data_health": {
            "failures": report.failures,
            "stale": report.stale,
            "notes": report.notes,
            "stale_threshold_sessions": STALE_TRADING_DAYS,
        },
    }


def _market_of(ticker: str) -> str:
    if ticker.endswith(".DE"):
        return "DE"
    if ticker.endswith((".SZ", ".SS")):
        return "CN"
    return "US"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--end", default=None,
                        help="EXCLUSIVE end date YYYY-MM-DD (yfinance convention)")
    parser.add_argument("--full", action="store_true", help="ignore the price cache")
    parser.add_argument("--skip-snapshots", action="store_true",
                        help="skip the slow Ticker.info NAV / shares-outstanding pass")
    parser.add_argument("--out", default=None, help="override the output path")
    args = parser.parse_args(argv)

    payload = build(end=args.end, full=args.full, skip_snapshots=args.skip_snapshots)

    out_path = Path(args.out) if args.out else OUT_PATH
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    health = payload["data_health"]
    print(f"\nas-of {payload['as_of']}  ({payload['as_of_per_market']})")
    print(f"rows {payload['n_rows']}, alerts shown {len(payload['alerts'])}")
    if payload["alerts_truncated"]:
        print(f"  {payload['alerts_truncated']} further alerts truncated by priority")
    for note in health["notes"]:
        print(f"  note: {note}")
    for s in health["stale"]:
        print(f"  STALE: {s}")
    for f in health["failures"]:
        print(f"  FAILED: {f}")
    print(f"wrote {out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
