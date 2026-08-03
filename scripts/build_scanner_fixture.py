"""Build the frozen price fixture the scanner's indicator tests pin against.

Run once; commit the parquet. Re-running with the same arguments must
reproduce the same file — that is the point. The fixture exists so that
``tests/test_scanner_indicators.py`` can assert exact indicator values on
real multi-market data without a network call, and so that any future
refactor which shifts a number fails loudly instead of quietly.

Three tickers, chosen because the spec's acceptance checklist (§9.1)
names them and because they span the three trading calendars the scanner
has to survive:

    SOXX        NYSE Arca, USD
    EXV1.DE     Xetra, EUR
    159801.SZ   Shenzhen, CNY

Prices are stored RAW — no FX conversion. The indicator library operates
on whatever series it is handed, so the fixture tests indicators; FX and
calendar handling are tested separately against the conversion code that
owns them.

The window end is pinned rather than derived from today's date, so the
fixture is stable and the file does not churn on every run. yfinance's
``end`` is EXCLUSIVE, so END_DATE is the day AFTER the last bar wanted —
the fencepost that shipped a factsheet missing a rebalance on 2026-07-17.

Usage:
    python scripts/build_scanner_fixture.py
    python scripts/build_scanner_fixture.py --verify   # rebuild and compare
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "scanner_prices.parquet"

TICKERS = ("SOXX", "EXV1.DE", "159801.SZ")
START_DATE = "2022-06-01"
END_DATE = "2026-08-01"      # exclusive — last bar wanted is 2026-07-31
FIELDS = ("Open", "High", "Low", "Close", "Volume")


def fetch() -> pd.DataFrame:
    """Long-format OHLCV for the fixture tickers, adjusted, raw currency."""
    import yfinance as yf

    raw = yf.download(
        list(TICKERS),
        start=START_DATE,
        end=END_DATE,
        auto_adjust=True,
        progress=False,
        group_by="column",
    )
    frames = []
    for ticker in TICKERS:
        cols = {}
        for field in FIELDS:
            try:
                cols[field.lower()] = raw[(field, ticker)]
            except KeyError as exc:  # pragma: no cover — surfaces a bad fetch
                raise RuntimeError(f"{ticker}: missing {field} column") from exc
        frame = pd.DataFrame(cols).dropna(how="all")
        frame.insert(0, "ticker", ticker)
        frames.append(frame)

    out = pd.concat(frames).reset_index()
    out = out.rename(columns={out.columns[0]: "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    return out.sort_values(["ticker", "date"]).reset_index(drop=True)


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, pd.DataFrame]:
    """Fixture as {ticker: OHLCV frame indexed by date}. Used by the tests."""
    long = pd.read_parquet(path)
    out: dict[str, pd.DataFrame] = {}
    for ticker, grp in long.groupby("ticker"):
        frame = grp.drop(columns=["ticker"]).set_index("date").sort_index()
        out[str(ticker)] = frame
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-fetch and report differences against the committed fixture",
    )
    args = parser.parse_args(argv)

    fresh = fetch()
    for ticker, grp in fresh.groupby("ticker"):
        print(
            f"  {ticker:<11} {len(grp):>5} bars  "
            f"{grp['date'].min().date()} -> {grp['date'].max().date()}"
        )

    if args.verify:
        if not FIXTURE_PATH.exists():
            print("no committed fixture to verify against")
            return 1
        committed = pd.read_parquet(FIXTURE_PATH)
        same = fresh.equals(committed)
        print(f"\nidentical to committed fixture: {same}")
        if not same:
            print(
                f"  committed rows {len(committed)}, fresh rows {len(fresh)} — "
                f"a vendor restatement or an adjustment change; review before "
                f"overwriting, because the pinned test values move with it"
            )
        return 0 if same else 1

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh.to_parquet(FIXTURE_PATH, index=False)
    print(f"\nwritten: {FIXTURE_PATH.relative_to(ROOT)} ({len(fresh)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
