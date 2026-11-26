"""Recover a missing Xetra daily bar from Yahoo's one-session chart.

The multi-day chart can withdraw its last row while the one-session chart
still carries the closing auction. Archive the actual response for later
runners. Python and pandas calendar months are 1-indexed.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from price_source import publish_text
from xetra_recovery import XETRA_ISINS

FIELDS = ("Open", "High", "Low", "Close")


def fetch_chart(symbol):
    response = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "1d", "interval": "1d", "events": "div,splits,capitalGains"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=(4, 8))
    response.raise_for_status()
    return response.json()["chart"]["result"][0]


def verified_bar(chart, symbol, close_time, acquired, previous):
    meta = chart["meta"]
    if (meta["symbol"], meta["fullExchangeName"], meta["currency"],
            meta["dataGranularity"], meta["exchangeTimezoneName"]) != (
            symbol, "XETRA", "EUR", "1d", "Europe/Berlin"):
        raise ValueError("listing, currency or interval mismatch")
    stamp = pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC")
    if (chart["timestamp"] != [meta["regularMarketTime"]] or
            stamp.tz_convert("Europe/Berlin").date() != close_time.tz_convert("Europe/Berlin").date()
            or not close_time <= stamp <= acquired):
        raise ValueError("daily observation is outside its completed session")
    if chart.get("events") or float(np.float32(meta["chartPreviousClose"])) != previous:
        raise ValueError("corporate action or changed previous-session basis")
    quote, = chart["indicators"]["quote"]
    prices = {}
    for field in FIELDS:
        value, = quote[field.lower()]
        prices[field] = float(value)
    adj, = chart["indicators"]["adjclose"]
    adjusted, = adj["adjclose"]
    volume, = quote["volume"]
    opening, high, low, close = prices.values()
    if not (all(np.isfinite(list(prices.values()))) and
            0 < low <= min(opening, close) <= max(opening, close) <= high and
            close == adjusted == float(np.float32(meta["regularMarketPrice"])) and
            np.isfinite(volume) and volume > 0):
        raise ValueError("invalid OHLC, adjustment, closing quote or volume")
    return prices


def recover_daily(out, missing, schedule, cap, now, symbol, directory, lineage, record):
    """Called inside the native-symbol lock, before certifying the settled frame."""
    if symbol not in XETRA_ISINS or not set(out.columns) <= set(FIELDS):
        return
    if out.attrs.get("currency") not in (None, "EUR"):
        return
    for day in missing:
        pos = schedule.index.get_loc(day)
        if pos == 0:
            continue
        previous = out["Close"].get(schedule.index[pos - 1])
        if previous is None or not np.isfinite(previous) or previous <= 0:
            continue
        path = Path(directory) / "yahoo_xetra_daily" / str(day.date()) / (symbol + ".json")
        close_time = schedule.iloc[pos].market_close
        stored = path.is_file()
        if not stored and (day != cap or now - close_time > pd.Timedelta(days=4)):
            continue
        try:
            if stored:
                evidence = json.loads(path.read_text(encoding="utf-8"))
            else:
                evidence = {"acquired_at": now.isoformat(), "chart": fetch_chart(symbol)}
            acquired = pd.Timestamp(evidence["acquired_at"])
            if acquired.tz is None or acquired > now:
                raise ValueError("capture time unavailable at this valuation")
            prices = verified_bar(evidence["chart"], symbol, close_time, acquired, previous)
            values = pd.Series(prices)[out.columns]
            if day in out.index:
                known = out.loc[day].dropna()
                if not np.array_equal(known.values, values[known.index].values):
                    raise ValueError("partial history conflicts with the daily observation")
        except (KeyError, IndexError, TypeError, ValueError, OverflowError, requests.RequestException) as exc:
            record["refusals"].append(f"one-session chart {day.date()}: {exc}")
            continue
        if not stored:
            path.parent.mkdir(parents=True, exist_ok=True)
            publish_text(path, json.dumps(evidence, indent=2, allow_nan=False) + "\n")
        out.loc[day] = values
        lineage[str(day.date())] = {"method": "yahoo-one-session", "acquired_at": acquired.isoformat()}
        record["retried_dates"].append(str(day.date()))
        print(f"  {symbol}: verified Yahoo one-session daily bar for {day.date()}")
