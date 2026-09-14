"""Presentation parity and integrity without real email.

The layout contract lives here: section 02 must answer what the portfolio does
before it answers what each fund does, and it must never state a mechanism the
sealed book does not record.
"""
from copy import deepcopy
import base64
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from component_factsheet_view import (EMAIL_CHANGE_LIMIT, MATERIAL_NAV, MODEL_ROUNDING_NAV,
                                      action_of, budget_sentence, budgets_held, context_from_sources,
                                      exact_return, pp, rationale, render_html, render_pdf, render_text,
                                      signal_cell, sizing_note, sizing_scheme, sleeve_shifts,
                                      sleeve_story, verified_context, view_model)
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


def test_portfolio_answer_precedes_the_fund_list(tmp_path,monkeypatch):
    """Section 02 leads with the strategy budgets, then the largest moves."""
    release=install(tmp_path,monkeypatch,gate=True)
    html=render_html({"action":"preview","d_hold":True},release)
    assert html.index("The week in numbers") < html.index("What changes and why")
    assert html.index("What changes and why") < html.index("Increased") < html.index("class='changes'")
    assert "Unchanged" in html


def test_unchanged_budgets_are_computed_not_asserted(tmp_path,monkeypatch):
    """The budget sentence follows the book: a de-risk must not read 'unchanged'."""
    steady=view_model({"action":"preview","d_hold":True},install(tmp_path,monkeypatch))
    assert steady["budgets_held"] and "budgets do not change" in budget_sentence(steady)
    derisked=install(tmp_path,monkeypatch,gate=True)
    shifts=sleeve_shifts(derisked["book"])
    assert not budgets_held(shifts)
    sentence=budget_sentence(view_model({"action":"preview","d_hold":True},derisked))
    assert "budgets change this week" in sentence and "unchanged" not in sentence
    assert all(abs(s["net"]-(s["target"]-s["held"]))<1e-15 for s in shifts)


def test_entries_and_exits_are_distinguished_from_resizing():
    rows=[{"held":0,"target":.04,"delta":.04},{"held":.01,"target":0,"delta":-.01},
          {"held":.05,"target":.07,"delta":.02},{"held":.05,"target":.03,"delta":-.02},
          {"held":.05,"target":.05,"delta":0.}]
    assert [action_of(r) for r in rows]==["ENTER","EXIT","ADD","TRIM","HOLD"]


def test_every_change_is_listed_once_with_its_direction(tmp_path,monkeypatch):
    # A de-risk resizes every sleeve, so the book carries a change per line.
    release=install(tmp_path,monkeypatch,ready=True,gate=True)
    v=view_model({"action":"regular","d_hold":False},release)
    html=render_html({"action":"regular","d_hold":False},release)
    assert html.count("class='position'")==len(v["changed"])
    assert f"All {len(v['changed'])} proposed changes are listed above" in html
    for row in v["changed"]:
        assert f">{row['traded']}</strong>" in html
        assert pp(row["delta"]) in html


def test_email_truncates_only_when_the_week_is_pathological(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,ready=True)
    core=deepcopy(release["book"]["lines"][0])
    release["book"]["lines"]=[{**core,"etf":f"core{i}","traded":f"core{i}",
                               "held":.01,"target":.02,"delta":.01} for i in range(EMAIL_CHANGE_LIMIT+5)]
    html=render_html({"action":"regular","d_hold":False},release)
    assert html.count("class='position'")==EMAIL_CHANGE_LIMIT
    assert f"{EMAIL_CHANGE_LIMIT} of {EMAIL_CHANGE_LIMIT+5} changes shown" in html
    assert "Every change and unchanged position is in the attached PDF" in html


def test_top_six_is_disclosed_and_d_risk_change_is_not_a_rank(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,gate=True)
    r=next(r for r in release["book"]["lines"] if r["sleeve"]=="D")
    assert "risk adjustment" in rationale(r,release["book"])
    assert "rank" not in rationale(r,release["book"])


def test_signal_story_does_not_claim_stale_hold_ranking(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,rounded_d=True)
    r=next(r for r in release["book"]["lines"] if r["sleeve"]=="D")
    release["book"]["sleeves"][-1]["signals"]={r["etf"]:.9}
    assert "pending complete data" in rationale(r,release["book"])


def test_signal_units_follow_the_recorded_signal_kind():
    """Relative breadth reads in percentage points; levels read as percentages."""
    relative={"signal_kind":"breadth_relative","signals":{"X":.1102,"Y":.2144},
              "signals_prev":{"X":.0188,"Y":.2376}}
    assert signal_cell("X",relative)=="+1.88 to +11.02pp · rank 2 of 2"
    level={"signal_kind":"breadth","signals":{"X":.7812,"Y":.9254},
           "signals_prev":{"X":.75,"Y":.9254}}
    assert signal_cell("X",level)=="75.00 to 78.12% · rank 2 of 2"
    distance={"signal_kind":"ma_distance","signals":{"X":.2118},"signals_prev":{}}
    assert signal_cell("X",distance)=="+21.18% · rank 1 of 1"
    assert signal_cell("missing",distance)=="No comparable signal recorded"


