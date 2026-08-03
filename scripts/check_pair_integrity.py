"""Does each breadth-signalled ETF actually move with its own constituents?

Every sleeve A and sleeve D member is a pair: a constituent panel that
produces the breadth signal, and a priced instrument that produces the
return. The two are joined by hand in ``etf_registry.py``, and nothing
downstream notices if they are joined wrongly — the signal computes, the
backtest runs, the book marks, and the numbers are meaningless.

That is not hypothetical. Registry key EXH3 paired an Industrial Goods &
Services panel with EXH3.DE, which is the Food & Beverage fund, and the
error survived from Phase 4 to 2026-08-03 because every label in the repo
restated it consistently. No documentary check could catch it; only a
behavioural one can.

This check correlates each member's priced series against an equal-weight
basket of its own latest constituent snapshot. A correctly paired member
sits far above the floor; the mispaired one sat far below it:

    EXV1.DE 0.987   EXH1.DE 0.935   EXV3.DE 0.943   EXH9.DE 0.984
    EXH3.DE 0.244  <- the defect     EXH4.DE 0.973  <- the correction

Returns are correlated in NATIVE currencies, with no FX conversion. The
clean members above span CHF, GBP, SEK, DKK and EUR constituents against
a EUR fund, and still land between 0.935 and 0.987, so FX noise does not
obscure the signal — and converting would add a fetch and a staleness
policy to a check whose whole value is being simple enough to trust.

Exit codes: 0 all pass or skip, 1 at least one breach. Intended for the
weekly workflow alongside check_capture_integrity.py, not for pytest —
the pure logic is unit-tested in tests/test_check_pair_integrity.py.

Usage:
    python scripts/check_pair_integrity.py
    python scripts/check_pair_integrity.py --etfs EXH3 EXV1 --verbose
    python scripts/check_pair_integrity.py --floor 0.6 --years 3
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from etf_registry import (  # noqa: E402
    ETF_REGISTRY,
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
)

DATA_DIR = ROOT / "data"

# A correctly paired member sits at 0.93+; the mispaired one sat at 0.24.
# 0.70 is placed in the empty middle of that gap, wide of both, so the
# check is decisive without being sensitive to where exactly it is set.
DEFAULT_FLOOR = 0.70
DEFAULT_YEARS = 2
DEFAULT_SAMPLE = 12       # top-N constituents, which arrive weight-ordered
MIN_NAMES = 5             # below this the basket is too thin to conclude
MIN_OBS = 200             # below this the correlation is too noisy to trust

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass(frozen=True)
class PairVerdict:
    etf: str                  # registry key (the panel identifier)
    traded: str               # priced instrument
    correlation: float
    n_names: int
    n_obs: int
    status: str
    note: str = ""


# --------------------------------------------------------------------------
# Pure logic — unit-tested offline
# --------------------------------------------------------------------------
def basket_returns(returns: pd.DataFrame, names: list[str]) -> tuple[pd.Series, int]:
    """Equal-weight mean log return across whichever names have real data.

    Equal weight, not cap weight: the point is to identify the sector, and
    a thin tail of small constituents identifies it just as well as the
    mega-caps while being far less sensitive to which names the snapshot
    happens to list first.
    """
    usable = [
        n for n in names
        if n in returns.columns and returns[n].notna().sum() >= MIN_OBS
    ]
    if not usable:
        return pd.Series(dtype="float64"), 0
    return returns[usable].mean(axis=1), len(usable)


def pair_correlation(priced: pd.Series, basket: pd.Series) -> tuple[float, int]:
    """Pearson correlation of the overlapping observations."""
    joined = pd.concat([priced, basket], axis=1).dropna()
    if len(joined) < 2:
        return float("nan"), len(joined)
    return float(joined.iloc[:, 0].corr(joined.iloc[:, 1])), len(joined)


def classify(
    correlation: float,
    n_names: int,
    n_obs: int,
    floor: float = DEFAULT_FLOOR,
) -> tuple[str, str]:
    """Verdict for one pair.

    Insufficient data is SKIP, never FAIL: a thin fetch must not raise a
    false alarm on a correctly paired member. Only a real, well-evidenced
    breach fails.
    """
    if n_names < MIN_NAMES:
        return SKIP, f"only {n_names} constituents resolved (need {MIN_NAMES})"
    if n_obs < MIN_OBS:
        return SKIP, f"only {n_obs} overlapping observations (need {MIN_OBS})"
    if not np.isfinite(correlation):
        return SKIP, "correlation undefined"
    if correlation < floor:
        return FAIL, (
            f"correlation {correlation:.3f} is below the {floor:.2f} floor — "
            f"the priced instrument does not move with its own constituents"
        )
    return PASS, ""


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
def latest_constituents(etf: str, sample: int = DEFAULT_SAMPLE) -> list[str]:
    """Top-``sample`` tickers from the most recent snapshot on disk."""
    path = DATA_DIR / f"constituents_{etf.lower()}.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshots = payload.get("snapshots") or {}
    for key in sorted(snapshots, reverse=True):
        tickers = (snapshots[key] or {}).get("tickers") or []
        if tickers:
            return list(tickers[:sample])
    return []


def traded_symbol(etf: str) -> str:
    return (ETF_REGISTRY.get(etf) or {}).get("yfinance_trading_proxy") or etf


def check_pairs(
    etfs: list[str],
    floor: float = DEFAULT_FLOOR,
    years: int = DEFAULT_YEARS,
    sample: int = DEFAULT_SAMPLE,
    end: str | None = None,
) -> list[PairVerdict]:
    """Fetch once for every symbol involved, then score each pair.

    ``end`` is passed through to yfinance, whose ``end`` is EXCLUSIVE — the
    caller supplies the day AFTER the last bar wanted.
    """
    import yfinance as yf

    panels = {e: latest_constituents(e, sample) for e in etfs}
    symbols = sorted(
        {traded_symbol(e) for e in etfs}
        | {n for names in panels.values() for n in names}
    )
    if not symbols:
        return []

    end_ts = pd.Timestamp(end) if end else pd.Timestamp.utcnow().normalize()
    start_ts = end_ts - pd.DateOffset(years=years)
    raw = yf.download(
        symbols,
        start=start_ts.strftime("%Y-%m-%d"),
        end=end_ts.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    closes = raw["Close"] if "Close" in raw else pd.DataFrame()
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(symbols[0])
    if closes.empty:
        return [
            PairVerdict(e, traded_symbol(e), float("nan"), 0, 0, SKIP, "no price data")
            for e in etfs
        ]

    returns = np.log(closes / closes.shift(1))

    verdicts: list[PairVerdict] = []
    for etf in etfs:
        traded = traded_symbol(etf)
        names = [n for n in panels[etf] if n != traded]
        if traded not in returns.columns:
            verdicts.append(
                PairVerdict(etf, traded, float("nan"), 0, 0, SKIP,
                            f"no price series for {traded}")
            )
            continue
        basket, n_names = basket_returns(returns, names)
        if basket.empty:
            verdicts.append(
                PairVerdict(etf, traded, float("nan"), 0, 0, SKIP,
                            "no constituent prices resolved")
            )
            continue
        corr, n_obs = pair_correlation(returns[traded], basket)
        status, note = classify(corr, n_names, n_obs, floor)
        verdicts.append(
            PairVerdict(etf, traded, corr, n_names, n_obs, status, note)
        )
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--etfs", nargs="*", default=None,
                        help="registry keys to check (default: sleeves A and D)")
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--end", default=None,
                        help="EXCLUSIVE end date YYYY-MM-DD (default: today)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    etfs = args.etfs or list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS)
    print(
        f"Checking {len(etfs)} signal/instrument pairs over {args.years}y, "
        f"floor {args.floor:.2f}, top-{args.sample} constituents ...\n"
    )
    verdicts = check_pairs(etfs, args.floor, args.years, args.sample, args.end)

    print(f"{'PANEL':<8} {'TRADED':<11} {'CORR':>7} {'NAMES':>6} {'OBS':>6}  STATUS")
    print("-" * 60)
    for v in sorted(verdicts, key=lambda x: (x.status != FAIL, x.correlation)):
        corr = f"{v.correlation:7.3f}" if np.isfinite(v.correlation) else "      -"
        print(
            f"{v.etf:<8} {v.traded:<11} {corr} {v.n_names:>6} {v.n_obs:>6}  {v.status}"
        )
        if v.note and (args.verbose or v.status == FAIL):
            print(f"         {v.note}")

    failures = [v for v in verdicts if v.status == FAIL]
    skips = [v for v in verdicts if v.status == SKIP]
    print(
        f"\n{len(verdicts) - len(failures) - len(skips)} pass, "
        f"{len(failures)} FAIL, {len(skips)} skip"
    )
    if failures:
        print(
            "\n[PAIR-INTEGRITY] A priced instrument does not track its own "
            "constituents. Either the registry's yfinance_trading_proxy names "
            "the wrong fund, or the constituent source does. Do not trust any "
            "signal or performance figure for the affected member until it is "
            "resolved."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
