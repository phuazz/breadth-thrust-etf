"""Presentation parity and integrity without real email."""
from copy import deepcopy
import base64
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from component_factsheet_view import exact_return, rationale, render_html, render_pdf, verified_context, pp, render_text, context_from_sources
from test_component_sender import install, NOW
import component_release as cr
import send_component_factsheet as sender


@pytest.mark.parametrize("start,end",[("2026-08-31","2026-09-04"),("2026-12-31","2027-01-04")])
def test_exact_window_boundaries(start,end):
    assert exact_return([start,end],[100,110],start,end)==pytest.approx(.1)
    assert exact_return([start],[100],start,end) is None
    assert exact_return([end],[110],start,end) is None


def test_no_shortened_or_invalid_window():
    assert exact_return(["2026-09-04","2026-09-10"],[100,110],"2026-09-04","2026-09-11") is None
    assert exact_return(["2026-09-04","2026-09-11"],[100,float('nan')],"2026-09-04","2026-09-11") is None
    assert exact_return(["2026-09-04","2026-09-04"],[100,110],"2026-09-04","2026-09-11") is None
    assert pp(.0000002)!="+0.00pp"


def test_source_drift_cannot_enrich_preview(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch)
    cr.write(tmp_path/"data/risk_overlay.json",{"current_breadth":.99})
    with pytest.raises(ValueError,match="presentation source"):
        verified_context(tmp_path,release)


def test_candidate_contains_pdf_and_preserves_no_send(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch,rounded_d=True)
    assert sender.prepare(tmp_path,NOW)["action"]=="preview"
    candidate=cr.read(tmp_path/sender.OUT/"candidate.json")
    pdf=base64.b64decode(candidate["pdf_base64"])
    assert pdf.startswith(b"%PDF-")
    assert (tmp_path/sender.OUT/candidate["pdf_filename"]).read_bytes()==pdf
    assert not (tmp_path/sender.LEDGER).exists()
    assert "Strategy D" in candidate["html"]
    assert "Thursday-close substitute" in candidate["html"]
    assert 'Performance is provisional' in candidate['html']
    assert "Historical" not in candidate["subject"]


def test_pdf_is_deterministic_and_every_position_is_present(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,rounded_d=True)
    decision={"action":"preview","d_hold":True}
    first=render_pdf(decision,release)
    assert first==render_pdf(decision,release)
    # Production dependencies do not require a PDF parser; rendering checks
    # use the bundled parser separately in the no-send visual rehearsal.
    html=render_html(decision,release,True)
    assert html.count("class='position'")==len(release["book"]["lines"])
    assert "zero" not in pp(.000001)


def test_top_six_is_disclosed_and_d_risk_change_is_not_a_rank(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,gate=True)
    r=next(r for r in release["book"]["lines"] if r["sleeve"]=="D")
    assert "risk adjustment" in rationale(r,release["book"])
    assert "rank" not in rationale(r,release["book"])
    html=render_html({"action":"preview","d_hold":True},release)
    assert "ordered by size" in html and "Every change" in html
    assert html.index("The week in numbers") < html.index("What changes next")


def test_signal_story_does_not_claim_stale_hold_ranking(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,rounded_d=True)
    r=next(r for r in release["book"]["lines"] if r["sleeve"]=="D")
    release["book"]["sleeves"][-1]["signals"]={r["etf"]:.9}
    assert "pending complete data" in rationale(r,release["book"])


def test_pdf_attached_to_same_message_as_html(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch)
    sender.prepare(tmp_path,NOW)
    candidate=cr.read(tmp_path/sender.OUT/"candidate.json")
    sent=[]
    class SMTP:
        def __init__(self,*a,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def login(self,*a):pass
        def send_message(self,msg,**kwargs):sent.append((msg,kwargs));return {}
    monkeypatch.setattr(sender.smtplib,"SMTP_SSL",SMTP)
    sender.smtp_send(candidate,{"GMAIL_USER":"sender@example.invalid","GMAIL_APP_PASSWORD":"test", "RECIPIENT_EMAIL":"one@example.invalid,two@example.invalid"})
    msg,kw=sent[0]
    assert len(kw['to_addrs'])==2
    attachments=list(msg.iter_attachments())
    assert [a.get_content_type() for a in attachments]==['application/pdf','text/html','application/json']
    assert attachments[0].get_payload(decode=True)==base64.b64decode(candidate['pdf_base64'])
    assert 'This week' in msg.get_body(preferencelist=('plain',)).get_content()


def test_missing_d_endpoint_is_not_invented_for_return_drivers(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch)
    release['performance']['wtd_start']='2026-09-04'
    source={'dates':['2026-09-04','2026-09-10'],'equity':[100,110]}
    context=context_from_sources(release,lambda path: source if 'rotation' in path else {})
    d=next(r for r in context['attribution'] if r['sleeve']=='D')
    assert d['ret'] is None and d['contribution'] is None
    assert not context['coverage_complete'] and context['residual'] is None
    assert 'breadth' not in context


def test_every_d_change_is_highlighted_even_outside_top_six(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,ready=True)
    # Rendering-only fixture: all these core moves outrank D in magnitude.
    core=deepcopy(release['book']['lines'][0])
    release['book']['lines']=[{**core,'etf':f'core{i}','traded':f'core{i}','delta':.01} for i in range(7)]
    release['book']['lines'].append({**core,'sleeve':'D','etf':'EXV1','traded':'EXV1','delta':.001})
    html=render_html({'action':'regular','d_hold':False},release)
    assert 'EXV1' in html.split('D confirmation')[1]
    assert '6 of 8 changes shown' in html
    text=render_text({'action':'regular','d_hold':False},release)
    assert 'D confirmation' in text and 'not executed trades' in text
