"""Owner corrections compose with the unchanged automatic D lifecycle.
Python datetime months are 1-indexed. All transport is synthetic.
"""
import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from datetime import datetime, timezone, timedelta
import pytest
import component_release as cr
import preview_supersession as ps
import send_component_factsheet as sender
from component_publication import Snapshot, email_decision, review_window
from test_component_sender import fixture_book, NOW
from test_factsheet_restatement import install_book, restated_book
from test_preview_supersession import TO


def first_correction(root, monkeypatch, policy='suppress_unchanged'):
 old,basis=fixture_book(NOW,d_ready=False,hold=('C',))
 prior=install_book(root/'old',monkeypatch,old,basis)
 current,basis=restated_book()
 current['sleeves'][-1]=copy.deepcopy(old['sleeves'][-1])
 current['lines']=[r for r in current['lines'] if r['sleeve']!='D']+[r for r in old['lines'] if r['sleeve']=='D']
 current['targets_final']=False
 release=install_book(root,monkeypatch,current,basis)
 original={'anchor':prior['anchor'],'preview':prior['identity'],'core':prior['core_identity'],'europe':prior['europe_identity'],'last_confirmed_at':NOW.isoformat()}
 cr.write(root/sender.LEDGER,{'schema':1,'anchors':{prior['anchor']:original}})
 options=dict(expected_preview=prior['identity'],authority='Explicit synthetic owner approval',supersession='correction-1',allowed_holds=['D'],followup_policy=policy,to=TO,now=NOW,committed=False,prior=prior)
 path=ps.prepare(root,**options);candidate=cr.read(path)
 check=dict(now=NOW,committed=False,prior=prior)
 ps.reserve(root,path,candidate['id'],**check)
 ps.send(root,path,candidate['id'],transport=lambda *_:None,env={'RECIPIENT_EMAIL':','.join(TO)},**check)
 return prior,release,original,options


def decision(release,state,now,ready,identity='new-d'):
 anchor=release['anchor']
 return email_decision(now,anchor=anchor,core=Snapshot(release['core_identity'],True,anchor,anchor),europe=Snapshot(identity,ready,anchor,anchor),sent=state)

@pytest.mark.parametrize('policy',['suppress_unchanged','ordinary_unchanged'])
def test_d_followup_keeps_original_deadlines(tmp_path,monkeypatch,policy):
 prior,release,original,_=first_correction(tmp_path,monkeypatch,policy)
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 regular,end=review_window(NOW)
 assert decision(release,state,NOW,True)['action']=='regular'
 assert decision(release,state,end,True)['action']=='regular'
 assert decision(release,state,end+timedelta(seconds=1),True)['action']=='alert'
 expected='wait' if policy=='suppress_unchanged' else 'regular'
 assert decision(release,state,regular,False,release['europe_identity'])['action']==expected
 state['regular']='d_hold'
 assert decision(release,state,end,True)['action']=='d_update'
 assert decision(release,state,end+timedelta(seconds=1),True)['action']=='alert'
 state['d_update']='already-confirmed';state['europe']='new-d'
 assert decision(release,state,end,True)['action']=='wait'
 assert decision(release,state,end,True,'later-d')['action']=='alert'


def test_late_core_then_automatic_d_then_owner_core_correction(tmp_path,monkeypatch):
 prior,first,original,options=first_correction(tmp_path,monkeypatch)
 saved=copy.deepcopy(sender.ledger_at(tmp_path))
 # A changed C target is not automatically mailed, even though still before Sunday.
 second=install_book(tmp_path,monkeypatch,*fixture_book(NOW,d_ready=False))
 assert sender.plan(tmp_path,NOW)[0]['action']=='alert'
 with pytest.raises(ValueError,match='latest delivered'):
  ps.plan(tmp_path,**(options|{'supersession':'correction-2','prior':first}))
 opts=options|{'supersession':'correction-2','expected_previous':first['identity'],'prior':first}
 path=ps.prepare(tmp_path,**opts);c=cr.read(path)
 assert c['decision']['is_late_correction']
 assert 'latest delivered instruction' in c['subject']
 assert 'latest delivered instruction' in c['text']
 assert 'normal automatic follow-up' in c['html']
 check=dict(now=NOW,committed=False,prior=first)
 ps.reserve(tmp_path,path,c['id'],**check)
 ps.send(tmp_path,path,c['id'],transport=lambda *_:None,env={'RECIPIENT_EMAIL':','.join(TO)},**check)
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 assert state['preview']==original['preview'] and 'regular' not in state
 assert state['preview_supersessions']['correction-1']==saved['anchors'][prior['anchor']]['preview_supersessions']['correction-1']
 assert len(state['preview_supersessions'])==2
 # D backfills with exactly the owner-approved core. Its ordinary route is live.
 ready=install_book(tmp_path,monkeypatch,*fixture_book(NOW,d_ready=True))
 assert sender.prepare(tmp_path,NOW,reserve=True)['action']=='regular'
 delivered=[]
 sender.send(tmp_path,NOW,transport=lambda c,e:delivered.append(c),env={})
 assert len(delivered)==1 and sender.plan(tmp_path,NOW)[0]['action']=='wait'
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 assert len(state['delivery_receipts'])==1
 frozen=copy.deepcopy(state)
 # An explicit later correction can follow the automatic D receipt as well.
 install_book(tmp_path,monkeypatch,*restated_book())
 opts=options|{'supersession':'correction-3','allowed_holds':[],
  'expected_previous':ready['identity'],'prior':ready}
 d,_=ps.plan(tmp_path,**opts)
 assert d['original_delivery_state']==frozen
 assert d['prior_release']==ready['identity']
 with pytest.raises(ValueError,match='latest delivered|reconstruct'):
  ps.plan(tmp_path,**(opts|{'expected_previous':first['identity']}))
 # Merely planning cannot mutate any receipt.
 assert sender.ledger_at(tmp_path)['anchors'][prior['anchor']]==frozen