def test_weighting_is_named_only_when_the_book_confirms_it():
    """An unrecognised scheme is described as nothing, never guessed."""
    signals={"A":.3116,"B":.2144,"C":.1588}
    total=sum(signals.values())
    proportional={"signals":signals,"weights":{k:round(v/total,6) for k,v in signals.items()}}
    assert "in proportion" in sizing_scheme(proportional)
    equal={"signals":signals,"weights":{k:.2 for k in signals}}
    assert sizing_scheme(equal)=="equal-weighted across the qualifying members"
    ranked={"signals":signals,"weights":{"A":.5,"B":.3,"C":.2}}
    assert sizing_scheme(ranked) is None
    assert sizing_scheme({"signals":signals,"weights":{"A":.6,"D":.4}}) is None
    # Disagreeing strategies mean the note is dropped, not averaged away.
    book={"sleeves":[{"sleeve":"A",**proportional},{"sleeve":"B",**equal}]}
    shifts=[{"sleeve":"A","changed":[1]},{"sleeve":"B","changed":[1]}]
    assert sizing_note(shifts,book) is None
    assert "in proportion" in sizing_note(shifts[:1],book)


def test_every_d_change_keeps_its_own_group_and_confirmation(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,ready=True)
    # Rendering-only fixture: all these core moves outrank D in magnitude.
    core=deepcopy(release['book']['lines'][0])
    release['book']['lines']=[{**core,'etf':f'core{i}','traded':f'core{i}','delta':.01} for i in range(7)]
    release['book']['lines'].append({**core,'sleeve':'D','etf':'EXV1','traded':'EXV1','delta':.001})
    html=render_html({'action':'regular','d_hold':False},release)
    assert 'Strategy D · Europe sectors' in html
    assert 'EXV1' in html.split('D confirmation')[1]
    assert html.index('Strategy A · US sectors') < html.index('Strategy D · Europe sectors')
    text=render_text({'action':'regular','d_hold':False},release)
    assert 'D confirmation' in text and 'not executed trades' in text


def test_small_entries_are_sized_against_the_house_threshold(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,ready=True)
    core=deepcopy(release["book"]["lines"][0])
    release["book"]["lines"]=[{**core,"etf":"TINY","traded":"TINY","held":0.,
                               "target":MATERIAL_NAV/10,"delta":MATERIAL_NAV/10}]
    assert "ranking-tail positions" in render_html({"action":"regular","d_hold":False},release)
    release["book"]["lines"]=[{**core,"etf":"BIG","traded":"BIG","held":0.,
                               "target":MATERIAL_NAV*2,"delta":MATERIAL_NAV*2}]
    assert "ranking-tail positions" not in render_html({"action":"regular","d_hold":False},release)


def test_plain_text_keeps_one_table_row_on_one_line(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,ready=True,gate=True)
    text=render_text({"action":"regular","d_hold":False},release)
    row=next(line for line in text.splitlines() if line.startswith("SPY"))
    assert row.count("·")==3 and "%" in row
    assert "This week" in text and "Held · Target · Change" in text


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


def test_rounding_sized_shift_is_not_reported_as_a_budget_decision(tmp_path,monkeypatch):
    """The HOLD rounding residual must not read as a strategy reallocation."""
    release=install(tmp_path,monkeypatch,rounded_d=True)
    shifts=sleeve_shifts(release["book"])
    assert budgets_held(shifts)
    assert all(abs(s["net"])<=MODEL_ROUNDING_NAV for s in shifts)
    html=render_html({"action":"preview","d_hold":True},release)
    assert "budgets do not change" in html
    assert "small rounding differences in totals are not trades" in html


def test_only_a_d_follow_up_may_claim_an_earlier_email(tmp_path,monkeypatch):
    """A d_update is authorised only on an unchanged core, so it can say so."""
    release=install(tmp_path,monkeypatch,ready=True,gate=True)
    follow_up=render_html({"action":"d_update","d_hold":False},release)
    assert "already sent in the initial email" in follow_up
    assert "unchanged from the initial email; do not submit them a second time" in follow_up
    # D itself is the new content, so its own heading never claims otherwise.
    d_heading="Strategy D · Europe sectors"+follow_up.split("Strategy D · Europe sectors")[1].split("</strong>")[0]
    assert "newly verified this week" in d_heading and "already sent" not in d_heading
    single=render_html({"action":"regular","d_hold":False},release)
    assert "initial email" not in single
    assert "All D changes are shown here" in single


def test_email_sleeve_colours_cannot_drift_from_the_dashboard_palette():
    """The email cannot import matplotlib, so its hues are literals.

    build_factsheet.py owns them. This is the guard that keeps the copy
    honest, since a silent divergence is exactly how two surfaces of one
    publication stop looking like one publication.
    """
    import build_factsheet as house
    from component_factsheet_view import SLEEVE_HEX, SLEEVE_KEY
    assert set(SLEEVE_HEX) == set(SLEEVE_KEY)
    for sleeve, key in SLEEVE_KEY.items():
        assert SLEEVE_HEX[sleeve].lower() == getattr(house, key).lower(), sleeve
    # The two overlays must not wear a ranked strategy's hue.
    assert len({SLEEVE_HEX[s] for s in ("A", "B", "C", "D", "TILT", "GATE")}) == 6


def test_every_colour_carries_a_word_and_a_dark_override(tmp_path,monkeypatch):
    """Colour is never the only signal, and never only a light-theme signal."""
    from component_factsheet_view import TONE
    release=install(tmp_path,monkeypatch,ready=True,gate=True)
    html=render_html({"action":"regular","d_hold":False},release)
    for tone,hexcode in TONE.items():
        if f"tone {tone}" not in html:
            continue
        assert hexcode in html
        assert f"html[data-theme=dark] .tone.{tone}{{color:" in html
    # An ENTER or EXIT is readable with every colour stripped.
    assert "ENTER" in html or "EXIT" in html or "RESIZE" in html


def test_sleeve_story_is_absent_without_a_change(tmp_path,monkeypatch):
    release=install(tmp_path,monkeypatch,ready=True)
    shift=next(s for s in sleeve_shifts(release["book"]) if not s["changed"])
    assert sleeve_story(shift,release) is None
