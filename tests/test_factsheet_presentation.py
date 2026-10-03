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
    # The at-a-glance table; a de-risk book has entries and trims but no resize up.
    assert html.index("What changes and why") < html.index("class='shifts'") < html.index("class='changes'")
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


def test_return_driver_bars_diverge_red_left_green_right(tmp_path,monkeypatch):
    """A loss draws in the left half in red, a gain in the right half in green."""
    import re
    from component_factsheet_view import TONE
    release=install(tmp_path,monkeypatch,ready=True)
    release.setdefault('presentation',{}).update(
        attribution=[dict(sleeve='A',weight=.35,ret=None,contribution=.0022),
                     dict(sleeve='D',weight=.20,ret=None,contribution=-.0063)],
        residual=0.0,start='2026-09-25',end='2026-10-02')
    html=render_html({"action":"regular","d_hold":False},release)
    bars=re.findall(r"text-align:(left|right);[^']*'><div style='[^']*width:([\d.]+)%;background:(#\w+)",html)
    assert bars==[('right','0.00',TONE['down']),('left','34.92',TONE['up']),
                  ('right','100.00',TONE['down']),('left','0.00',TONE['up'])]
    # A missing headline figure is short in its cell and named in the note.
    assert "Unavailable</strong>" not in html and ">n/a<" in html
    assert "Not available in this snapshot: One year." in html


def test_pdf_contribution_bars_are_green_for_gains_red_for_losses(monkeypatch):
    import build_factsheet as house
    import component_factsheet_view as view
    from matplotlib.colors import to_hex
    seen=[]
    monkeypatch.setattr(house,"_chart_to_image",lambda fig,w,dpi=0: seen.extend(
        (round(p.get_width(),2),to_hex(p.get_facecolor())) for p in fig.axes[0].patches))
    ctx={"attribution":[dict(sleeve="A",contribution=.0022),dict(sleeve="D",contribution=-.0063)]}
    view.sleeve_contribution_chart(ctx,480)
    assert seen==[(.22,view.TONE["up"]),(-.63,view.TONE["down"])]


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
    # The group heading, not a summary row's "Strategy D · Europe sectors" context line.
    d_heading="Strategy D · Europe sectors"+follow_up.split("<strong>Strategy D · Europe sectors")[1].split("</strong>")[0]
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
    from component_factsheet_view import SLEEVE_HEX, SLEEVE_KEY, TONE
    assert set(SLEEVE_HEX) == set(SLEEVE_KEY)
    for sleeve, key in SLEEVE_KEY.items():
        assert SLEEVE_HEX[sleeve].lower() == getattr(house, key).lower(), sleeve
    # The two overlays must not wear a ranked strategy's hue.
    assert len({SLEEVE_HEX[s] for s in ("A", "B", "C", "D", "TILT", "GATE")}) == 6
    for tone, source in (("up", house.GOOD), ("down", house.BAD), ("warn", house.WARN)):
        assert TONE[tone].lower() == source.hexval().replace("0x", "#"), tone


def test_no_house_hue_carries_two_meanings():
    """PALETTE_SPY was byte-identical to PALETTE_A: one blue, two meanings.

    Every line series the factsheet can draw in one document has to be
    separable from every other, or the legend on page five contradicts the
    attribution chart on page two.
    """
    import build_factsheet as house
    series = {name: getattr(house, "PALETTE_" + name)
              for name in ("BLEND", "SPY", "BENCH", "DD", "A", "B", "C", "D")}
    assert len(set(v.lower() for v in series.values())) == len(series), series
    # The per-ETF chart's own map is where the second instance hid, as a bare
    # literal equal to PALETTE_C. Every band in one figure must be separable.
    bars = house._SLEEVE_PALETTE
    assert len(set(v.lower() for v in bars.values())) == len(bars), bars
    assert set(bars.values()) <= set(series.values()), "chart colours must come from the palette"


