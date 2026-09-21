"""Tests for scripts/emit_state.py — the STATE_CONTRACT emission.

The emission is a COPY of values this repo already publishes, so the tests that
matter are not about arithmetic. They are about the two ways a copy goes wrong:

  1. It emits a guess. A renamed or null key must stop the emission rather than
     produce a null that reads downstream as a state. Every pointer is asserted.
  2. It emits a stale file. A failed run must leave the previous state.json
     untouched and say so, never half-write a new one.

The shape itself is checked against the field list the consumer validates on,
because a drift there is rejected on that side and the rejection is easier to
diagnose here, beside the data.

Synthetic payloads stand in for the on-disk JSON, so the tests do not move with
the market — with one deliberate exception at the foot of this file. Synthetic
inputs alone let a real defect lie dormant: the tilt's off-label mismatch sat
unexercised from 2025-04-07 to 2026-09-15 because no fixture carried an off
state and the live one never was one. The last two tests therefore read the
engine's own COMMITTED `risk_overlay.json` and assert the emitter can carry
whatever is in it. They do not assert which state is live, only that it has a
contract label, so they stay stable as the market moves.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import emit_state  # noqa: E402

# The ten required contract fields plus the two optional additions.
REQUIRED = {"as_of", "state", "value", "zone", "role", "horizon",
            "evidence_grade", "licence", "action_hint", "source_file"}
OPTIONAL = {"computed_at", "cadence"}

SIGNALS = {"engine_phase19_gate", "engine_phase22_em_tilt", "engine_live_blend"}


def _risk_overlay(**over):
    d = {
        "panel_end_date": "2026-08-25",
        "current_state": "RISK_ON",
        "current_state_since": "2026-04-13",
        "current_breadth": 0.6447,
        "computed_at_utc": "2026-08-26T02:11:00Z",
        "phase22_eem_tilt": {
            "current_state": "EM_TILT_ON",
            "current_state_since": "2025-11-07",
            "current_ratio": 1.0412,
        },
    }
    d.update(over)
    return d


def _live_track(**over):
    d = {
        "anchor_date": "2026-08-24",
        "deployed_key": "blend_35_35_10_20_gated_eem_tilted",
        "effective_weights": {f"E{i}": 0.04 for i in range(23)},
        "regime_state": "RISK_ON",
        "eem_tilt_active": True,
        "computed_at_utc": "2026-08-26T02:11:00Z",
    }
    d.update(over)
    return d


@pytest.fixture
def files(monkeypatch):
    """Serve synthetic inputs in place of the on-disk data files."""
    store = {"risk_overlay.json": _risk_overlay(), "live_track.json": _live_track()}

    def fake_load(name):
        if name not in store:
            raise emit_state.EmitError(f"source file not found: {name}")
        return store[name]

    monkeypatch.setattr(emit_state, "load", fake_load)
    return store


# --- the shape the consumer validates on ------------------------------------

def test_emits_exactly_the_three_engine_signals(files):
    assert set(emit_state.build()["signals"]) == SIGNALS


def test_envelope_carries_version_and_source(files):
    p = emit_state.build()
    assert p["contract_version"] == "1"
    assert p["emitted_by"] == "breadth-thrust-etf"
    assert p["emitted_at"].endswith("+00:00") or p["emitted_at"].endswith("Z")


def test_every_block_carries_the_required_fields_and_nothing_unknown(files):
    for sid, block in emit_state.build()["signals"].items():
        assert REQUIRED <= set(block), f"{sid} missing {REQUIRED - set(block)}"
        assert set(block) <= REQUIRED | OPTIONAL, f"{sid} has {set(block) - REQUIRED - OPTIONAL}"


def test_no_score_or_weight_field_is_emitted(files):
    """The consumer's policy forbids averaging signals and its schema has no
    score field. Emitting one would be rejected there; never emit one."""
    banned = {"score", "weight", "weights", "composite", "rank"}
    for block in emit_state.build()["signals"].values():
        assert not (banned & set(block))


def test_values_are_copied_from_the_source_files(files):
    s = emit_state.build()["signals"]
    assert s["engine_phase19_gate"]["as_of"] == "2026-08-25"
    assert s["engine_phase19_gate"]["state"] == "RISK_ON"
    assert s["engine_phase19_gate"]["value"] == 0.6447
    assert s["engine_phase19_gate"]["zone"] == "since 2026-04-13"
    assert s["engine_phase22_em_tilt"]["value"] == 1.0412
    assert s["engine_phase22_em_tilt"]["zone"] == "since 2025-11-07"
    # The blend's headline number is the holding COUNT, not a weight.
    assert s["engine_live_blend"]["value"] == 23
    assert s["engine_live_blend"]["zone"] == "blend_35_35_10_20_gated_eem_tilted"
    assert s["engine_live_blend"]["as_of"] == "2026-08-24"


def test_the_gate_and_tilt_share_the_panel_end_date(files):
    """Both come off risk_overlay.json, so a divergence between them would mean
    the emitter had invented a date rather than copied one."""
    s = emit_state.build()["signals"]
    assert s["engine_phase19_gate"]["as_of"] == s["engine_phase22_em_tilt"]["as_of"]


# --- it must never emit a guess ---------------------------------------------

@pytest.mark.parametrize("pointer", [
    "panel_end_date", "current_state", "current_state_since", "current_breadth",
])
def test_a_missing_risk_overlay_key_stops_the_emission(files, pointer):
    del files["risk_overlay.json"][pointer]
    with pytest.raises(emit_state.EmitError, match=pointer):
        emit_state.build()


@pytest.mark.parametrize("pointer", ["anchor_date", "deployed_key", "effective_weights"])
def test_a_missing_live_track_key_stops_the_emission(files, pointer):
    del files["live_track.json"][pointer]
    with pytest.raises(emit_state.EmitError, match=pointer):
        emit_state.build()


def test_a_renamed_nested_tilt_key_stops_the_emission(files):
    files["risk_overlay.json"]["phase22_eem_tilt"].pop("current_ratio")
    with pytest.raises(emit_state.EmitError, match="current_ratio"):
        emit_state.build()


def test_an_explicit_null_is_refused_rather_than_emitted(files):
    files["risk_overlay.json"]["current_breadth"] = None
    with pytest.raises(emit_state.EmitError, match="null"):
        emit_state.build()


def test_a_wrong_type_is_refused(files):
    files["risk_overlay.json"]["current_breadth"] = "0.64"
    with pytest.raises(emit_state.EmitError, match="expected"):
        emit_state.build()


def test_a_state_outside_the_frozen_vocabulary_is_refused(files):
    """Catching it here names the file. Left to the consumer it is rejected
    anyway, but from a repo the reader is not looking at."""
    files["risk_overlay.json"]["current_state"] = "RISK_MAYBE"
    with pytest.raises(emit_state.EmitError, match="RISK_MAYBE"):
        emit_state.build()


def test_a_tilt_state_outside_its_vocabulary_is_refused(files):
    files["risk_overlay.json"]["phase22_eem_tilt"]["current_state"] = "ON"
    with pytest.raises(emit_state.EmitError, match="ON"):
        emit_state.build()


def test_the_contracts_own_off_label_is_not_accepted_from_the_engine(files):
    """`OFF` is what the CONSUMER is given, not what the engine writes. If it
    ever appears in risk_overlay.json the engine has changed underneath this
    script, and the emission should stop rather than coincidentally work."""
    files["risk_overlay.json"]["phase22_eem_tilt"]["current_state"] = "OFF"
    with pytest.raises(emit_state.EmitError, match="OFF"):
        emit_state.build()


# --- the tilt off branch: engine label EM_TILT_OFF -> contract label OFF -----
# This mapping sat unexercised from 2025-04-07 to 2026-09-15 because the tilt
# was continuously on. When it flipped, the check refused to emit and the
# published state.json froze carrying a superseded EM_TILT_ON. Every test below
# exists so that cannot lie dormant a second time.

def test_the_engine_off_label_is_translated_to_the_contract_label(files):
    files["risk_overlay.json"]["phase22_eem_tilt"]["current_state"] = "EM_TILT_OFF"
    assert emit_state.build()["signals"]["engine_phase22_em_tilt"]["state"] == "OFF"


def test_the_engine_on_label_passes_through_unchanged(files):
    assert emit_state.build()["signals"]["engine_phase22_em_tilt"]["state"] == "EM_TILT_ON"


def test_the_tilt_off_branch_writes_a_file_the_consumer_accepts(files, monkeypatch, tmp_path):
    """End to end: a genuine off state must emit, not refuse. The live failure
    was a refusal, so asserting the mapped value alone would not reproduce it —
    the run has to reach exit 0 with a file on disk."""
    files["risk_overlay.json"]["phase22_eem_tilt"].update(
        {"current_state": "EM_TILT_OFF", "current_state_since": "2026-09-15",
         "current_ratio": 0.088})
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)

    assert emit_state.main([]) == 0
    block = json.loads(out.read_text(encoding="utf-8"))["signals"]["engine_phase22_em_tilt"]
    assert block["state"] == "OFF"
    assert block["state"] in emit_state.TILT_STATES
    assert block["value"] == 0.088
    assert block["zone"] == "since 2026-09-15"


def test_the_gate_off_branch_writes_a_file_the_consumer_accepts(files, monkeypatch, tmp_path):
    """The same end-to-end check on the gate. Its labels happen to match the
    contract's, but that is a fact to pin, not one to keep assuming."""
    files["risk_overlay.json"]["current_state"] = "RISK_OFF"
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)

    assert emit_state.main([]) == 0
    block = json.loads(out.read_text(encoding="utf-8"))["signals"]["engine_phase19_gate"]
    assert block["state"] == "RISK_OFF"
    assert block["state"] in emit_state.GATE_STATES


