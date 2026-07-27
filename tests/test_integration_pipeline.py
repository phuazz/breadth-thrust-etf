"""Synthetic integration coverage for the breadth pipeline.

End-to-end test of compute_breadth.main with a tiny synthetic
constituent file and a stub download_prices. No network, no real
parquet cache, no real iShares CSV — just verifies that the integrated
pipeline produces a well-formed breadth_*.json and that the active
roster dedup (Phase 14) propagates through to the universe count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compute_breadth as cb  # noqa: E402


def test_compute_breadth_main_uses_unique_roster_without_network(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    constituents = {
        "etf": "TEST",
        "start_friday": "2024-01-05",
        "end_friday": "2024-04-30",
        "snapshots": {
            "2024-01-05": {
                "actual_date": "2024-01-05",
                "n_tickers": 3,
                "tickers": ["A", "A", "B"],
            },
            "2024-01-12": {
                "actual_date": "2024-01-12",
                "n_tickers": 2,
                "tickers": ["A", "B"],
            },
        },
    }
    (data_dir / "constituents_test.json").write_text(
        json.dumps(constituents), encoding="utf-8"
    )

    idx = pd.bdate_range("2023-07-01", "2024-05-10")
    prices = pd.DataFrame({
        "A": np.linspace(50.0, 120.0, len(idx)),
        "B": np.linspace(120.0, 80.0, len(idx)),
    }, index=idx)

    def fake_download_prices(tickers, start, end, cache_path, force=False,
                             reuse_cache_dates=False):
        assert tickers == ["A", "B"]
        return prices.loc[pd.Timestamp(start):pd.Timestamp(end), tickers]

    monkeypatch.setattr(cb, "DATA_DIR", data_dir)
    monkeypatch.setattr(cb, "download_prices", fake_download_prices)
    monkeypatch.setattr(sys, "argv", ["compute_breadth.py", "--etf", "TEST"])

    assert cb.main() == 0

    out = json.loads((data_dir / "breadth_test.json").read_text(encoding="utf-8"))
    assert out["etf"] == "TEST"
    assert out["data_quality"]["universe_size"] == 2
    # Dedup at the roster level → every day's denominator is 2, not 3.
    assert set(out["series"]["n_constituents"]) == {2}
    assert set(out["series"]["n_with_price"]) == {2}