@pytest.mark.parametrize("name,floor", [("GOOD", 4.5), ("BAD", 4.5), ("WARN", 4.5),
                                        ("INK", 4.5), ("INK_SOFT", 4.5)])
def test_house_text_colours_clear_wcag_aa_on_both_grounds(name, floor):
    """WARN is a text colour, so it answers to the text threshold.

    It was #b76e00 at 4.00:1 on white and 3.77:1 on the panel, used for the
    RESIZE action at 7.5pt and the watchlist badge at 8pt. Both grounds are
    checked because the badge sits on BG_PANEL, not on the page.
    """
    import build_factsheet as house

    def luminance(hexcode):
        channels = (int(hexcode[i:i + 2], 16) / 255 for i in (0, 2, 4))
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(a, b):
        first, second = luminance(a) + 0.05, luminance(b) + 0.05
        return max(first, second) / min(first, second)

    ink = getattr(house, name).hexval()[2:]
    for ground in (house.WHITE.hexval()[2:], house.BG_PANEL.hexval()[2:]):
        assert contrast(ink, ground) >= floor, (name, ground, contrast(ink, ground))


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


def test_d_confirmation_is_a_table_of_every_d_change(tmp_path, monkeypatch):
    """The D block uses the changes-table component, all D rows, no truncation,
    and carries the D instruction once under its own heading (2026-09-27)."""
    release = install(tmp_path, monkeypatch, ready=True, gate=True)
    decision = {"action": "regular", "d_hold": False}
    v = view_model(decision, release)
    html = render_html(decision, release)
    block = html.split("D confirmation")[1]
    d_rows = [r for r in v["changed"] if r["sleeve"] == "D"]
    assert d_rows
    assert "<table class='changes'" in block
    assert block.count("class='d-confirm'") == len(d_rows)
    assert "<strong>Strategy D:</strong>" not in html
    assert " → target " not in block


def _two_cash_lines(release):
    """B's cash floor and C's fired sleeve gate in one book: two SHY lines."""
    book=release["book"]
    b=next(s for s in book["sleeves"] if s["sleeve"]=="B")
    c=next(s for s in book["sleeves"] if s["sleeve"]=="C")
    b.update(top_k=7,weights={**{f"B{i}":.12 for i in range(6)},"SHY":.142857})
    c.update(top_k=5,weights={"SHY":1.0},gate={"enabled":True,"fired":True,"n_above":6,
             "n_universe":25,"breadth":.24,"floor":.05,"threshold":.3})
    smh=next(r for r in book["lines"] if r["etf"]=="SMH")
    smh.update(target=0.0,delta=-smh["held"])
    book["lines"]+=[{"sleeve":"C","etf":"SHY","traded":"SHY","held":0.0,"target":.1,"delta":.1,"status":"READY"},
                    {"sleeve":"B","etf":"SHY","traded":"SHY","held":0.0,"target":.05,"delta":.05,"status":"READY"}]
    release["labels"]["SHY"]="iShares 1-3y US Treasury (sleeve cash floor)"
    return release


def test_two_cash_lines_carry_their_strategy_and_reason(tmp_path,monkeypatch):
    """Two SHY rows read as a duplicate when neither named its strategy (2026-10-03)."""
    import re
    release=_two_cash_lines(install(tmp_path,monkeypatch,ready=True))
    html=render_html({"action":"regular","d_hold":False},release)
    glance=re.search(r"<table class='shifts'.*?</table>",html,re.S).group(0)
    assert glance.count(">SHY</strong>")==2
    assert "Strategy C · Thematic · sleeve-breadth gate: 6 of 25 above +5%, 8 needed" in glance
    assert "sleeve-breadth gate: 6 of 25 names above the +5% floor, under the 30% threshold (8 needed)" in html
    assert "Strategy B · Asset classes · cash floor: 6 of 7 slots qualify; 1 unfilled" in glance
    assert "Strategy C · Thematic · exits on the sleeve-breadth gate" in glance
    # One reason for two lines is gone from every surface.
    assert "sleeve cash floor" not in html
    # Under a fired gate no name was decided on, so no rank is the driver.
    story=sleeve_story(next(s for s in view_model({"action":"regular"},release)["shifts"] if s["sleeve"]=="C"),release)
    assert story.startswith("Sleeve-breadth gate on: 6 of 25") and "Re-ranked" not in story
    assert "No comparable signal recorded" not in html


