"""Offline regressions for the observed 6 October failure shapes."""
import sys
from pathlib import Path
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from simple_attribution import holding_contributions
from build_panel_series import check_price_frame_freshness, StalePriceCacheError
import compute_breadth as cb
import mixed_market_tail as mt


@pytest.mark.parametrize('gate', [False, True])
@pytest.mark.parametrize('tilt', [False, True])
def test_shared_shy_conserves_nav_and_each_sleeve(gate, tilt):
    scale = .5 if gate else 1
    a, b, c, d, em = .35*scale, (.25 if tilt else .35)*scale, .1*scale, .2*scale, (.1*scale if tilt else 0)
    reserve = 1-scale
    live = {'regime_state': 'RISK_OFF' if gate else 'RISK_ON',
            'eem_tilt_active': tilt,
            'sleeve_extensions': {
                'strategy_a': {'weights': {'SPY': 1}},
                'strategy_b': {'weights': {'DBC': 6/7, 'SHY': 1/7}},
                'strategy_c': {'weights': {'SHY': 1}},
                'strategy_d': {'weights': {'EXV1': 1}}},
            'effective_weights': {'SPY': a, 'DBC': b*6/7, 'SHY': b/7+c+reserve, 'EXV1': d}}
    if tilt:
        live['effective_weights']['EEM'] = em
    result = holding_contributions(live, 'SHY', 'EEM', 'reserve')
    assert result['SHY']['strategy_b'] == pytest.approx(b/7)
    assert result['SHY']['strategy_c'] == pytest.approx(c)
    assert result['SHY'].get('reserve', 0) == pytest.approx(reserve)
    assert sum(sum(p.values()) for p in result.values()) == pytest.approx(1)
    for ticker, weight in live['effective_weights'].items():
        assert sum(result[ticker].values()) == pytest.approx(weight)
    totals = {k: sum(p.get(k, 0) for p in result.values()) for k in
              ('strategy_a', 'strategy_b', 'strategy_c', 'strategy_d', 'tilt', 'reserve')}
    assert list(totals.values()) == pytest.approx([a,b,c,d,em,reserve])
    assert len(result) == len(live['effective_weights'])


def test_shared_non_cash_and_tilt_not_filed_as_reserve():
    live = {'regime_state':'RISK_ON', 'eem_tilt_active':True,
            'sleeve_extensions': {'strategy_b':{'weights':{'EEM':1}},
                                  'strategy_c':{'weights':{'EEM':1}}},
            'effective_weights':{'EEM':.45}}
    parts = holding_contributions(live, 'SHY', 'EEM', 'reserve')['EEM']
    assert parts == pytest.approx({'strategy_b':.25,'strategy_c':.10,'tilt':.10})
    live['effective_weights']['EEM'] = .50
    with pytest.raises(ValueError, match='disagrees'):
        holding_contributions(live, 'SHY', 'EEM', 'reserve')


def test_shared_rounding_and_intentional_underweight():
    live = {'regime_state':'RISK_OFF','eem_tilt_active':False,
            'sleeve_extensions': {'strategy_b':{'weights':{'SHY':.2,'DBC':.3}},
                                  'strategy_c':{'weights':{'SHY':.9999}}},
            'effective_weights':{'SHY':.175*.2+.05+.5,'DBC':.175*.3}}
    parts = holding_contributions(live, 'SHY', 'EEM', 'reserve')['SHY']
    assert parts == pytest.approx({'strategy_b':.035,'strategy_c':.05,'reserve':.5})
    live['sleeve_extensions']['strategy_c']['weights']['SHY'] = 1.02
    with pytest.raises(ValueError, match='exceed'):
        holding_contributions(live, 'SHY', 'EEM', 'reserve')


def mixed_frame():
    columns = [f'{600000+i}.SS' for i in range(8)] + ['0700.HK','SPY']
    frame = pd.DataFrame(10., index=pd.to_datetime(['2026-09-29','2026-09-30']), columns=columns)
    frame.loc[pd.Timestamp('2026-10-05')] = float('nan')
    frame.loc[pd.Timestamp('2026-10-05'), ['0700.HK','SPY']] = [10.1,10.2]
    return frame


def test_mixed_holiday_retains_real_prices_without_breadth_eligibility():
    frame = mixed_frame()
    frozen = frame.copy(deep=True)
    out, evidence = cb.verify_price_tail(frame, list(frame.columns), fetch_single=lambda t: frame[t].dropna())
    pd.testing.assert_frame_equal(frame, frozen)
    pd.testing.assert_frame_equal(out, frozen)
    row = evidence['rows'][0]
    assert row['verdict'] == 'partial'
    assert len(row['calendar_closed']) == 8
    assert set(row['open_market_priced']) == {'0700.HK','SPY'}
    assert pd.Timestamp('2026-10-05') not in cb.priced_sessions(out, list(frame.columns))
    assert out.loc['2026-10-05'].isna().sum() == 8
    check_price_frame_freshness('ICHN', out, date(2026,10,5))


@pytest.mark.parametrize('missing', ['SPY', '0700.HK', '600000.SS'])
def test_fresh_subset_cannot_hide_stale_current_name(missing):
    frame = mixed_frame()
    frame.loc[frame.index >= pd.Timestamp('2026-09-30'), missing] = float('nan')
    with pytest.raises(StalePriceCacheError, match=missing.replace('.', r'\.')):
        check_price_frame_freshness('ICHN', frame, date(2026,10,5))


def test_real_frozen_open_market_cache_still_refused():
    frame = mixed_frame().loc[:'2026-09-30']
    with pytest.raises(StalePriceCacheError, match='SPY'):
        check_price_frame_freshness('ICHN', frame, date(2026,10,5))


def test_calendar_failure_and_unknown_symbol_fail_closed(monkeypatch):
    frame = mixed_frame()
    monkeypatch.setattr(mt, 'venue_sessions', lambda *args: None)
    with pytest.raises(StalePriceCacheError):
        check_price_frame_freshness('ICHN', frame, date(2026,10,5))
    assert not mt.closed_since('1234.UNKNOWN', date(2026,9,30), date(2026,10,5))


@pytest.mark.parametrize('ticker,day,closed', [
    ('600000.SS',date(2026,10,5),True),('000001.SZ',date(2026,10,5),True),
    ('0700.HK',date(2026,10,1),True),('0700.HK',date(2026,10,2),False),
    ('SPY',date(2026,10,1),False),('600000.SS',date(2026,10,8),False),
    ('SPY',date(2027,1,1),True),('SPY',date(2027,1,4),False)])
def test_calendar_october_and_year_boundary(ticker,day,closed):
    assert mt.is_closed(ticker,day) is closed
