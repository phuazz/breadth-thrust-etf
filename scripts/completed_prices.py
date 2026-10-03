"""Completed vendor days, independent of the portfolio execution calendar.

Yahoo spot Bitcoin daily bars open at 00:00 UTC and close the next midnight.
Python datetime months are 1-indexed. No prices or date labels are shifted.
"""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd


def crypto_completed_through(now_utc):
    now = pd.Timestamp(now_utc)
    if now.tz is None:
        raise ValueError('crypto completion requires an aware timestamp')
    return (now.tz_convert('UTC').normalize() - pd.Timedelta(days=1)).tz_localize(None)


def mask_uncompleted_crypto(frame, names, now_utc):
    out = frame.copy()
    bound = crypto_completed_through(now_utc)
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert('UTC').tz_localize(None)
    for name in names:
        if name in out.columns:
            out.loc[idx.normalize() > bound, name] = float('nan')
    return out


def cache_crypto_verified(cache_path, names, required_through):
    """Only a recorded pre-fetch completion bound certifies a current cache.

    Legacy caches must refresh once; a write timestamp cannot prove the fetch
    began after midnight. An absent attestation never certifies live prices.
    """
    if not names:
        return True
    try:
        data = json.loads(Path(cache_path).with_suffix('.source.json').read_text())
        bounds = data['completed_crypto_through']
        return all(pd.Timestamp(bounds[n]) >= pd.Timestamp(required_through)
                   for n in names)
    except (OSError, ValueError, TypeError, KeyError):
        return False
