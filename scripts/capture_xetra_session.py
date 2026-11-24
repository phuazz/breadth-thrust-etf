"""Capture a completed Xetra session for a reviewed gap repair.

Run during the next Xetra session, when Yahoo explicitly identifies the
previous close. ARIVA supplies dated EUR OHLC and volume, checked against
FT history and the incumbent Yahoo price representation. Running the
command twice cannot overwrite an existing capture. Review the file in git.
"""
import argparse
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from native_sessions import effective_calendar
from xetra_recovery import XETRA_ISINS

# Python dates use 1-indexed months.
INSTRUMENTS = {
    "EXV1.DE": "banks-ucits-etf-de",
    "EXH1.DE": "oil-gas-ucits-etf-de",
    "EXH9.DE": "utilities-ucits-etf-de",
    "EXV3.DE": "technology-ucits-etf-de-eur-dis",
    "EXH4.DE": "ind-goods-serv-ucits-etf-de",
}
FIELDS = ("Open", "High", "Low", "Close")


def page(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def capture(symbol, anchor, target, next_day):
    isin, slug = XETRA_ISINS[symbol], INSTRUMENTS[symbol]
    ariva = f"https://www.ariva.de/etf/ishares-stoxx-europe-600-{slug}/kurse/historische-kurse"
    ft = f"https://markets.ft.com/data/etfs/tearsheet/historical?s={symbol.split('.')[0]}:GER:EUR"
    soup = page(ariva)
    if isin not in soup.title.get_text():
        raise ValueError(f"{symbol}: ARIVA ISIN mismatch")
    heading = next(h for h in soup.select("h2,h3") if h.get_text(strip=True) == "Historische Kurse (Xetra)")
    table = heading.find_next("table")
    if table.find("tr").get_text("|", strip=True).split("|")[:5] != ["Datum", "Erster", "Hoch", "Tief", "Schluss"]:
        raise ValueError(f"{symbol}: unknown OHLC columns")
    ft_page = page(ft)
    if f"{symbol.split('.')[0]}:GER:EUR" not in ft_page.title.get_text():
        raise ValueError(f"{symbol}: FT listing mismatch")
    ticker = yf.Ticker(symbol)
    daily = ticker.history(period="1mo", auto_adjust=False, actions=True)
    meta = ticker.history_metadata
    stamp = pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC").tz_convert("Europe/Berlin")
    if (meta["symbol"], meta["fullExchangeName"], meta["currency"], stamp.date()) != (
            symbol, "XETRA", "EUR", next_day):
        raise ValueError(f"{symbol}: previousClose cannot be dated to {target}")
    if daily.index[-1].date() != next_day or any(daily.loc[str(anchor):, k].ne(0).any()
                                                 for k in ("Dividends", "Stock Splits", "Capital Gains") if k in daily):
        raise ValueError(f"{symbol}: missing session or corporate action across the join")
    if not daily.loc[str(anchor):, "Close"].equals(daily.loc[str(anchor):, "Adj Close"]):
        raise ValueError(f"{symbol}: raw and adjusted tail differ")
    records = {}
    for day in (anchor, target):
        row = next(r for r in table.select("tr") if r.td and r.td.get_text(strip=True) == day.strftime("%d.%m.%y"))
        cells = [td.get_text(strip=True) for td in row.select("td")]
        if any("€" not in x for x in cells[1:5]):
            raise ValueError(f"{symbol}: non-EUR price")
        prices = [Decimal(x.replace("€", "").strip().replace(".", "").replace(",", ".")) for x in cells[1:5]]
        volume = int(cells[6].replace(".", ""))
        ft_date = f"{day:%A, %B} {day.day}, {day.year}"
        ft_row = next(r for r in ft_page.select("table tr") if r.td and
                      r.td.get_text(" ", strip=True).startswith(ft_date))
        ft_cells = ft_row.select("td")
        ft_prices = [Decimal(td.get_text(strip=True).replace(",", "")) for td in ft_cells[1:5]]
        ft_volume = int(ft_cells[5].find("span").get_text(strip=True).replace(",", ""))
        # FT displays two decimals. Accept either rounding direction on a half cent.
        if volume <= 0 or volume != ft_volume or any(abs(x-y) > Decimal(".005") for x,y in zip(prices, ft_prices)):
            raise ValueError(f"{symbol} {day}: FT OHLC or volume disagrees")
        if day == anchor:
            for field, value in zip(FIELDS, prices):
                if float(np.float32(value)) != daily.loc[str(day), field]:
                    raise ValueError(f"{symbol}: adjusted overlap mismatch in {field}")
        elif prices[-1] != Decimal(str(meta["previousClose"])):
            raise ValueError(f"{symbol}: Yahoo previous close disagrees")
        records[str(day)] = dict(zip(FIELDS, map(float, prices)), Volume=volume)
    return dict(isin=isin, ariva_url=ariva, ft_url=ft, previous_close=meta["previousClose"],
                reference_quote_time=stamp.isoformat(), bars=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=date.fromisoformat)
    args = parser.parse_args()
    today = datetime.now(timezone.utc)
    target = pd.Timestamp(args.session)
    schedule = effective_calendar("XETR").schedule(target-pd.Timedelta(days=14), target+pd.Timedelta(days=14))
    position = schedule.index.get_loc(target)
    if today <= schedule.iloc[position].market_close:
        raise ValueError("Xetra session has not closed")
    anchor, next_day = schedule.index[position-1].date(), schedule.index[position+1].date()
    results = {s: capture(s, anchor, args.session, next_day) for s in XETRA_ISINS}
    payload = dict(venue="XETR", currency="EUR", session=str(args.session), overlap=str(anchor),
                   acquired_at=datetime.now(timezone.utc).isoformat(), instruments=results)
    dest = Path(__file__).resolve().parents[1] / "data" / "xetra_price_recoveries" / f"{args.session}.json"
    dest.parent.mkdir(exist_ok=True)
    with dest.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2) + "\n")
    print(dest)


if __name__ == "__main__":
    main()
