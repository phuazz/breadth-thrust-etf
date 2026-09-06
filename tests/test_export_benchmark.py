"""The benchmark the email and the PDF compare against must reach CI.

The first automatic-era factsheet (2026-09-06) went out with "—" under every
KPI on the PDF's first page and no SPY line on the email tiles: both builders
read SPY from the sleeve B engine cache, which is gitignored and absent on the
runner that sends. These pin the committed export that closes the gap, the
loader's order (cache first, export second, then nothing), and that both
builders and the refresh use it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_benchmark as xb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _cache(tmp_path, dates, closes):
    p = tmp_path / "asset_class_prices_cache.parquet"
    pd.DataFrame({"SPY": closes, "TLT": [1.0] * len(dates)},
                 index=pd.to_datetime(dates)).to_parquet(p)
    return p


DATES = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def test_export_writes_the_series_with_its_basis(tmp_path):
    cache = _cache(tmp_path, DATES, [100.0, 101.0, 102.0, 103.0, 102.0])
    out = tmp_path / "benchmark_spy.json"
    assert xb.export(cache, out, now_utc=datetime(2026, 9, 6, 15, tzinfo=timezone.utc)) == out
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert blob["ticker"] == "SPY" and "adjusted close" in blob["basis"]
    assert blob["first"] == "2026-08-31" and blob["last"] == "2026-09-04"
    assert blob["dates"] == DATES and blob["closes"] == [100.0, 101.0, 102.0, 103.0, 102.0]
    assert blob["written_at_utc"] == "2026-09-06T15:00:00+00:00"


def test_loader_prefers_the_cache_and_falls_back_to_the_export(tmp_path):
    cache = _cache(tmp_path, DATES, [100.0, 101.0, 102.0, 103.0, 102.0])
    out = tmp_path / "benchmark_spy.json"
    xb.export(cache, out)
    s = xb.load_spy_series(cache, out)
    assert float(s.iloc[-1]) == 102.0 and len(s) == 5
    cache.unlink()                                       # the CI runner
    s2 = xb.load_spy_series(cache, out)
    assert s2 is not None and s2.equals(s), "the export must reproduce the cache exactly"
    assert xb.load_spy_series(cache, tmp_path / "absent.json") is None


def test_export_refuses_to_shrink_a_longer_series(tmp_path, capsys):
    long = _cache(tmp_path, DATES, [100.0, 101.0, 102.0, 103.0, 102.0])
    out = tmp_path / "benchmark_spy.json"
    xb.export(long, out)
    (tmp_path / "s").mkdir()
    short = _cache(tmp_path / "s", DATES[:3], [100.0, 101.0, 102.0])
    assert xb.export(short, out) is None
    assert "REFUSED" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["last"] == "2026-09-04"


def test_missing_or_unreadable_inputs_give_none_not_a_guess(tmp_path):
    assert xb.spy_from_cache(tmp_path / "none.parquet") is None
    bad = tmp_path / "benchmark_spy.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert xb.spy_from_json(bad) is None
    assert xb.export(tmp_path / "none.parquet", tmp_path / "x.json") is None


def test_nan_closes_are_dropped_and_dates_sorted(tmp_path):
    cache = tmp_path / "asset_class_prices_cache.parquet"
    pd.DataFrame({"SPY": [102.0, np.nan, 100.0]},
                 index=pd.to_datetime(["2026-09-04", "2026-09-03", "2026-09-02"])).to_parquet(cache)
    out = tmp_path / "benchmark_spy.json"
    xb.export(cache, out)
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert blob["dates"] == ["2026-09-02", "2026-09-04"] and blob["closes"] == [100.0, 102.0]


def test_both_builders_and_the_refresh_use_the_shared_loader():
    email = (ROOT / "scripts" / "build_email_body.py").read_text(encoding="utf-8")
    pdf = (ROOT / "scripts" / "build_factsheet.py").read_text(encoding="utf-8")
    assert "from export_benchmark import load_spy_series" in email
    assert "from export_benchmark import load_spy_series" in pdf
    assert 'spy_cache = DATA_DIR / "asset_class_prices_cache.parquet"' not in pdf, \
        "the PDF must not read the gitignored cache on its own"
    refresh = (ROOT / "scripts" / "refresh_all.py").read_text(encoding="utf-8")
    assert "scripts/export_benchmark.py" in refresh
    assert refresh.index("scripts/export_benchmark.py") < refresh.index("scripts/pipeline.py")
