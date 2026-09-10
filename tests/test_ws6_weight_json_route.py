"""Offline guard on the WS6 A3 JSON weight route.

The A3 weight tables are the frozen basis of a BINDING register, and since
2026-09-10 they may be built from a SECOND source: the product-data API's
``holdingPercent``, adopted because the CSV holdings endpoint began serving
anti-bot HTML for UCITS and US lines alike.

``scripts/prove_ws6_weight_parity.py`` proves the two routes agree on real
snapshots, but it needs the network to do it — the two caches on disk are
disjoint, so the overlap has to be fetched. These tests are the offline half:
they pin the filtering invariant that makes that parity possible at all, on
synthetic payloads, so a later edit to either parser fails here rather than
silently restating 4,836 published snapshots.

Offline and synthetic throughout.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from fetch_constituents import PayloadContractError, parse_holdings  # noqa: E402
from fetch_ws6_weights import (  # noqa: E402
    CSV_WEIGHT_DECIMALS,
    _round_like_csv,
    parse_holdings_weights,
    parse_holdings_weights_json,
)

TARGET = date(2026, 9, 4)


def _json(rows, as_of="20260904", drop=()):
    """Build a product-data payload from (ticker, assetClass, pct) rows."""
    dps = {
        "asOfDate": {"value": as_of},
        "ticker": {"value": [r[0] for r in rows]},
        "assetClass": {"value": [r[1] for r in rows]},
        "holdingPercent": {"value": [r[2] for r in rows]},
    }
    for k in drop:
        dps.pop(k, None)
    return {"componentsByNameMap": {"holdings": {"containersByNameMap": {
        "all": {"dataPointsByNameMap": dps}}}}}


def _csv(rows):
    """The same rows as an iShares holdings CSV body."""
    head = 'Ticker,Name,Asset Class,Weight (%)\n'
    body = "".join(
        f'{t},{t} Inc,{c},{"" if p is None else p}\n' for t, c, p in rows)
    return f'Fund Holdings as of,"04-Sep-2026"\n\n{head}{body}\n'


# --- the invariant that makes parity possible ------------------------------

def test_both_routes_agree_on_an_equivalent_payload():
    """The whole basis of the substitution, driven offline.

    Same holdings expressed as CSV and as JSON must give the same keys and the
    same weights. The CSV publishes 2 dp and the API 4-5, so the JSON route
    stores at the CSV's precision; that is what makes this equality exact.
    """
    rows_json = [("AVGO", "Equity", 8.6683), ("AMD", "Equity", 8.65915),
                 ("NVDA", "Equity", 8.01594), ("BRK.B", "Equity", 4.08698)]
    rows_csv = [("AVGO", "Equity", 8.67), ("AMD", "Equity", 8.66),
                ("NVDA", "Equity", 8.02), ("BRK.B", "Equity", 4.09)]

    jw, jno = parse_holdings_weights_json(_json(rows_json), TARGET)
    cw, cno = parse_holdings_weights(_csv(rows_csv))

    assert jw == cw, "the two routes disagree on the weight table"
    assert jno == cno == []
    assert jw["BRK-B"] == 4.09, "dot -> dash normalisation must match the CSV"


def test_json_keys_join_the_membership_parser():
    """The keys must equal what the membership parser yields on the same body.

    That join is what lets the weight table address the snapshots; a mismatch
    breaks the basket rather than merely moving a number.
    """
    rows = [("AVGO", "Equity", 50.0), ("CASH_USD", "Cash", 10.0),
            ("-", "Equity", 5.0), ("BRK.B", "Equity", 40.0)]
    jw, jno = parse_holdings_weights_json(_json(rows), TARGET)
    membership = parse_holdings(_csv([(t, c, p) for t, c, p in rows]))
    assert sorted(list(jw) + jno) == sorted(membership)


# --- the asOfDate echo, which is load-bearing ------------------------------

def test_unechoed_asofdate_yields_no_weights():
    """For a weekend, holiday or pre-inception date the API silently returns
    the LATEST holdings. Accepting that would write today's weights onto a
    historical Friday — a look-ahead defect in a point-in-time register."""
    payload = _json([("AVGO", "Equity", 100.0)], as_of="20260911")
    assert parse_holdings_weights_json(payload, TARGET) == ({}, [])


def test_echoed_asofdate_is_accepted():
    payload = _json([("AVGO", "Equity", 100.0)], as_of="20260904")
    w, _ = parse_holdings_weights_json(payload, TARGET)
    assert w == {"AVGO": 100.0}


# --- filtering, row for row against the CSV parser -------------------------

def test_non_equity_rows_are_dropped():
    rows = [("AVGO", "Equity", 60.0), ("USD", "Cash", 40.0)]
    w, _ = parse_holdings_weights_json(_json(rows), TARGET)
    assert w == {"AVGO": 60.0}


def test_placeholder_ticker_is_skipped():
    rows = [("-", "Equity", 5.0), ("AVGO", "Equity", 95.0)]
    w, _ = parse_holdings_weights_json(_json(rows), TARGET)
    assert w == {"AVGO": 95.0}


def test_first_occurrence_wins_on_a_duplicate_ticker():
    rows = [("AVGO", "Equity", 60.0), ("AVGO", "Equity", 1.0)]
    w, _ = parse_holdings_weights_json(_json(rows), TARGET)
    assert w == {"AVGO": 60.0}


def test_ticker_overrides_are_applied():
    rows = [("OLD", "Equity", 100.0)]
    w, _ = parse_holdings_weights_json(_json(rows), TARGET,
                                       ticker_overrides={"OLD": "NEW"})
    assert w == {"NEW": 100.0}


def test_override_to_none_drops_the_row():
    rows = [("JUNK", "Equity", 100.0)]
    w, no = parse_holdings_weights_json(_json(rows), TARGET,
                                        ticker_overrides={"JUNK": None})
    assert w == {} and no == []


def test_null_weight_becomes_a_member_without_weight():
    """A name keeps its membership and contributes no weight — same semantics
    as an empty Weight (%) cell on the CSV route, which the caller reports."""
    rows = [("AVGO", "Equity", 100.0), ("NOPCT", "Equity", None)]
    w, no = parse_holdings_weights_json(_json(rows), TARGET)
    assert w == {"AVGO": 100.0}
    assert no == ["NOPCT"]


def test_absent_weight_column_leaves_every_member_without_weight():
    rows = [("AVGO", "Equity", 1.0), ("AMD", "Equity", 2.0)]
    payload = _json(rows, drop=("holdingPercent",))
    w, no = parse_holdings_weights_json(payload, TARGET)
    assert w == {}
    assert sorted(no) == ["AMD", "AVGO"]


# --- precision, the one deliberate choice ----------------------------------

def test_weights_are_stored_at_the_csv_published_precision():
    """4,836 snapshots are already on the 2 dp basis. A table that changed
    precision part-way through would put a step into exactly the
    basis-point-scale divergence series WS6b exists to measure."""
    assert CSV_WEIGHT_DECIMALS == 2
    w, _ = parse_holdings_weights_json(
        _json([("AVGO", "Equity", 8.66829999)]), TARGET)
    assert w == {"AVGO": 8.67}


def test_exact_halves_round_HALF_UP_like_the_publisher():
    """The whole of the first parity run's 9 disagreements.

    Python rounds half to EVEN, the publisher rounds half UP, so an exact
    half-way weight goes opposite ways: holdingPercent 0.435 is 0.43 under
    round() and 0.44 in the published CSV. Measured on IUES 2018-02-02 (NFX),
    and on 8 further snapshots across IUHC, IUUS, IUMS and IUSP — every failure
    the proof reported, each exactly one unit in the last decimal place.
    """
    assert _round_like_csv(0.435) == 0.44      # round() would give 0.43
    assert _round_like_csv(0.425) == 0.43      # round() would give 0.42
    assert _round_like_csv(2.675) == 2.68

    w, _ = parse_holdings_weights_json(_json([("NFX", "Equity", 0.435)]), TARGET)
    assert w == {"NFX": 0.44}, "an exact half must round the publisher's way"


def test_rounding_reads_the_published_decimal_not_the_binary_float():
    """Decimal(repr(x)), never Decimal(x).

    The float nearest 0.435 is 0.434999..., so quantising the binary value
    rounds DOWN and reintroduces the defect this helper exists to remove.
    """
    from decimal import ROUND_HALF_UP, Decimal
    naive = float(Decimal(0.435).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    assert naive == 0.43                       # the trap
    assert _round_like_csv(0.435) == 0.44      # the helper avoids it


# --- contract failures must raise, not return quietly ----------------------

def test_column_length_mismatch_raises():
    payload = _json([("AVGO", "Equity", 1.0)])
    dps = payload["componentsByNameMap"]["holdings"]["containersByNameMap"][
        "all"]["dataPointsByNameMap"]
    dps["holdingPercent"]["value"] = [1.0, 2.0]        # one row too many
    with pytest.raises(PayloadContractError):
        parse_holdings_weights_json(payload, TARGET)


def test_missing_holdings_path_raises():
    with pytest.raises(PayloadContractError):
        parse_holdings_weights_json({"componentsByNameMap": {}}, TARGET)


def test_exchange_suffix_lines_are_refused_on_both_routes():
    """Every WS6 single-named line holds US constituents. A line configured
    otherwise must refuse rather than diverge from the production resolver —
    and the JSON route must refuse on the same terms as the CSV one."""
    with pytest.raises(ValueError):
        parse_holdings_weights_json(_json([("X", "Equity", 1.0)]), TARGET,
                                    apply_exchange_suffix=True)
    with pytest.raises(ValueError):
        parse_holdings_weights("", apply_exchange_suffix=True)
