"""Known instrument identities explain gaps without removing them."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from price_gap_context import gap_context


def test_right_is_not_mapped_to_the_ordinary_share():
    rows = gap_context("breadth_exh9", ["2689012D.L", "PNN.L"], {})
    assert rows[0]["category"] == "subscription_right"
    assert rows[0]["isin"] == "GB00BWNPZS82"
    assert rows[1]["category"] == "unresolved_price_gap"


def test_identity_context_does_not_hide_gaps_or_apply_to_other_panels():
    behind = {"6446.TW": "2026-10-12"}
    rows = gap_context("breadth_itwn", ["2418.TW"], behind)
    assert len(rows) == 2
    assert rows[0]["category"] == "delisted_security"
    assert rows[1]["category"] == "unresolved_price_gap"
    assert rows[1]["last_price_date"] == "2026-10-12"
    assert gap_context("breadth_other", ["2418.TW"], {})[0]["category"] == "unresolved_price_gap"
    assert gap_context("breadth_itwn", [], {}) == []
