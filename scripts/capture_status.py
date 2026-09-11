"""Provenance for the current roster and the prices captured for it.

This describes availability, without changing registered breadth coverage
floors or substituting today's membership into historical calculations.
Python datetime months are 1-indexed.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd


def roster_fingerprint(constituents: dict) -> str:
    snapshots = constituents.get("snapshots") or {}
    key = max(snapshots) if snapshots else None
    latest = snapshots.get(key) or {}
    encoded = json.dumps({"target": key, "actual_date": latest.get("actual_date"),
                          "tickers": sorted(set(latest.get("tickers") or []))},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def describe_capture(constituents: dict, prices: pd.DataFrame,
                     required_through, panel_through: str, price_source: str) -> dict:
    snapshots = constituents.get("snapshots") or {}
    key = max(snapshots) if snapshots else None
    latest = snapshots.get(key) or {}
    roster = sorted(set(latest.get("tickers") or []))
    required = pd.Timestamp(required_through) if required_through is not None else None
    usable = prices.reindex(columns=roster)
    if required is not None:
        usable = usable.loc[:required]
    ends = {t: (str(usable[t].dropna().index.max().date())
                if usable[t].notna().any() else None) for t in roster}
    absent = [t for t, d in ends.items() if d is None]
    short = [t for t, d in ends.items()
             if d is not None and required is not None and pd.Timestamp(d) < required]
    return {
        "schema_version": 1,
        "roster_fingerprint": roster_fingerprint(constituents),
        "roster_target": key,
        "roster_actual": latest.get("actual_date"),
        "roster_carried": bool(latest.get("carried_forward_from")),
        "roster_endpoint_status": (constituents.get("endpoint_health") or {}).get("status"),
        "roster_staleness": (constituents.get("staleness") or {}).get("status"),
        "requested_price_source": price_source,
        "required_price_session": str(required.date()) if required is not None else None,
        "panel_through": panel_through,
        "panel_current": required is not None and pd.Timestamp(panel_through) == required,
        "n_active": len(roster),
        "n_priced_through_required": len(roster) - len(absent) - len(short),
        "no_price_history": absent,
        "behind_required_session": {t: ends[t] for t in short},
        "note": "Availability of active constituents; breadth coverage rules are unchanged.",
    }