def test_an_entry_or_exit_is_listed_once_at_a_glance(tmp_path,monkeypatch):
    release=_two_cash_lines(install(tmp_path,monkeypatch,ready=True))
    v=view_model({"action":"regular"},release)
    grouped=v["increases"]+v["reductions"]+v["entering"]+v["exiting"]
    assert len(grouped)==len({id(r) for r in grouped})
    assert all(action_of(r)=="ADD" for r in v["increases"])
    assert all(action_of(r)=="TRIM" for r in v["reductions"])


def test_float_noise_never_prints_a_bare_decimal_point():
    assert pp(-1e-17)=="+0.00pp" and pp(1e-17)=="+0.00pp"
    assert pp(-.00003535)=="-0.003535pp"


def test_complete_book_holds_one_line_per_instrument(tmp_path,monkeypatch):
    """The account holds one SHY position, so the flat book shows one (2026-10-03)."""
    from component_factsheet_view import book_by_instrument
    release=_two_cash_lines(install(tmp_path,monkeypatch,ready=True))
    rows=book_by_instrument(release["book"]["lines"])
    shy=[r for r in rows if r["traded"]=="SHY"]
    assert len(shy)==1 and shy[0]["sleeves"]==["B","C"]
    assert shy[0]["target"]==pytest.approx(.15) and shy[0]["split"]=="B 5.00% + C 10.00%"
    assert len(rows)==len({r["traded"] for r in release["book"]["lines"]})
    assert render_pdf({"action":"regular","d_hold":False},release).startswith(b"%PDF-")


def test_b_cash_slot_names_the_name_that_fell_below_the_floor(tmp_path,monkeypatch):
    """SHY's B slot opened because VGK crossed below its 200-day average (2026-10-03)."""
    import component_factsheet_view as view
    assert view.ELIGIBILITY_FLOOR=={"B":0.0,"C":0.05}
    release=install(tmp_path,monkeypatch,ready=True)
    book=release["book"]
    b=next(s for s in book["sleeves"] if s["sleeve"]=="B")
    qqq=next(r for r in book["lines"] if r["etf"]=="QQQ")
    b.update(top_k=2,weights={"VGKX":.5,"SHY":.5},
             signals={"QQQ":-.0021,"VGKX":.05,"EFAX":-.03},signals_prev={"QQQ":.0259,"VGKX":.06,"EFAX":-.02})
    qqq.update(target=0.0,delta=-qqq["held"])
    book["lines"]+=[{"sleeve":"B","etf":"VGKX","traded":"VGKX","held":.175,"target":.175,"delta":0.0,"status":"READY"},
                    {"sleeve":"B","etf":"SHY","traded":"SHY","held":0.0,"target":.175,"delta":.175,"status":"READY"}]
    shy=book["lines"][-1]
    assert view.floor_fallers("B",book)==[qqq]
    assert view.cash_reason(shy,book,short=True)=="cash floor: QQQ fell below its 200-day average; 1 of 2 slots filled"
    assert view.cash_reason(shy,book)==("cash floor: only 1 of the 3 ranked ETFs are above their 200-day average "
                                        "for 2 slots after QQQ fell below it, +2.59% to -0.21%; SHY takes the unfilled slot")
    assert view.line_evidence(qqq,book).endswith("fell below its 200-day average; its slot goes to SHY")
    story=sleeve_story(next(s for s in view_model({"action":"regular"},release)["shifts"] if s["sleeve"]=="B"),release)
    assert "fell below its 200-day average, +2.59% to -0.21%, and exits" in story
    assert "so SHY takes the unfilled weight at 17.50% of NAV" in story