def test_owner_correction_after_checkpoint_is_explicit_not_automatic(tmp_path,monkeypatch):
 prior,first,_,options=first_correction(tmp_path,monkeypatch)
 install_book(tmp_path,monkeypatch,*fixture_book(NOW,d_ready=False))
 _,end=review_window(NOW);late=end+timedelta(hours=1)
 assert sender.plan(tmp_path,late)[0]['action']=='alert'
 d,_=ps.plan(tmp_path,**(options|{'supersession':'late-c','expected_previous':first['identity'],'prior':first,'now':late}))
 assert d['after_review_checkpoint'] and d['authority']
 with pytest.raises(ValueError,match='authority'):
  ps.plan(tmp_path,**(options|{'supersession':'late-c','expected_previous':first['identity'],'prior':first,'now':late,'authority':''}))


def test_automatic_d_uncertainty_blocks_explicit_retry(tmp_path,monkeypatch):
 prior,_,_,_=first_correction(tmp_path,monkeypatch)
 install_book(tmp_path,monkeypatch,*restated_book())
 assert sender.prepare(tmp_path,NOW,reserve=True)['action']=='regular'
 def failed(*_):raise TimeoutError('synthetic SMTP uncertainty')
 with pytest.raises(TimeoutError):sender.send(tmp_path,NOW,transport=failed,env={})
 with pytest.raises(ValueError,match='already attempted'):
  sender.send(tmp_path,NOW,transport=lambda *_:pytest.fail('duplicate SMTP'),env={})
 assert sender.plan(tmp_path,NOW)[0]['action']=='alert'

@pytest.mark.parametrize('fault',[None,'push','receipt'])
def test_automatic_d_attempt_is_durable_before_smtp(tmp_path,monkeypatch,fault):
 import json,subprocess
 prior,_,_,_=first_correction(tmp_path,monkeypatch)
 release=install_book(tmp_path,monkeypatch,*restated_book())
 sender.prepare(tmp_path,NOW,reserve=True)
 remote=copy.deepcopy(sender.ledger_at(tmp_path));events=[]
 monkeypatch.setattr(sender,'verify',lambda *a,**k:release)
 def git(cmd,**kwargs):
  events.append(cmd[1])
  if cmd[1]=='show':return subprocess.CompletedProcess(cmd,0,stdout=json.dumps(remote).encode())
  if cmd[1]=='status':return subprocess.CompletedProcess(cmd,0,stdout='')
  assert sender.ledger_at(tmp_path)['anchors'][prior['anchor']]['pending']['attempted_at']
  if cmd[1]=='push' and fault=='push':raise subprocess.CalledProcessError(1,cmd)
  return subprocess.CompletedProcess(cmd,0)
 monkeypatch.setattr(sender.subprocess,'run',git)
 if fault=='receipt':
  write=sender.write
  def fail_receipt(path,value):
   if path == tmp_path / sender.LEDGER and value['anchors'][prior['anchor']].get('regular'):
    raise OSError('synthetic confirmed-receipt write failure')
   return write(path,value)
  monkeypatch.setattr(sender,'write',fail_receipt)
 def transport(*_):
  events.append('smtp')
  assert events[-4:]==['add','commit','push','smtp']
 if fault:
  with pytest.raises((OSError,subprocess.CalledProcessError)):
   sender.send(tmp_path,NOW,transport=transport,env={},committed=True)
  assert events.count('smtp')==(1 if fault=='receipt' else 0)
  assert sender.ledger_at(tmp_path)['anchors'][prior['anchor']]['pending']['attempted_at']
 else:
  sender.send(tmp_path,NOW,transport=transport,env={},committed=True)
  assert events.count('smtp')==1


def test_d_readiness_regression_is_not_an_unchanged_duplicate(tmp_path,monkeypatch):
 prior,release,_,_=first_correction(tmp_path,monkeypatch)
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 state['regular']='all_ready';state['europe']='new-ready-d'
 assert decision(release,state,NOW,False,'unverified-d')['action']=='alert'
 # An expressly approved later HOLD replaces the effective D instruction.
 state['preview_supersession']['europe']='approved-hold-d'
 state['europe']='approved-hold-d'
 assert decision(release,state,NOW,False,'approved-hold-d')['action']=='wait'


