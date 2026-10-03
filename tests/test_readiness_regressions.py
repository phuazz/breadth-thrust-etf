"""Readiness failures must not become synthetic fresh instructions.

Python datetime months are 1-indexed.
"""
from datetime import datetime, timezone
import json
import pandas as pd
import pytest
from scripts import live_targets as lt
from scripts.check_factsheet_gate import build_gate_report
from scripts.scheduled_refresh import refresh_success_notice


@pytest.mark.parametrize("signal", [pd.DataFrame(columns=['X']),
    pd.DataFrame({'X': [float('nan')]}), pd.DataFrame({'X': pd.Series(dtype=float)})])
def test_empty_non_date_panels_hold_without_calling_weights(signal):
    def forbidden(_):
        raise AssertionError('empty input reached the ranker')
    result = lt._rank(signal, forbidden, 'NYSE', datetime(2026, 10, 3, tzinfo=timezone.utc), 'A')
    assert result['status'] == 'HOLD'
    assert result['decision_session'] is None and result['weights'] == {}
    assert result['last_completed_session'] == '2026-10-02'


def test_nonempty_non_date_index_is_rejected():
    with pytest.raises(ValueError, match='DatetimeIndex'):
        lt._rank(pd.DataFrame({'X': [0.2]}), lambda _: None, 'NYSE',
                 datetime(2026, 10, 3, tzinfo=timezone.utc), 'A')


@pytest.mark.parametrize('size', [5, 12, 14, 25])
@pytest.mark.parametrize('partial', [False, True])
def test_complete_rows_ready_and_partial_rows_remain_held(size, partial):
    panel = pd.DataFrame([[0.1 + i / 100 for i in range(size)]],
                         columns=[f'X{i}' for i in range(size)],
                         index=pd.to_datetime(['2026-10-02']))
    if partial:
        panel.iloc[0, -1] = float('nan')
    result = lt._rank(panel, lambda row: row / row.sum(), 'NYSE',
                      datetime(2026, 10, 3, tzinfo=timezone.utc), 'A')
    assert result['decision_session'] == '2026-10-02'
    assert result['status'] == ('HOLD' if partial else 'READY')
    if partial:
        assert result['weights'] == {} and result['missing_signal_inputs'] == [f'X{size - 1}']
    else:
        assert sum(result['weights'].values()) == pytest.approx(1)


def test_cache_free_ci_preserves_exact_instruction(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lt, 'DATA_DIR', tmp_path)
    target = tmp_path / 'live_targets.json'
    original = b'{"computed_at_utc":"old","targets_final":true,"lines":[{"held":0.2}]}\n'
    target.write_bytes(original)
    monkeypatch.setattr(lt, 'build', lambda: pytest.fail('cache-free CI must not build or download'))
    assert lt.main(['--json', str(target), '--preserve-on-missing-panels']) == 0
    assert target.read_bytes() == original
    assert 'current readiness is not established' in capsys.readouterr().out


def test_cache_free_ci_cannot_claim_to_preserve_a_missing_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, 'DATA_DIR', tmp_path)
    with pytest.raises(FileNotFoundError, match='no existing target'):
        lt.main(['--json', str(tmp_path / 'absent.json'), '--preserve-on-missing-panels'])


@pytest.mark.parametrize('now,end,anchor', [
    (datetime(2026, 10, 1, 1, 42, tzinfo=timezone.utc), '2026-09-30', '2026-09-25'),
    (datetime(2027, 1, 2, 1, 42, tzinfo=timezone.utc), '2026-12-31', '2026-12-31'),
])
def test_published_anchor_does_not_get_unreleased_resend_advice(tmp_path, now, end, anchor):
    panel, marker, release = [tmp_path / n for n in ['panel.json', 'marker.json', 'release.json']]
    panel.write_text(json.dumps({'end_date': end}))
    marker.write_text(json.dumps({'anchor': anchor}))
    release.write_text(json.dumps({'approved_anchor': '2026-09-04'}))
    report = build_gate_report('publish', now, panel, marker, release_path=release)
    assert report['publish'] is False
    assert 'already published' in report['summary']
    assert 'dispatch' not in report['detail'] and 'NOT released' not in report['detail']


@pytest.mark.parametrize('entry,expected', [
    ({'restatements': {'r1': {'confirmed_at': '2026-09-27T14:12:03Z'}}}, 'restatement delivery confirmed'),
    ({'preview': 'sealed-id'}, 'consolidated delivery is not confirmed'),
    ({}, 'No confirmed component delivery'),
])
def test_postfill_notice_uses_delivery_evidence_without_promising_email(tmp_path, entry, expected):
    from datetime import date
    path = tmp_path / 'delivery.json'
    path.write_text(json.dumps({'anchors': {'2026-09-25': entry}}))
    before = path.read_bytes()
    subject, body = refresh_success_notice('pushed', 'post-fill', {'detail': 'publish=false'},
                                          date(2026, 9, 25), path)
    assert 'valuation' in subject and expected in body
    assert 'does not release or resend' in body and 'factsheet publishing' not in subject
    assert path.read_bytes() == before


def test_corrupt_delivery_does_not_invent_confirmation(tmp_path):
    from datetime import date
    path = tmp_path / 'delivery.json'
    path.write_text('[]')
    _, body = refresh_success_notice('pushed', 'post-fill', {'detail': 'publish=false'},
                                    date(2026, 9, 25), path)
    assert 'state is unavailable' in body