def test_every_state_the_map_produces_is_in_the_contract_vocabulary():
    """A mis-edit of the map could translate a label into another value the
    consumer also rejects. The map's outputs are the contract, by definition."""
    assert set(emit_state.TILT_FROM_ENGINE.values()) <= set(emit_state.TILT_STATES)


def test_both_engine_tilt_labels_are_mapped():
    """The map must be exhaustive against the engine, not against whichever
    state is live. Both branches, named explicitly."""
    assert set(emit_state.TILT_FROM_ENGINE) == {"EM_TILT_ON", "EM_TILT_OFF"}


# --- the committed engine output must be emittable as it stands --------------

def test_the_live_committed_overlay_carries_states_this_emitter_can_publish():
    """The guard that ends the dormancy.

    Every test above runs on synthetic payloads, which is why a label the
    engine had genuinely started writing could sit unnoticed. This one reads
    the engine's own committed output and asserts the emitter can carry what is
    actually in it. It goes red on the first commit that introduces a state
    outside the contract — in this repo's CI, beside the data, rather than as
    an ERROR row on the consumer's board days later.

    `data/risk_overlay.json` is a committed artefact, not a generated one, so
    this reads a file the repo guarantees (commits 0a4e3837 / d14026ac).

    It is not a market test: it does not care WHICH state is live, only that
    whichever one is live has a contract label.
    """
    ro = json.loads((emit_state.REPO / "data" / "risk_overlay.json")
                    .read_text(encoding="utf-8"))

    gate = ro["current_state"]
    assert gate in emit_state.GATE_STATES, (
        f"the engine's committed gate state {gate!r} has no contract label")

    tilt = ro["phase22_eem_tilt"]["current_state"]
    assert tilt in emit_state.TILT_FROM_ENGINE, (
        f"the engine's committed tilt state {tilt!r} has no contract label; add it to "
        "TILT_FROM_ENGINE — do not widen the contract vocabulary to accept it")


