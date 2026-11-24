"""Apply reviewed native Xetra observations to the consumer's adjusted series.

Yahoo evidence is settled and saved before this step. A genuine incumbent
observation always wins. A change in adjustment basis requires fresh evidence.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from observed_price_cache import PROVENANCE_ATTR

XETRA_ISINS = {
    "EXV1.DE": "DE000A0F5UJ7",
    "EXH1.DE": "DE000A0H08M3",
    "EXH9.DE": "DE000A0Q4R02",
    "EXV3.DE": "DE000A0H08Q4",
    "EXH4.DE": "DE000A0H08J9",
}


def apply_recoveries(frame, symbol, data_dir):
    directory = Path(data_dir) / "xetra_price_recoveries"
    if symbol not in XETRA_ISINS or frame.empty or not directory.is_dir():
        return frame
    provenance = frame.attrs.get(PROVENANCE_ATTR, {})
    # Batch data omits currency. Capture independently binds this symbol and
    # its preceding OHLC to the EUR listing. Reject contradictory metadata.
    if provenance.get("currency") not in (None, "EUR"):
        return frame
    if (provenance.get("symbol"), provenance.get("venue"),
            provenance.get("basis")) != (symbol, "XETR", "auto_adjust=True"):
        return frame
    result = frame
    fields = list(frame.columns)
    if set(fields) not in ({"Close"}, {"Open", "High", "Low", "Close"}):
        return frame
    for path in sorted(directory.glob("*.json")):
        day = pd.Timestamp(path.stem)
        if day < frame.index.min() or day > pd.Timestamp(provenance["through"]):
            continue
        if day in frame.index and frame.loc[day, fields].notna().all():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["venue"], data["currency"], data["session"]) != ("XETR", "EUR", path.stem):
            raise ValueError(f"Invalid Xetra price evidence in {path}")
        if symbol not in data["instruments"] or pd.Timestamp(data["acquired_at"]) > pd.Timestamp(provenance["acquired_at"]):
            continue
        entry = data["instruments"][symbol]
        if entry["isin"] != XETRA_ISINS[symbol]:
            raise ValueError(f"{symbol}: Xetra recovery ISIN mismatch")
        anchor = pd.Timestamp(data["overlap"])
        if anchor >= day:
            raise ValueError(f"{symbol}: invalid overlap for {path.stem}")
        if anchor not in frame.index:
            continue
        raw_anchor = entry["bars"][data["overlap"]]
        # Yahoo stores unadjusted exchange prices as binary32. Compare exact
        # values in that representation before accepting the native EUR bar.
        expected = [float(np.float32(raw_anchor[f])) for f in fields]
        if not np.array_equal(frame.loc[anchor, fields].to_numpy(), expected):
            raise ValueError(f"{symbol} {path.stem}: recovered bar has a different adjustment basis")
        bar = entry["bars"][path.stem]
        opening, high, low, close = (bar[f] for f in ("Open", "High", "Low", "Close"))
        if not (0 < low <= min(opening, close) <= max(opening, close) <= high and np.isfinite(high)
                and close == entry["previous_close"] and bar["Volume"] > 0):
            raise ValueError(f"{symbol} {path.stem}: invalid corroborated Xetra bar")
        values = pd.Series({f: float(np.float32(bar[f])) for f in fields})
        if day in frame.index:
            known = frame.loc[day, fields].dropna()
            if not np.array_equal(known.to_numpy(), values[known.index].to_numpy()):
                raise ValueError(f"{symbol} {path.stem}: partial vendor bar conflicts with recovery")
        if result is frame:
            result = frame.copy()
            result.attrs.pop(PROVENANCE_ATTR)
        result.loc[day, fields] = values[fields]
        result.attrs.setdefault("xetra_price_recoveries", {})[path.stem] = entry["ariva_url"]
        print(f"  {symbol}: using reviewed Xetra {path.stem} exchange prices ({path.name})")
    return result if result is frame else result.sort_index()
