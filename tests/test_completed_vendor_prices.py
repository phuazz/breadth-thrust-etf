"""Regression cases: UTC crypto closure, invalid vendor prices and source isolation.
Python datetime months are 1-indexed.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import completed_prices as cp
import vendor_tail as vt
import compute_breadth as cb
import repair_price_gaps as repair

@pytest.mark.parametrize('clock,bound', [
 ('2026-10-02T20:01:00Z','2026-10-01'),
 ('2026-10-03T00:00:00Z','2026-10-02'),
 ('2026-10-01T00:00:00Z','2026-09-30'),
 ('2027-01-01T00:00:00Z','2026-12-31')])
def test_crypto_completion(clock,bound):
 assert str(cp.crypto_completed_through(clock).date()) == bound


def test_crypto_mask_does_not_shift_or_modify_history():
 f=pd.DataFrame({'BTC-USD':[100.,101.],'SHY':[50.,51.]},index=pd.to_datetime(['2026-10-01','2026-10-02']))
 out=cp.mask_uncompleted_crypto(f,['BTC-USD'],'2026-10-02T22:00:00Z')
 assert np.isnan(out.iloc[-1,0])
 pd.testing.assert_series_equal(out['SHY'],f['SHY'])
 assert out.iloc[0,0]==100 and f.iloc[-1,0]==101


def test_legacy_and_prefetch_cache_attestation(tmp_path):
 p=tmp_path/'cache.parquet'; side=p.with_suffix('.source.json')
 side.write_text(json.dumps({'written_at_utc':'2026-10-03T00:01:00Z'}))
 assert not cp.cache_crypto_verified(p,['BTC-USD'],'2026-10-02')
 side.write_text(json.dumps({'completed_crypto_through':{'BTC-USD':'2026-10-01'}}))
 assert not cp.cache_crypto_verified(p,['BTC-USD'],'2026-10-02')
 side.write_text(json.dumps({'completed_crypto_through':{'BTC-USD':'2026-10-02'}}))
 assert cp.cache_crypto_verified(p,['BTC-USD'],'2026-10-02')

@pytest.mark.parametrize('bad',[float('inf'),-1.,0.])
def test_invalid_tail_is_not_a_served_close(bad):
 idx=pd.to_datetime(['2026-10-01','2026-10-02'])
 f=pd.DataFrame({'BTC-USD':[100.,np.nan]},index=idx)
 out,rec=vt.heal_hollow_tail(f,['BTC-USD'],through=idx[-1].date(),fetch_single=lambda n:pd.Series([100.,bad],index=idx))
 assert pd.isna(out.iloc[-1,0])
 assert not cb._has_close(pd.Series([bad],index=idx[-1:]),idx[-1])

@pytest.mark.parametrize('values',[(float('inf'),100.,101.),(100.,100.,float('inf')),(100.,float('nan'),101.)])
def test_repair_refuses_nonfinite(values):
 with pytest.raises(repair.RepairError):repair.splice_value(*values)

@pytest.mark.parametrize('bad',[float('inf'),0.,-1.])
def test_bad_cached_price_cannot_certify_session(bad):
 f=pd.DataFrame({'BTC-USD':[bad]},index=pd.to_datetime(['2026-10-02']))
 assert not vt.has_required_session(f,['BTC-USD'],'2026-10-02')


def test_optional_secondary_failure_cannot_discard_primary(tmp_path,monkeypatch):
 idx=pd.bdate_range('2026-09-28','2026-10-02')
 f=pd.DataFrame({'BTC-USD':[100.,101.,102.,103.,np.nan]},index=idx)
 f.to_parquet(tmp_path/'thematic_prices_cache.parquet')
 monkeypatch.setattr(repair,'DATA_DIR',tmp_path)
 monkeypatch.setattr(repair,'fetch_primary',lambda *a:pd.Series([103.,104.],index=idx[-2:]))
 def secondary(*a): raise AssertionError('must not query optional source')
 monkeypatch.setattr(repair,'fetch_secondary',secondary)
 before=(tmp_path/'thematic_prices_cache.parquet').read_bytes()
 result=repair.repair_cache('thematic','BTC-USD',sessions=idx)
 assert result[0]['source']=='primary:yfinance' and result[0]['value']==104.
 assert (tmp_path/'thematic_prices_cache.parquet').read_bytes()==before


def test_transport_failures_are_reported_not_called_unserved(tmp_path,monkeypatch):
 idx=pd.bdate_range('2026-09-28','2026-10-02')
 pd.DataFrame({'BTC-USD':[100.,101.,102.,103.,np.nan]},index=idx).to_parquet(tmp_path/'thematic_prices_cache.parquet')
 monkeypatch.setattr(repair,'DATA_DIR',tmp_path)
 def failed(*a):raise TimeoutError('synthetic transport outage')
 monkeypatch.setattr(repair,'fetch_primary',failed)
 monkeypatch.setattr(repair,'fetch_secondary',failed)
 result=repair.repair_cache('thematic','BTC-USD',sessions=idx)
 assert result[0]['source_errors']=={'primary':'TimeoutError','secondary':'TimeoutError'}
 assert 'value' not in result[0]