def test_the_live_committed_overlay_emits_without_error():
    """The same file, through the real build() rather than a field check — so a
    future pointer rename is caught here too, not only a state rename."""
    payload = emit_state.build()
    assert set(payload["signals"]) == SIGNALS
    assert payload["signals"]["engine_phase22_em_tilt"]["state"] in emit_state.TILT_STATES
    assert payload["signals"]["engine_phase19_gate"]["state"] in emit_state.GATE_STATES


def test_an_empty_deployed_blend_is_refused(files):
    files["live_track.json"]["effective_weights"] = {}
    with pytest.raises(emit_state.EmitError, match="empty"):
        emit_state.build()


def test_an_absent_optional_field_is_carried_as_none_not_invented(files):
    del files["risk_overlay.json"]["computed_at_utc"]
    s = emit_state.build()["signals"]
    assert s["engine_phase19_gate"]["computed_at"] is None


# --- a failed run must not leave a half-written file -------------------------

def test_a_failed_run_writes_nothing_and_exits_non_zero(files, monkeypatch, tmp_path, capsys):
    out = tmp_path / "state.json"
    out.write_text('{"previous": "emission"}', encoding="utf-8")
    monkeypatch.setattr(emit_state, "OUT", out)
    files["risk_overlay.json"]["current_state"] = "NONSENSE"

    assert emit_state.main([]) == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {"previous": "emission"}
    assert "FAILED" in capsys.readouterr().err


def test_check_mode_writes_nothing(files, monkeypatch, tmp_path):
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)
    assert emit_state.main(["--check"]) == 0
    assert not out.exists()


def test_a_successful_run_writes_valid_json(files, monkeypatch, tmp_path):
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)
    assert emit_state.main([]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert set(written["signals"]) == SIGNALS


# --- an unchanged state must not churn the repo ------------------------------

def test_an_unchanged_state_is_not_rewritten(files, monkeypatch, tmp_path):
    """emitted_at moves every run. Writing unconditionally left a diff every
    time, and the workflow committed a no-op to a public repo on every weekday
    run — observed once live before this guard existed."""
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)

    assert emit_state.main([]) == 0
    first = out.read_text(encoding="utf-8")

    assert emit_state.main([]) == 0
    assert out.read_text(encoding="utf-8") == first, "unchanged state was rewritten"


def test_a_changed_state_IS_rewritten(files, monkeypatch, tmp_path):
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)
    assert emit_state.main([]) == 0

    files["risk_overlay.json"]["current_state"] = "RISK_OFF"
    assert emit_state.main([]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["signals"] \
        ["engine_phase19_gate"]["state"] == "RISK_OFF"


def test_only_the_timestamp_differing_counts_as_unchanged(files, monkeypatch, tmp_path):
    out = tmp_path / "state.json"
    monkeypatch.setattr(emit_state, "OUT", out)
    payload = emit_state.build()
    out.write_text(json.dumps({**payload, "emitted_at": "1999-01-01T00:00:00+00:00"},
                              indent=2) + "\n", encoding="utf-8")
    assert emit_state.unchanged(emit_state.build()) is True


def test_an_unreadable_previous_emission_is_rewritten(files, monkeypatch, tmp_path):
    """A corrupt file must be replaced, not treated as 'unchanged' and left."""
    out = tmp_path / "state.json"
    out.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(emit_state, "OUT", out)
    assert emit_state.unchanged(emit_state.build()) is False
    assert emit_state.main([]) == 0
    assert set(json.loads(out.read_text(encoding="utf-8"))["signals"]) == SIGNALS