def test_legacy_restatement_and_new_correction_remain_compatible(tmp_path,monkeypatch):
 prior,_,_,options=first_correction(tmp_path,monkeypatch)
 ready=install_book(tmp_path,monkeypatch,*restated_book())
 sender.prepare(tmp_path,NOW,reserve=True)
 sender.send(tmp_path,NOW,transport=lambda *_:None,env={})
 legacy=install_book(tmp_path,monkeypatch,*fixture_book(NOW,d_ready=True))
 original=copy.deepcopy(sender.ledger_at(tmp_path)['anchors'][prior['anchor']]['preview_supersessions'])
 sender.prepare_restatement(tmp_path,NOW,restatement='legacy-reviewed',authority='Synthetic owner approval',reserve=True,prior_release=ready)
 sender.send_restatement(tmp_path,NOW,restatement='legacy-reviewed',authority='Synthetic owner approval',prior_release=ready,transport=lambda *_:None,env={})
 assert sender.plan(tmp_path,NOW)[0]['action']=='wait'
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 assert state['preview_supersessions']==original
 assert state['latest_delivery_release']==legacy['identity']
 install_book(tmp_path,monkeypatch,*restated_book())
 d,_=ps.plan(tmp_path,**(options|{'supersession':'after-legacy','allowed_holds':[],
   'expected_previous':legacy['identity'],'prior':legacy}))
 assert d['prior_release']==legacy['identity']


def test_owner_correction_keeps_distinct_nyse_and_xetra_sessions(tmp_path,monkeypatch):
 # Python months are 1-indexed; calendars derive the actual sessions.
 at=datetime(2026,7,4,6,tzinfo=timezone.utc)
 old,basis=fixture_book(at,d_ready=False,hold=('C',))
 prior=install_book(tmp_path/'old',monkeypatch,old,basis,now=at)
 current,basis=restated_book(at)
 release=install_book(tmp_path,monkeypatch,current,basis,now=at)
 d_sleeve=release['book']['sleeves'][-1]
 assert d_sleeve['decision_session_for_fill'] != release['anchor']
 state={'anchor':prior['anchor'],'preview':prior['identity'],'core':prior['core_identity'],
        'europe':prior['europe_identity'],'last_confirmed_at':at.isoformat()}
 cr.write(tmp_path/sender.LEDGER,{'schema':1,'anchors':{prior['anchor']:state}})
 decision,verified=ps.plan(tmp_path,expected_preview=prior['identity'],authority='Synthetic owner review',
     supersession='venue-holiday-review',allowed_holds=[],followup_policy='suppress_unchanged',
     to=TO,now=at,committed=False,prior=prior)
 assert verified['d_ready'] and not decision['d_hold']

@pytest.mark.parametrize('policy',['suppress_unchanged','ordinary_unchanged'])
def test_owner_delivered_ready_d_does_not_trigger_duplicate_d_update(tmp_path,monkeypatch,policy):
 prior,first,_,options=first_correction(tmp_path,monkeypatch,'ordinary_unchanged')
 regular,_=review_window(NOW)
 assert sender.prepare(tmp_path,regular,reserve=True)['action']=='regular'
 sender.send(tmp_path,regular,transport=lambda *_:None,env={})
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 assert state['regular']=='d_hold' and 'd_update' not in state
 install_book(tmp_path,monkeypatch,*restated_book())
 at=regular+timedelta(minutes=30)
 opts=options|{'now':at,'supersession':'owner-includes-ready-d','allowed_holds':[],
  'followup_policy':policy,'expected_previous':first['identity'],'prior':first}
 path=ps.prepare(tmp_path,**opts);c=cr.read(path)
 check=dict(now=at,committed=False,prior=first)
 ps.reserve(tmp_path,path,c['id'],**check)
 ps.send(tmp_path,path,c['id'],transport=lambda *_:None,env={'RECIPIENT_EMAIL':','.join(TO)},**check)
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 assert state['regular']=='d_hold' and 'd_update' not in state
 assert sender.plan(tmp_path,at)[0]['action']=='wait'
 assert 'no duplicate' in sender.plan(tmp_path,at)[0]['reason']


def test_owner_reholding_d_cannot_reopen_an_already_delivered_ready_instruction(tmp_path,monkeypatch):
 prior,release,_,_=first_correction(tmp_path,monkeypatch)
 state=sender.ledger_at(tmp_path)['anchors'][prior['anchor']]
 state['regular']='d_hold'
 state['preview_supersessions']['earlier-ready']={'decision':{'d_hold':False}}
 # The latest explicitly approved instruction is HOLD; a subsequent READY
 # change is another correction, not the first D follow-up slot again.
 assert decision(release,state,NOW,True,'another-ready-d')['action']=='alert'
 assert decision(release,state,NOW,False,release['europe_identity'])['action']=='wait'
