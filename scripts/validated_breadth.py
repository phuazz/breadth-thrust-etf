"""Common consumer boundary for validated constituent signals.

Raw caches remain intact for recovery and audit. Python months are 1-indexed.
"""
import json
from datetime import date
from pathlib import Path

import pandas as pd


class RosterMismatch(ValueError):
    """The validated signal was built from another roster."""


def validated_end(data_dir: Path, etf: str) -> pd.Timestamp:
    panel = json.loads((data_dir / f"breadth_{etf.lower()}.json").read_text(encoding="utf-8"))
    bound = pd.Timestamp(date.fromisoformat(panel["end_date"]))
    if panel.get("current_capture"):
        from capture_status import roster_fingerprint
        roster = json.loads((data_dir / f"constituents_{etf.lower()}.json").read_text(encoding="utf-8"))
        if panel["current_capture"].get("roster_fingerprint") != roster_fingerprint(roster):
            raise RosterMismatch(f"{etf}: validated breadth does not match the roster")
    return bound


def cap_signal(series: pd.Series, bound: pd.Timestamp) -> pd.Series:
    """Mask, never forward-fill, observations beyond the validated boundary."""
    return series.where(series.index <= bound)
