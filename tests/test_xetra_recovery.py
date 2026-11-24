import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from component_release import europe_identity
from observed_price_cache import PROVENANCE_ATTR
from pipeline import pending_europe_fill
from xetra_recovery import apply_recoveries

DATA = Path(__file__).resolve().parents[1] / "data"
SESSION = "2026-11-23"
ANCHOR = "2026-11-20"


def reference_frame():
    frame = pd.DataFrame({"Close": [float(np.float32(37.26))]}, index=pd.to_datetime([ANCHOR]))
    frame.attrs[PROVENANCE_ATTR] = dict(symbol="EXV1.DE", venue="XETR", currency=None,
        basis="auto_adjust=True", through=SESSION, acquired_at="2026-11-24T18:00:00+00:00")
    return frame


def test_verified_close_separate_from_yahoo_and_repeatable():
    frame = reference_frame()
    repaired = apply_recoveries(frame, "EXV1.DE", DATA)
    assert repaired.loc[SESSION, "Close"] == float(np.float32(38.265))
    assert PROVENANCE_ATTR not in repaired.attrs
    assert SESSION in repaired.attrs["xetra_price_recoveries"]
    assert len(frame) == 1 and PROVENANCE_ATTR in frame.attrs
    frame.loc[pd.Timestamp(SESSION), "Close"] = 38.29
    assert apply_recoveries(frame, "EXV1.DE", DATA) is frame


def test_rejects_changed_basis_and_respects_capture_cutoff():
    frame = reference_frame()
    frame.iloc[0, 0] *= .98
    with pytest.raises(ValueError, match="different adjustment basis"):
        apply_recoveries(frame, "EXV1.DE", DATA)
    frame.attrs[PROVENANCE_ATTR]["through"] = ANCHOR
    assert apply_recoveries(frame, "EXV1.DE", DATA) is frame


@pytest.mark.parametrize("anchor,fill", [("2026-10-30", "2026-11-02"), ("2023-12-29", "2024-01-02")])
def test_pending_instruction_crosses_calendar_boundary_and_clears_on_fill(anchor, fill):
    # Python calendar months are 1-indexed. The venue supplies the completed session.
    sleeve = dict(sleeve="D", venue="XETR", weights={"EXV1": 1.0}, status="READY",
                  decision_session=anchor, decision_session_for_fill=anchor, fill_date=fill)
    release = dict(anchor=anchor, d_ready=True, book={"sleeves": [sleeve], "lines": []})
    release["europe_identity"] = europe_identity(release["book"])
    europe = {"headline": {"latest_rebalance": {"date": anchor}, "headline_equity_dates": [anchor]}}
    fresh = {"strategies": [{"sleeve": "D", "venue_last_session": anchor}]}
    assert pending_europe_fill(europe, fresh, release) is None
    fresh["strategies"][0]["venue_last_session"] = fill
    assert pending_europe_fill(europe, fresh, release)["weights"] == sleeve["weights"]
    europe["headline"]["latest_rebalance"]["date"] = fill
    assert pending_europe_fill(europe, fresh, release) is None
