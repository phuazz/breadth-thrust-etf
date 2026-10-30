"""Combined-book risk view: capital weight, estimated risk contribution and
cross-sleeve overlap for the MODEL-HELD book. Reporting only.

WHAT THIS IS. A description of the book the dashboard already holds on paper
(``data/component_held_basis.json``, validated by ``component_basis.validate``),
not of broker-confirmed positions and not of the proposed next-fill targets in
``data/live_targets.json``. Nothing here feeds a selector, rank, sleeve weight,
gate, floor, cadence, order or overlay setting: the only output is
``data/combined_book_risk.json``, which the dashboard build injects into the
Monitor tab after ``validate_snapshot`` has checked it against the current
files. No engine imports this module.

REPORTING CONVENTION (not an investment rule; owner rulings of 2026-10-28 on
handoffs/2026-10-28_combined-book-risk-view-review.md). Sample covariance
(ddof=1) of COMMON-CLOSE simple USD total returns: returns between consecutive
dates on which EVERY held instrument has a close, inside the 252-close window
that ``holdings_prices_1y.json`` exports, annualised x252. A venue holiday
lengthens the interval for every line rather than being filled, so x252 is a
convention, not the observed interval rate. Euler decomposition on the merged
instrument weights:

    variance           = w' S w
    vol contribution_i = w_i (S w)_i / vol
    share_i            = w_i (S w)_i / variance   (undefined when variance = 0)

Sleeve contributions sum each sleeve-line weight against the SAME combined
(S w) vector. Negative contributions are kept. One fixed, report-only
sensitivity accompanies it: the last common close of each week, annualised x52,
compared with the common-close estimate over the identical endpoint dates, the
same book, prices and FX. Neither is ground truth and neither is selected.

PRICE AND FX BASIS. ``holdings_prices_1y.json`` holds adjusted closes in native
currency (4 significant figures), keyed by the registry trading proxy. Sleeve D
lines are EUR and are converted with the Sleeve D engine's own EUR/USD
(yfinance ``EURUSD=X`` through ``run_europe_rotation._fx_convert_eur_to_usd``),
read from a local file only: ``data/combined_book_risk_fx_eurusd.json``, which
an operator writes with ``export-engine-fx`` (a vendor fetch, never run by the
build). The export carries structured provenance (vendor, ticker, converter,
aligned Xetra line, export time); it is validated field by field, the label is
derived from it, and it is fingerprinted. ``data/ws1_fx_eurusd_cache.parquet``
is examined and reported but never used: it carries only pandas index
metadata, so the file itself cannot establish its source or converter. A
series that does not reach the cutoff within the engines' 10-day cap makes the
risk estimate unavailable; it is never forward-filled and no other FX source
is substituted.

FAIL CLOSED. Missing, malformed, non-finite, non-positive, duplicated,
unordered, gapped or stale prices or FX, an invalid held basis, an unverified
reconciliation or too few observations make the risk estimate UNAVAILABLE with
reasons. Capital weights are still reported when the held basis validates.
Missing risk is never zero-filled and weights are never renormalised.

SNAPSHOT VALIDITY. The artefact fingerprints every observation it used (one
digest per date) and the inputs it reconciled. A later check re-verifies every
used observation still present in the export. Observations dated before the
export's current first date have rolled out of the 252-close window: their
values can no longer be re-verified, and a deletion at the very front of a
series is indistinguishable from such a roll. A roll is accepted only when no
more observations have left the front than have been appended after the
cutoff SINCE the snapshot (each fingerprint records the post-cutoff count it
saw; the export is a fixed-length trailing window); it is then reported as a
note. The artefact contract also requires the evidence to cover the recorded
estimate: every price fingerprint is its instrument's whole recorded series,
their common dates are the recorded common closes, and the FX fingerprint
covers every EUR close. The held lines, sleeves, proxy instruments, FX
handling and overlap lists must all be derivable from the saved canonical held
book (hashed to ``held_basis_sha256``) and the saved resolver mapping, which
the check re-derives from the project resolvers.

RUN OWNERSHIP. A run holds an operating-system lock on ``<artefact>.lock`` for
its whole life (released by the OS if the process dies) and creates a
``.pending`` marker before calculating; it removes the marker only after its
own run has atomically replaced the artefact, so a failed, overlapping or
interrupted run leaves no earlier artefact eligible for display. A marker left
by a run that ended without completing is cleared only by
``recover --run-id <id>``, which is refused while the lock is held and which
replaces the artefact with a failure record before removing the marker.

Python datetime months are 1-indexed (January = 1); pandas Timestamps and
Periods follow the same convention.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "combined_book_risk.json"
ENGINE_FX_EXPORT = "combined_book_risk_fx_eurusd.json"
ENGINE_FX_CACHE = "ws1_fx_eurusd_cache.parquet"
sys.path.insert(0, str(ROOT / "scripts"))

# v3: per-observation fingerprints, structured FX provenance, run id.
# v4: each series fingerprint records its post-cutoff count at snapshot time,
# and evidence coverage is cross-checked against the recorded window.
# v5: the fingerprint carries the canonical held book (hashed to
# held_basis_sha256) and the resolver-derived proxy/FX mapping; instruments,
# sleeves, child lines and overlaps must be derivable from the two. Earlier
# artefacts fail validation and must be rebuilt.
SCHEMA = "combined_book_risk/v5"
# Any edit to this file changes the calculation version, which invalidates
# every saved snapshot until it is recomputed.
CALC_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
ANNUALISATION_DAILY = 252
ANNUALISATION_WEEKLY = 52
# Minimum common-close returns. The export window is 252 closes, so a complete
# panel carries ~245-251 returns once venue holidays are intersected.
MIN_RETURNS = 200
MIN_WEEKLY_RETURNS = 26
# Same cap the Sleeve C/D engines and mark_to_market_live use on FX.
FX_MAX_STALE_DAYS = 10
# Display convention for the correlated-exposure list; listed, never summed.
CORR_DISPLAY_THRESHOLD = 0.80
# Mirrors component_basis.MODEL_WEIGHT_ROUNDING_TOL.
WEIGHT_TOL = 1e-4
SLEEVE_ORDER = ["A", "B", "C", "D", "TILT", "GATE"]
SLEEVE_LABEL = {
    "A": "A · US sector breadth", "B": "B · asset-class momentum",
    "C": "C · thematic momentum", "D": "D · Europe sector breadth",
    "TILT": "EM tilt", "GATE": "De-risk gate reserve",
}
FX_EXPORT_SCHEMA = "combined_book_risk_fx/v1"
# The only provenance accepted for the primary EUR/USD; every field must match.
ENGINE_FX_PROVENANCE = {"vendor": "yfinance", "ticker": "EURUSD=X",
                        "converter": "run_europe_rotation._fx_convert_eur_to_usd"}
FX_EXPORT_KEYS = {"schema", "provenance", "dates", "close"}
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX16 = re.compile(r"[0-9a-f]{16}")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Identifiable file/format failures of the parquet reader: the file cannot be
# opened (OSError, which pyarrow's ArrowIOError aliases) or pyarrow rejects its
# bytes (ArrowInvalid: bad magic, truncated footer). Nothing else in the
# ArrowException hierarchy is a data problem: ArrowNotImplementedError and the
# rest, like a bare ValueError, propagate as a failed run. Schema, index and
# value problems are checked separately after a successful read.
import pyarrow as _pa
PARQUET_ERRORS: tuple = (OSError, _pa.ArrowInvalid)

# Files whose bytes this module must never change; the invariance test hashes
# them before and after a run.
ENGINE_FILES = [
    "component_held_basis.json", "live_targets.json", "live_track.json",
    "overlay_decision.json", "multi_strategy.json", "holdings_prices_1y.json",
    ENGINE_FX_CACHE,
]


class InputError(Exception):
    """An expected data problem: reported as a reason, never raised past build."""


# --------------------------------------------------------------------------
# Strict I/O helpers
# --------------------------------------------------------------------------

def _reject_constant(token: str):
    raise InputError(f"non-standard JSON constant {token}")


class _DuplicateKey(Exception):
    pass


def _unique_pairs(pairs: list) -> dict:
    """object_pairs_hook: a repeated key at any depth is rejected, never
    resolved silently to its last value."""
    seen: set = set()
    for k, _ in pairs:
        if k in seen:
            raise _DuplicateKey(k)
        seen.add(k)
    return dict(pairs)


def _loads_strict(text: str, name: str):
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
    except json.JSONDecodeError as exc:
        raise InputError(f"{name} is not valid JSON ({exc.msg}, line {exc.lineno})") from None
    except _DuplicateKey as exc:
        raise InputError(f"{name}: duplicate key {exc.args[0]!r}") from None


def read_json(path: Path):
    if not path.exists():
        raise InputError(f"{path.name} is missing")
    return _loads_strict(path.read_text(encoding="utf-8"), path.name)


def read_object(path: Path) -> dict:
    """A JSON file whose top level must be an object."""
    obj = read_json(path)
    if not isinstance(obj, dict):
        raise InputError(f"{path.name}: top level is a {type(obj).__name__}, expected an object")
    return obj


def need_str(x, what: str) -> str:
    if not isinstance(x, str) or not x:
        raise InputError(f"{what} is missing or not a string")
    return x


def need_number(x, what: str) -> float:
    """A finite JSON number (booleans are not numbers here)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise InputError(f"{what} is not a number ({x!r})")
    v = float(x)
    if not math.isfinite(v):
        raise InputError(f"{what} is non-finite")
    return v


def canonical_sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def parse_iso_date(x) -> date | None:
    """A date written exactly as YYYY-MM-DD and valid on the calendar, else None."""
    if not isinstance(x, str) or not ISO_DATE.fullmatch(x):
        return None
    try:
        return date.fromisoformat(x)
    except ValueError:
        return None


def validated_series(dates, values, name: str) -> pd.Series:
    """Finite, positive values on strictly increasing (so unique) ISO dates.
    Dates must be exact YYYY-MM-DD strings and values plain numbers: nothing is
    truncated, parsed from a string, unwrapped or coerced from a boolean."""
    if not isinstance(dates, list) or not isinstance(values, list) or len(dates) != len(values):
        raise InputError(f"{name}: dates and values are missing or of unequal length")
    if len(dates) < 2:
        raise InputError(f"{name}: fewer than two observations")
    parsed = [parse_iso_date(d) for d in dates]
    bad = next((d for d, p in zip(dates, parsed) if p is None), None)
    if any(p is None for p in parsed):
        raise InputError(f"{name}: unparseable date {bad!r} (expected YYYY-MM-DD)")
    if any(b <= a for a, b in zip(parsed, parsed[1:])):
        raise InputError(f"{name}: dates are duplicated or out of order")
    bad_v = next((v for v in values if isinstance(v, bool) or not isinstance(v, (int, float))), None)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        raise InputError(f"{name}: non-numeric values (for example {bad_v!r})")
    arr = np.array([float(v) for v in values], dtype=float)
    if not np.isfinite(arr).all():
        raise InputError(f"{name}: non-finite values")
    if (arr <= 0).any():
        raise InputError(f"{name}: non-positive values")
    return pd.Series(arr, index=pd.DatetimeIndex(pd.to_datetime(parsed)), name=name)


def observation_digests(series: pd.Series) -> dict[str, str]:
    """{ISO date: digest of "date=value"} for every observation used. One
    digest per observation lets a later check re-verify each retained value
    individually after the front of the export window has rolled; the values
    themselves are not stored."""
    return {f"{d:%Y-%m-%d}": hashlib.sha256(f"{d:%Y-%m-%d}={float(v)!r}".encode()).hexdigest()[:16]
            for d, v in series.items()}


def series_fingerprint(series: pd.Series, after_cutoff: int) -> dict:
    """``after_cutoff`` is the number of export observations dated after the
    cutoff when the snapshot was taken, so that a later check counts only
    observations appended since then as licensing a front roll."""
    obs = observation_digests(series)
    return {"first_used": f"{series.index[0]:%Y-%m-%d}", "last_used": f"{series.index[-1]:%Y-%m-%d}",
            "n": len(obs), "observations": obs, "sha256": canonical_sha(obs),
            "after_cutoff": int(after_cutoff)}


# --------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------

def annualised_covariance(returns: np.ndarray, periods: int = ANNUALISATION_DAILY) -> np.ndarray:
    """Sample covariance (ddof=1) of a T x N return matrix, times ``periods``."""
    returns = np.asarray(returns, dtype=float)
    if returns.ndim != 2 or returns.shape[0] < 2:
        raise ValueError("need a T x N return matrix with T >= 2")
    if not np.isfinite(returns).all():
        raise ValueError("return matrix contains non-finite values")
    return np.atleast_2d(np.cov(returns, rowvar=False, ddof=1)) * periods


def decompose(weights: np.ndarray, cov: np.ndarray) -> dict:
    """Euler decomposition. Shares are None (undefined) when variance is zero."""
    w = np.asarray(weights, dtype=float)
    cov = np.asarray(cov, dtype=float)
    sigma_w = cov @ w
    variance = float(w @ sigma_w)
    if -1e-15 < variance < 0:
        variance = 0.0
    if variance < 0:
        raise ValueError("covariance is not positive semi-definite for these weights")
    vol = math.sqrt(variance)
    raw = w * sigma_w
    if variance == 0.0:
        return {"variance": 0.0, "vol": 0.0, "sigma_w": sigma_w,
                "share": [None] * len(w), "vol_contrib": [None] * len(w)}
    return {"variance": variance, "vol": vol, "sigma_w": sigma_w,
            "share": [float(x) for x in raw / variance],
            "vol_contrib": [float(x) for x in raw / vol]}


def line_contributions(lines: list[dict], proxies: list[str], sigma_w: np.ndarray,
                       variance: float, vol: float) -> list[dict]:
    """Per sleeve-line contribution against the combined (S w) vector."""
    pos = {p: i for i, p in enumerate(proxies)}
    out = []
    for ln in lines:
        raw = ln["held"] * float(sigma_w[pos[ln["proxy"]]])
        out.append({**ln,
                    "share": (raw / variance) if variance > 0 else None,
                    "vol_contrib": (raw / vol) if vol > 0 else None})
    return out


def sleeve_totals(contrib: list[dict], key: str) -> dict[str, float | None]:
    """Sum by sleeve; a sleeve with any undefined line stays undefined."""
    out: dict[str, float | None] = {}
    for c in contrib:
        s = c["sleeve"]
        if c[key] is None or (s in out and out[s] is None):
            out[s] = None
        else:
            out[s] = out.get(s, 0.0) + c[key]
    return out


def correlation(cov: np.ndarray) -> np.ndarray:
    """Correlation matrix with NaN where either variance is zero (undefined)."""
    vols = np.sqrt(np.clip(np.diag(cov), 0, None))
    denom = np.outer(vols, vols)
    out = np.full_like(cov, np.nan)
    np.divide(cov, denom, out=out, where=denom > 0)
    return out


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def instrument_for(etf: str) -> tuple[str, str]:
    """(return-proxy symbol, fx handling) from the project's own resolvers.
    A disagreement between the two resolvers is an InputError."""
    from export_holdings_prices import resolve_book_symbol
    from mark_to_market_live import _load_registry, _resolve_yf_symbol
    sym, fx = _resolve_yf_symbol(etf, _load_registry())
    alt = resolve_book_symbol(etf)
    if sym != alt:
        raise InputError(f"{etf}: price resolvers disagree ({sym} vs {alt})")
    return sym, fx


def held_canonical(basis: dict) -> dict:
    return {"anchor": basis["anchor"], "model_as_of": basis["model_as_of"],
            "lines": sorted([r["sleeve"], r["etf"], float(r["held"])] for r in basis["lines"])}


def live_targets_held(data_dir: Path, anchor: str) -> tuple[dict | None, str | None]:
    """(held lines keyed by (sleeve, etf), sha) when live_targets describes the
    same anchor; (None, None) when it describes another one. The WHOLE held
    collection is validated before it is keyed: a duplicate (sleeve, etf), a
    negative, non-numeric or non-finite amount or a malformed record is an
    InputError, never overwritten or dropped. A zero amount means not held, as
    in the held basis, and is left out of the comparison on both sides.
    Target amounts are never read."""
    lt = read_object(data_dir / "live_targets.json")
    as_of = need_str(lt.get("as_of"), "live_targets.json as_of")
    if as_of != anchor:
        return None, None
    rows = lt.get("lines")
    if not isinstance(rows, list):
        raise InputError("live_targets.json lines is not a list")
    held: dict = {}
    seen: set = set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise InputError(f"live_targets line {i} is not an object")
        key = (need_str(r.get("sleeve"), f"live_targets line {i} sleeve"),
               need_str(r.get("etf"), f"live_targets line {i} etf"))
        if key in seen:
            raise InputError(f"live_targets has a duplicate held line for {key[0]} {key[1]}")
        seen.add(key)
        v = need_number(r.get("held"), f"live_targets line {i} ({key[0]} {key[1]}) held")
        if v < 0:
            raise InputError(f"live_targets line {i} ({key[0]} {key[1]}) has negative held {v}")
        if v > 0:
            held[key] = v
    return held, canonical_sha(sorted([k[0], k[1], v] for k, v in held.items()))


def live_track_canonical(data_dir: Path) -> dict:
    lt = read_object(data_dir / "live_track.json")
    anchor = need_str(lt.get("anchor_date"), "live_track.json anchor_date")
    eff = lt.get("effective_weights")
    if not isinstance(eff, dict):
        raise InputError("live_track.json effective_weights is missing or not an object")
    weights = {need_str(k, "live_track effective weight key"):
               need_number(v, f"live_track effective weight {k}") for k, v in sorted(eff.items())}
    # Long-only NAV fractions. Checked per amount, before reconciliation, so a
    # malformed amount is never legitimised by the reconciliation tolerance.
    for k, v in weights.items():
        if not 0.0 <= v <= 1.0:
            raise InputError(f"live_track effective weight {k} is {v}, outside [0, 1]")
    return {"anchor_date": anchor, "effective_weights": weights}


def _check_basis_shape(basis: dict) -> None:
    """Types the held-basis records before the strategy validator sees them."""
    need_str(basis.get("anchor"), "component_held_basis.json anchor")
    need_str(basis.get("model_as_of"), "component_held_basis.json model_as_of")
    rows = basis.get("lines")
    if not isinstance(rows, list):
        raise InputError("component_held_basis.json lines is not a list")
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise InputError(f"held basis line {i} is not an object")
        need_str(r.get("sleeve"), f"held basis line {i} sleeve")
        need_str(r.get("etf"), f"held basis line {i} etf")
        need_number(r.get("held"), f"held basis line {i} held")


def load_held_book(data_dir: Path, today: date) -> tuple[dict | None, list[str]]:
    """The validated model-held book, or (None, reasons). Never the targets."""
    from component_basis import validate
    try:
        basis = read_object(data_dir / "component_held_basis.json")
        _check_basis_shape(basis)
        validate(basis, basis.get("anchor"))
    except InputError as exc:
        return None, [str(exc)]
    except (ValueError, KeyError, TypeError) as exc:
        return None, [f"held basis failed validation: {exc}"]
    try:
        anchor_day = date.fromisoformat(basis["anchor"])
    except ValueError:
        return None, [f"held basis anchor {basis['anchor']!r} is not an ISO date"]
    if anchor_day > today:
        return None, [f"held basis anchor {basis['anchor']} is after today {today}"]
    return basis, []


def reconcile(lines: list[dict], data_dir: Path, anchor: str) -> tuple[dict, list[str]]:
    """Same-basis reconciliation. Only status "pass" permits a risk estimate."""
    reasons: list[str] = []
    out: dict = {"tolerance": WEIGHT_TOL}
    try:
        lt_held, lt_sha = live_targets_held(data_dir, anchor)
        b_held = {(ln["sleeve"], ln["etf"]): ln["held"] for ln in lines}
        out["live_targets_held"] = ("not applicable (other as-of)" if lt_held is None
                                    else ("pass" if lt_held == b_held else "fail"))
        out["live_targets_held_sha256"] = lt_sha
        if out["live_targets_held"] == "fail":
            reasons.append("live_targets held lines disagree with the held basis")
    except InputError as exc:
        out["live_targets_held"] = "not checked"
        reasons.append(f"live_targets cross-check not possible: {exc}")
    try:
        track = live_track_canonical(data_dir)
        out["live_track_sha256"] = canonical_sha(track)
        if track["anchor_date"] != anchor:
            out["status"] = "not checked"
            out["detail"] = f"live_track anchor {track['anchor_date']} is not {anchor}"
        else:
            agg: dict[str, float] = {}
            for ln in lines:
                agg[ln["etf"]] = agg.get(ln["etf"], 0.0) + ln["held"]
            eff = track["effective_weights"]
            diff = max((abs(agg.get(k, 0.0) - eff.get(k, 0.0)) for k in set(agg) | set(eff)),
                       default=0.0)
            out.update({"status": "pass" if diff <= WEIGHT_TOL else "fail",
                        "max_abs_diff": diff,
                        "detail": "held lines aggregated by ETF vs live_track.effective_weights"})
    except InputError as exc:
        out.update({"status": "not checked", "detail": str(exc), "live_track_sha256": None})
    if out["status"] == "fail":
        reasons.append("held lines do not reconcile to live_track effective weights "
                       f"(max difference {out['max_abs_diff']:.6f} > {WEIGHT_TOL})")
    elif out["status"] != "pass":
        reasons.append(f"reconciliation not verified: {out['detail']}")
    return out, reasons


def expected_last_session(symbol: str, cutoff: date) -> date | None:
    """The venue's last session on or before the cutoff (None if unknown).

    Read from the exporter's calendar (export_holdings_prices._venue_calendar),
    which for Shanghai/Shenzhen is the effective calendar less the validated
    pinned closures, so this and sleeve C's window check agree on the days
    Shenzhen traded (2026-10-30). A pin conflict or calendar fault is
    reported by load_prices through calendar_problem.
    """
    from export_holdings_prices import _venue_calendar, venue_calendar_for
    name = venue_calendar_for(symbol)
    if not name:
        return None
    sched = _venue_calendar(name).schedule(
        start_date=(cutoff - timedelta(days=14)).isoformat(), end_date=cutoff.isoformat())
    return sched.index[-1].date() if len(sched) else None


def load_prices(prices: dict, symbols: list[str], cutoff: date
                ) -> tuple[dict[str, pd.Series], list[str], dict]:
    """Native-currency closes on or before the cutoff, with problems listed.
    A calendar that cannot establish the expected sessions (a pin conflict or
    a fault) is a problem, never a pass."""
    from export_holdings_prices import interior_gap_check
    out, reasons, meta = {}, [], {}
    for sym in sorted(set(symbols)):
        entry = prices.get(sym)
        if not isinstance(entry, dict):
            reasons.append(f"{sym}: no price series in holdings_prices_1y.json")
            continue
        try:
            s = validated_series(entry.get("dates"), entry.get("prices"), sym)
        except InputError as exc:
            reasons.append(str(exc))
            continue
        after = int((s.index.date > cutoff).sum())
        s = s[s.index.date <= cutoff]
        if len(s) < 2:
            reasons.append(f"{sym}: fewer than two closes on or before {cutoff}")
            continue
        gaps, problem = interior_gap_check(
            {"dates": [d.strftime("%Y-%m-%d") for d in s.index]}, sym)
        if gaps:
            reasons.append(f"{sym}: missing expected sessions {', '.join(gaps)}")
        if problem:
            reasons.append(f"{sym}: expected sessions not established: {problem}")
        try:
            exp_last = expected_last_session(sym, cutoff)
        except Exception as exc:  # noqa: BLE001 — a calendar fault is a reason
            reasons.append(f"{sym}: last expected session not established ({exc})")
            exp_last = None
        last = s.index[-1].date()
        if exp_last is not None and last != exp_last:
            reasons.append(f"{sym}: last close {last}, expected {exp_last}")
        meta[sym] = {"first": str(s.index[0].date()), "last": str(last),
                     "closes": int(len(s)), "after_cutoff": after}
        out[sym] = s
    return out, reasons, meta


def read_prices(data_dir: Path) -> tuple[dict, str | None]:
    """(symbol -> entry, export timestamp) from holdings_prices_1y.json, typed."""
    blob = read_object(data_dir / "holdings_prices_1y.json")
    prices = blob.get("prices")
    if not isinstance(prices, dict):
        raise InputError("holdings_prices_1y.json prices is missing or not an object")
    stamp = blob.get("computed_at_utc")
    if stamp is not None and not isinstance(stamp, str):
        raise InputError("holdings_prices_1y.json computed_at_utc is not a string")
    return prices, stamp


def fx_sources(data_dir: Path) -> list[dict]:
    """EUR/USD candidates in order. Only the operator export can establish
    provenance; the legacy engine cache is read for diagnosis only."""
    return [{"id": "engine-export", "path": data_dir / ENGINE_FX_EXPORT, "kind": "export"},
            {"id": "engine-ws1-cache", "path": data_dir / ENGINE_FX_CACHE, "kind": "legacy-cache"}]


def fx_provenance(raw: dict, name: str) -> dict:
    """Validated provenance of an operator export. Every ENGINE_FX_PROVENANCE
    field must match exactly; a missing, conflicting or unexpected field is an
    InputError, so a series is never accepted for its filename alone."""
    extra = set(raw) - FX_EXPORT_KEYS
    if extra:
        raise InputError(f"{name}: unexpected field(s) {', '.join(sorted(extra))}; "
                         f"provenance must be the structured {FX_EXPORT_SCHEMA} record")
    if raw.get("schema") != FX_EXPORT_SCHEMA:
        raise InputError(f"{name}: schema {raw.get('schema')!r} is not {FX_EXPORT_SCHEMA}")
    prov = raw.get("provenance")
    if not isinstance(prov, dict):
        raise InputError(f"{name}: provenance is missing or not an object")
    for k, want in ENGINE_FX_PROVENANCE.items():
        if prov.get(k) != want:
            raise InputError(f"{name}: provenance {k} is {prov.get(k)!r}, expected {want!r}")
    aligned = prov.get("aligned_to")
    if not isinstance(aligned, str) or not aligned.endswith(".DE"):
        raise InputError(f"{name}: provenance aligned_to {aligned!r} is not a Xetra line")
    exported = prov.get("exported_at_utc")
    try:
        datetime.fromisoformat(str(exported).replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f"{name}: provenance exported_at_utc {exported!r} is not a timestamp") from None
    if set(prov) - set(ENGINE_FX_PROVENANCE) - {"aligned_to", "exported_at_utc"}:
        raise InputError(f"{name}: provenance carries unexpected fields")
    return {**ENGINE_FX_PROVENANCE, "aligned_to": aligned, "exported_at_utc": str(exported)}


def fx_label(prov: dict) -> str:
    return (f"{prov['vendor']} {prov['ticker']} through {prov['converter']}; operator export "
            f"aligned to {prov['aligned_to']}, exported {prov['exported_at_utc']}")


def fx_provenance_sha(prov: dict) -> str:
    """Fingerprinted provenance. The export time is excluded so that a
    re-export that only appends after the cutoff does not invalidate."""
    return canonical_sha({k: prov[k] for k in (*ENGINE_FX_PROVENANCE, "aligned_to")})


def read_fx(src: dict) -> tuple[pd.Series, dict | None]:
    """(series, validated provenance or None when none can be established)."""
    path = src["path"]
    if not path.exists():
        raise InputError(f"{path.name} is missing")
    if path.suffix == ".parquet":
        try:
            frame = pd.read_parquet(path)
        except PARQUET_ERRORS as exc:
            raise InputError(f"{path.name} is unreadable ({type(exc).__name__}: {exc})") from None
        if "EURUSD" not in frame.columns:
            raise InputError(f"{path.name} has no EURUSD column")
        col = frame["EURUSD"]
        if not isinstance(col.index, pd.DatetimeIndex):
            raise InputError(f"{path.name}: index is not dates ({type(col.index).__name__})")
        if pd.api.types.is_bool_dtype(col) or not pd.api.types.is_numeric_dtype(col):
            raise InputError(f"{path.name}: EURUSD has non-numeric values (dtype {col.dtype})")
        return validated_series([d.strftime("%Y-%m-%d") for d in col.index],
                                col.astype(float).tolist(), "EUR/USD"), None
    raw = read_object(path)
    prov = fx_provenance(raw, path.name)
    return validated_series(raw.get("dates"), raw.get("close"), "EUR/USD"), prov


def load_fx(cutoff: date, sources: list[dict]) -> tuple[pd.Series | None, dict, list[str]]:
    """First source with validated provenance that reaches the cutoff within
    the cap. Nothing is forward-filled to the cutoff and no other source is
    substituted."""
    tried = []
    for src in sources:
        try:
            s, prov = read_fx(src)
        except InputError as exc:
            tried.append({"id": src["id"], "problem": str(exc)})
            continue
        after = int((s.index.date > cutoff).sum())
        s = s[s.index.date <= cutoff]
        problems, last = [], None
        if s.empty:
            problems.append(f"no observation on or before {cutoff}")
        else:
            last = s.index[-1].date()
            age = (cutoff - last).days
            if age > FX_MAX_STALE_DAYS:
                problems.append(f"last observation {last} is {age} days before the "
                                f"cutoff {cutoff} (cap {FX_MAX_STALE_DAYS})")
        if prov is None:
            problems.append("provenance cannot be established: the file carries only pandas "
                            "index metadata, no source or converter record (diagnostic only)")
        if problems:
            tried.append({"id": src["id"], "last": str(last) if last else None,
                          "problem": "; ".join(problems)})
            continue
        return s, {"id": src["id"], "label": fx_label(prov), "provenance": prov,
                   "path": src["path"].name, "last": str(last), "after_cutoff": after,
                   "tried": tried}, []
    detail = "; ".join(f"{t['id']}: {t['problem']}" for t in tried)
    return None, {"id": None, "label": None, "tried": tried}, [
        f"engine-consistent EUR/USD does not cover {cutoff} ({detail}). Missing "
        f"dependency: {ENGINE_FX_EXPORT} exported through {cutoff} by "
        f"`python scripts/combined_book_risk.py export-engine-fx` (vendor fetch, operator-run)"]


def usd_panel(native: dict[str, pd.Series], fx_of: dict[str, str],
              eurusd: pd.Series | None) -> tuple[pd.DataFrame | None, list[str]]:
    """USD closes on the common date set. No forward-fill of prices."""
    from alignment import align_series_to_index
    reasons, usd = [], {}
    for sym, s in native.items():
        fx = fx_of[sym]
        if fx == "none":
            usd[sym] = s
        elif fx == "eur_to_usd":
            if eurusd is None:
                reasons.append(f"{sym}: engine-consistent EUR/USD unavailable")
                continue
            rate = align_series_to_index(eurusd, s.index, max_stale_days=FX_MAX_STALE_DAYS)
            if rate.isna().any():
                reasons.append(f"{sym}: EUR/USD missing or stale on {int(rate.isna().sum())} closes")
                continue
            usd[sym] = s * rate
        else:
            reasons.append(f"{sym}: unsupported FX handling {fx}")
    if reasons or not usd:
        return None, reasons or ["no instruments to price"]
    common = None
    for s in usd.values():
        common = s.index if common is None else common.intersection(s.index)
    return pd.DataFrame({k: v.reindex(common) for k, v in usd.items()}).sort_index(), []


# --------------------------------------------------------------------------
# Estimates
# --------------------------------------------------------------------------

def estimate(rets: pd.DataFrame, proxies: list[str], weights: np.ndarray,
             lines: list[dict], periods: int) -> dict:
    cov = annualised_covariance(rets[proxies].to_numpy(), periods)
    d = decompose(weights, cov)
    contrib = line_contributions(lines, proxies, d["sigma_w"], d["variance"], d["vol"])
    return {"cov": cov, "d": d, "sleeve_share": sleeve_totals(contrib, "share"),
            "sleeve_vol_contrib": sleeve_totals(contrib, "vol_contrib"),
            "n_returns": int(len(rets)),
            "first_return_date": str(rets.index[0].date()),
            "last_return_date": str(rets.index[-1].date())}


def weekly_sensitivity(panel: pd.DataFrame, proxies: list[str], weights: np.ndarray,
                       lines: list[dict]) -> dict:
    """Last common close of each week (Monday-Sunday weeks, pandas Period
    'W-SUN') annualised x52, against common-close returns over the identical
    endpoint dates annualised x252. Same book, prices and FX. Report only."""
    weekly = panel.groupby(panel.index.to_period("W-SUN")).tail(1)
    start, end = weekly.index[0], weekly.index[-1]
    out = {"basis": "fixed held book, same USD prices and FX, identical endpoints",
           "endpoint_start": str(start.date()), "endpoint_end": str(end.date()),
           "min_weekly_returns": MIN_WEEKLY_RETURNS}
    wret = weekly.pct_change().iloc[1:]
    dret = panel.loc[start:end].pct_change().iloc[1:]
    for key, rets, periods in (("common_close", dret, ANNUALISATION_DAILY),
                               ("weekly", wret, ANNUALISATION_WEEKLY)):
        if len(rets) < 2:
            out[key] = {"n_returns": int(len(rets)), "vol": None, "sleeve_share": None}
            continue
        e = estimate(rets, proxies, weights, lines, periods)
        out[key] = {"annualisation": periods, "n_returns": e["n_returns"],
                    "first_return_date": e["first_return_date"],
                    "last_return_date": e["last_return_date"],
                    "vol": e["d"]["vol"], "sleeve_share": e["sleeve_share"]}
    out["status"] = ("available" if out["weekly"]["n_returns"] >= MIN_WEEKLY_RETURNS
                     else "unavailable")
    return out


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(data_dir: Path = DATA_DIR, now: datetime | None = None,
          sources: list[dict] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    sources = fx_sources(data_dir) if sources is None else sources
    out: dict = {
        "schema": SCHEMA, "calc_sha256": CALC_SHA256, "run_status": "ok",
        "run_id": uuid.uuid4().hex,
        "computed_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "reporting_only": True,
        "basis": {"book": "model-held (component_held_basis.json); "
                          "not broker-confirmed, not next-fill targets",
                  "currency": "USD",
                  "estimator": "sample covariance (ddof=1) of common-close simple total "
                               "returns, annualised x252",
                  "window": "returns between consecutive common closes in the 252-close "
                            "export window; a venue holiday lengthens the interval",
                  "min_returns": MIN_RETURNS},
        "capital_status": "unavailable", "risk_status": "unavailable", "reasons": [],
        "fingerprint": {"calc_sha256": CALC_SHA256, "schema": SCHEMA},
        "sleeves": [], "instruments": [], "portfolio": None, "sensitivity": None,
        "overlaps": {"exact": [], "proxy": [], "correlated": [],
                     "corr_threshold": CORR_DISPLAY_THRESHOLD},
    }
    basis, reasons = load_held_book(data_dir, today)
    out["reasons"].extend(reasons)
    if basis is None:
        return out
    anchor = basis["anchor"]
    cutoff = date.fromisoformat(anchor)
    fp = out["fingerprint"]
    fp["held_book"] = held_canonical(basis)
    fp["held_basis_sha256"] = canonical_sha(fp["held_book"])
    fp["anchor"] = anchor
    out["basis"].update({"anchor": anchor, "model_as_of": basis["model_as_of"],
                         "price_cutoff": anchor,
                         "rounding_residual_nav": basis.get("rounding_residual_nav")})

    lines = []
    for r in basis["lines"]:
        held = float(r["held"])
        if held <= 0:
            continue
        try:
            proxy, fx = instrument_for(r["etf"])
        except InputError as exc:
            out["reasons"].append(str(exc))
            proxy, fx = None, None
        lines.append({"sleeve": r["sleeve"], "etf": r["etf"], "held": held,
                      "proxy": proxy, "fx": fx})
    # Resolver output per held ETF; validate_snapshot re-derives it.
    fp["mapping"] = {ln["etf"]: {"proxy": ln["proxy"], "fx": ln["fx"]} for ln in lines}
    total = sum(ln["held"] for ln in lines)
    by_sleeve: dict[str, float] = {}
    for ln in lines:
        by_sleeve[ln["sleeve"]] = by_sleeve.get(ln["sleeve"], 0.0) + ln["held"]
    out.update({"capital_status": "available", "capital_total": total,
                "residual_nav": 1.0 - total,
                "sleeves": [{"sleeve": k, "label": SLEEVE_LABEL.get(k, k),
                             "capital": by_sleeve[k], "share": None, "vol_contrib": None}
                            for k in SLEEVE_ORDER if k in by_sleeve]})

    rec, rec_reasons = reconcile(lines, data_dir, anchor)
    out["reconciliation"] = rec
    out["reasons"].extend(rec_reasons)
    fp["live_track_sha256"] = rec.get("live_track_sha256")
    fp["live_targets_held_sha256"] = rec.get("live_targets_held_sha256")

    priced = [ln for ln in lines if ln["proxy"]]
    proxies = sorted({ln["proxy"] for ln in priced})
    fx_of = {ln["proxy"]: ln["fx"] for ln in priced}
    native, p_meta = {}, {}
    try:
        prices_map, export_utc = read_prices(data_dir)
        out["basis"]["price_source"] = "holdings_prices_1y.json (adjusted closes, 4 s.f.)"
        out["basis"]["price_export_utc"] = export_utc
        native, p_reasons, p_meta = load_prices(prices_map, proxies, cutoff)
        out["reasons"].extend(p_reasons)
    except InputError as exc:
        out["reasons"].append(str(exc))
    fp["prices"] = {sym: series_fingerprint(s, p_meta[sym]["after_cutoff"]) for sym, s in native.items()}

    eurusd = None
    if any(fx_of.get(p) == "eur_to_usd" for p in proxies):
        eurusd, fx_meta, fx_reasons = load_fx(cutoff, sources)
        out["reasons"].extend(fx_reasons)
        out["basis"]["fx"] = fx_meta
        if eurusd is not None and native:
            first_needed = min(s.index[0] for s in native.values()) - timedelta(days=FX_MAX_STALE_DAYS)
            used = eurusd[eurusd.index >= first_needed]
            fp["fx"] = {"id": fx_meta["id"],
                        "provenance_sha256": fx_provenance_sha(fx_meta["provenance"]),
                        **series_fingerprint(used, fx_meta["after_cutoff"])}
    else:
        out["basis"]["fx"] = None

    out["instruments"] = [
        {"proxy": p, "fx": fx_of[p], "price": p_meta.get(p),
         "lines": [{"sleeve": ln["sleeve"], "etf": ln["etf"], "held": ln["held"]}
                   for ln in priced if ln["proxy"] == p],
         "weight": sum(ln["held"] for ln in priced if ln["proxy"] == p),
         "vol": None, "share": None, "vol_contrib": None}
        for p in proxies]

    corr = None
    if not out["reasons"]:
        panel, u_reasons = usd_panel(native, fx_of, eurusd)
        out["reasons"].extend(u_reasons)
        if panel is not None:
            rets = panel[proxies].pct_change().iloc[1:]
            out["basis"].update({
                "n_returns": int(len(rets)), "common_closes": int(len(panel)),
                "first_return_date": str(rets.index[0].date()) if len(rets) else None,
                "last_return_date": str(rets.index[-1].date()) if len(rets) else None,
                "extended_intervals": int(sum(
                    1 for a, b in zip(panel.index, panel.index[1:])
                    if any(((s.index > a) & (s.index < b)).any() for s in native.values())))})
            if len(rets) < MIN_RETURNS:
                out["reasons"].append(f"only {len(rets)} common-close returns (minimum {MIN_RETURNS})")
            elif str(panel.index[-1].date()) != anchor:
                out["reasons"].append(f"common panel ends {panel.index[-1].date()}, not {anchor}")
    out["basis"]["nav_covered"] = total if not out["reasons"] else 0.0

    if not out["reasons"]:
        w = np.array([i["weight"] for i in out["instruments"]])
        e = estimate(rets, proxies, w, priced, ANNUALISATION_DAILY)
        d = e["d"]
        vols = np.sqrt(np.diag(e["cov"]))
        corr = correlation(e["cov"])
        for i, inst in enumerate(out["instruments"]):
            inst.update({"vol": float(vols[i]), "share": d["share"][i],
                         "vol_contrib": d["vol_contrib"][i]})
        for s in out["sleeves"]:
            s["share"] = e["sleeve_share"].get(s["sleeve"])
            s["vol_contrib"] = e["sleeve_vol_contrib"].get(s["sleeve"])
        out["portfolio"] = {"vol": d["vol"], "variance": d["variance"],
                            "attribution": "defined" if d["variance"] > 0 else
                            "undefined (zero estimated variance)"}
        out["sensitivity"] = weekly_sensitivity(panel[proxies], proxies, w, priced)
        out["risk_status"] = "available"
    out["overlaps"] = overlaps(lines, out["instruments"], corr)
    return out


def overlaps(lines: list[dict], instruments: list[dict], corr) -> dict:
    """Three separate lists; none is summed into a NAV total."""
    def rows(group):
        return [{"sleeve": l["sleeve"], "etf": l["etf"], "held": l["held"]} for l in group]

    def by(key):
        g: dict[str, list[dict]] = {}
        for ln in lines:
            if ln[key]:
                g.setdefault(ln[key], []).append(ln)
        return g
    exact = [{"instrument": k, "lines": rows(v), "combined": sum(l["held"] for l in v)}
             for k, v in sorted(by("etf").items()) if len({l["sleeve"] for l in v}) > 1]
    proxy = [{"proxy": k, "lines": rows(v), "combined": sum(l["held"] for l in v)}
             for k, v in sorted(by("proxy").items())
             if len({l["sleeve"] for l in v}) > 1 and len({l["etf"] for l in v}) > 1]
    correlated = []
    if corr is not None:
        sleeves_of = {i["proxy"]: sorted({l["sleeve"] for l in i["lines"]}) for i in instruments}
        names = [i["proxy"] for i in instruments]
        for a in range(len(names)):
            for b in range(a + 1, len(names)):
                rho = corr[a, b]
                if not np.isfinite(rho) or rho < CORR_DISPLAY_THRESHOLD:
                    continue  # undefined correlations are suppressed, not listed
                sa, sb = sleeves_of[names[a]], sleeves_of[names[b]]
                if sa == sb and len(sa) == 1:
                    continue  # same-sleeve pair: not a cross-sleeve overlap
                correlated.append({"a": names[a], "a_sleeves": sa,
                                   "a_weight": instruments[a]["weight"],
                                   "b": names[b], "b_sleeves": sb,
                                   "b_weight": instruments[b]["weight"], "corr": float(rho)})
        correlated.sort(key=lambda r: -r["corr"])
    return {"exact": exact, "proxy": proxy, "correlated": correlated,
            "corr_threshold": CORR_DISPLAY_THRESHOLD}


# --------------------------------------------------------------------------
# Output contract and snapshot validity
# --------------------------------------------------------------------------

REQUIRED = {"schema": str, "calc_sha256": str, "run_status": str, "run_id": str,
            "computed_at_utc": str, "capital_status": str, "risk_status": str,
            "reasons": list, "basis": dict, "fingerprint": dict, "sleeves": list,
            "instruments": list, "overlaps": dict}
UNDEFINED_ATTRIBUTION = "undefined (zero estimated variance)"
SUM_TOL = 1e-6


def _req(cond, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _walk_strict(obj, path: str = "$") -> None:
    """Every value is a JSON type and every number finite; named on failure."""
    if isinstance(obj, float):
        _req(math.isfinite(obj), f"{path} is non-finite ({obj!r})")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _req(isinstance(k, str), f"{path} has a non-string key {k!r}")
            _walk_strict(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_strict(v, f"{path}[{i}]")
    else:
        _req(obj is None or isinstance(obj, (str, bool, int)),
             f"{path} has unsupported type {type(obj).__name__}")


def _check_series_fp(fs, name: str, cutoff: str | None) -> None:
    _req(isinstance(fs, dict), f"fingerprint for {name} is not an object")
    obs = fs.get("observations")
    _req(isinstance(obs, dict) and len(obs) >= 2, f"fingerprint for {name} has no observations")
    dates = list(obs)
    _req(all(parse_iso_date(d) is not None for d in dates)
         and dates == sorted(set(dates)), f"fingerprint for {name} has malformed dates")
    _req(all(isinstance(v, str) and HEX16.fullmatch(v) for v in obs.values()),
         f"fingerprint for {name} has malformed digests")
    _req(fs.get("n") == len(obs) and fs.get("first_used") == dates[0]
         and fs.get("last_used") == dates[-1] and fs.get("sha256") == canonical_sha(obs),
         f"fingerprint for {name} is internally inconsistent")
    after = fs.get("after_cutoff")
    _req(isinstance(after, int) and not isinstance(after, bool) and after >= 0,
         f"fingerprint for {name} has no post-cutoff count")
    _req(cutoff is None or dates[-1] <= cutoff, f"fingerprint for {name} runs past the cutoff")


PROVENANCE_KEYS = (*ENGINE_FX_PROVENANCE, "aligned_to", "exported_at_utc")


def _check_provenance(prov) -> None:
    _req(isinstance(prov, dict) and all(isinstance(prov.get(k), str) for k in PROVENANCE_KEYS),
         "basis fx provenance is malformed (fields " + ", ".join(PROVENANCE_KEYS) + " must be strings)")


def _check_coverage(instruments: list, prices_fp: dict, fx_fp: dict | None,
                    basis: dict, cutoff: str) -> None:
    """The evidence must be the evidence of THIS estimate: each price
    fingerprint is the instrument's whole recorded series through the cutoff,
    their common dates are exactly the recorded common closes, and the FX
    fingerprint covers every EUR close within the staleness cap. A
    self-consistent but truncated fingerprint therefore cannot support a
    window of hundreds of returns."""
    from bisect import bisect_right
    _req(set(prices_fp) == {i["proxy"] for i in instruments},
         "price evidence does not match the instruments")
    for i in instruments:
        pm, fs = i.get("price"), prices_fp[i["proxy"]]
        _req(isinstance(pm, dict) and pm.get("closes") == fs["n"] and pm.get("first") == fs["first_used"]
             and pm.get("last") == fs["last_used"] == cutoff,
             f"price evidence does not cover the recorded price record for {i['proxy']}")
    common = sorted(set.intersection(*(set(fs["observations"]) for fs in prices_fp.values())))
    _req(len(common) >= 2 and basis.get("common_closes") == len(common)
         and basis.get("n_returns") == len(common) - 1
         and basis.get("first_return_date") == common[1] and basis.get("last_return_date") == common[-1] == cutoff,
         "price evidence does not cover the recorded common-close window "
         f"({len(common)} common dates in the evidence, {basis.get('common_closes')!r} recorded)")
    if fx_fp is None:
        return
    fx_days = sorted(parse_iso_date(x) for x in fx_fp["observations"])
    for i in instruments:
        if i["fx"] != "eur_to_usd":
            continue
        for x in prices_fp[i["proxy"]]["observations"]:
            day = parse_iso_date(x)
            k = bisect_right(fx_days, day) - 1
            _req(k >= 0 and (day - fx_days[k]).days <= FX_MAX_STALE_DAYS,
                 f"FX evidence does not cover {i['proxy']} close {x} within {FX_MAX_STALE_DAYS} days")


LINE_KEYS = {"sleeve", "etf", "held"}
CORR_KEYS = {"a", "a_sleeves", "a_weight", "b", "b_sleeves", "b_weight", "corr"}


def _line_set(lines, what: str) -> list[tuple]:
    """Typed child holding records as sorted (sleeve, etf, held) tuples."""
    _req(isinstance(lines, list) and all(
        isinstance(l, dict) and set(l) == LINE_KEYS and isinstance(l["sleeve"], str)
        and isinstance(l["etf"], str) and _isnum(l["held"]) and l["held"] > 0 for l in lines),
        f"{what} line records malformed (each needs sleeve, etf and a positive held)")
    return sorted((l["sleeve"], l["etf"], float(l["held"])) for l in lines)


def _check_book(obj: dict, fp: dict, risk: bool) -> None:
    """Everything the display shows about the book must be derivable from the
    canonical held book (whose hash is held_basis_sha256, checked against the
    current file by validate_snapshot) and the saved resolver mapping (re-derived
    by validate_snapshot): sleeve capital, the unique proxy instruments with
    their FX handling, child lines and weights, and the exact and proxy overlap
    lists. Two legal instruments on one proxy remain one instrument."""
    hb = fp.get("held_book")
    _req(isinstance(hb, dict) and set(hb) == {"anchor", "model_as_of", "lines"}
         and isinstance(hb["lines"], list), "fingerprint held_book missing or malformed")
    _req(canonical_sha(hb) == fp["held_basis_sha256"] and hb["anchor"] == fp["anchor"],
         "fingerprint held_book does not match held_basis_sha256 or the anchor")
    held = []
    for r in hb["lines"]:
        _req(isinstance(r, list) and len(r) == 3 and isinstance(r[0], str) and isinstance(r[1], str)
             and _isnum(r[2]), "fingerprint held_book line malformed")
        if r[2] > 0:
            held.append((r[0], r[1], float(r[2])))
    _req(len({(s, e) for s, e, _ in held}) == len(held), "held book repeats a sleeve line")
    mapping = fp.get("mapping")
    _req(isinstance(mapping, dict) and set(mapping) == {e for _, e, _ in held},
         "proxy mapping does not cover exactly the held ETFs")
    for e, m in mapping.items():
        _req(isinstance(m, dict) and set(m) == {"proxy", "fx"}
             and ((m["proxy"] is None and m["fx"] is None)
                  or (isinstance(m["proxy"], str) and m["proxy"] and m["fx"] in ("none", "eur_to_usd"))),
             f"proxy mapping for {e} malformed")
    if risk:
        _req(all(m["proxy"] is not None for m in mapping.values()),
             "risk available with an unresolved proxy mapping")

    by_sleeve: dict[str, float] = {}
    for s, _, h in held:
        by_sleeve[s] = by_sleeve.get(s, 0.0) + h
    sleeves = obj["sleeves"]
    _req(sorted(s["sleeve"] for s in sleeves) == sorted(by_sleeve)
         and all(abs(s["capital"] - by_sleeve[s["sleeve"]]) <= SUM_TOL for s in sleeves)
         and abs(obj["capital_total"] - sum(by_sleeve.values())) <= SUM_TOL,
         "sleeve capital does not match the held book")

    expected: dict[str, list[tuple]] = {}
    for s, e, h in held:
        if mapping[e]["proxy"] is not None:
            expected.setdefault(mapping[e]["proxy"], []).append((s, e, h))
    instruments = obj["instruments"]
    _req([i["proxy"] for i in instruments] == sorted(expected),
         "instruments do not match the held book's proxy mapping (one instrument per proxy)")
    for i in instruments:
        got = _line_set(i["lines"], f"instrument {i['proxy']}")
        _req(got == sorted(expected[i["proxy"]]),
             f"instrument {i['proxy']} lines do not match the held book")
        _req(all(mapping[e]["fx"] == i["fx"] for _, e, _ in got),
             f"instrument {i['proxy']} FX handling disagrees with the proxy mapping")
        _req(abs(i["weight"] - sum(h for _, _, h in got)) <= SUM_TOL,
             f"instrument {i['proxy']} weight does not equal its lines")
    eur = any(i["fx"] == "eur_to_usd" for i in instruments)
    _req((obj["basis"].get("fx") is None) != eur and (fp.get("fx") is None or eur),
         "FX record disagrees with the instruments' FX handling")

    ov = obj["overlaps"]

    def groups(key):
        g: dict[str, list[tuple]] = {}
        for s, e, h in held:
            k = e if key == "instrument" else mapping[e]["proxy"]
            if k is not None:
                g.setdefault(k, []).append((s, e, h))
        return g
    want = {"exact": {k: v for k, v in groups("instrument").items() if len({x[0] for x in v}) > 1},
            "proxy": {k: v for k, v in groups("proxy").items()
                      if len({x[0] for x in v}) > 1 and len({x[1] for x in v}) > 1}}
    for kind, key in (("exact", "instrument"), ("proxy", "proxy")):
        rows = ov[kind]
        _req(all(set(r) == {key, "lines", "combined"} and isinstance(r[key], str)
                 and _isnum(r["combined"]) for r in rows), f"overlaps {kind} record malformed")
        _req([r[key] for r in rows] == sorted(want[kind]), f"overlaps {kind} do not match the held book")
        for r in rows:
            got = _line_set(r["lines"], f"overlaps {kind} {r[key]}")
            _req(got == sorted(want[kind][r[key]])
                 and abs(r["combined"] - sum(h for _, _, h in got)) <= SUM_TOL,
                 f"overlaps {kind} {r[key]} do not match the held book")
    # Correlated pairs follow overlaps()' selection: real instruments named by
    # string, in instrument order, each pair once, cross-sleeve, at or above
    # the configured threshold, listed by descending correlation with ties in
    # instrument order. Completeness cannot be checked here: the artefact does
    # not carry the correlation matrix, and nothing is recomputed.
    inst = {i["proxy"]: i for i in instruments}
    pos = {p: k for k, p in enumerate(inst)}
    seen: set[tuple[str, str]] = set()
    order: list[tuple] = []
    for c in ov["correlated"]:
        _req(set(c) == CORR_KEYS and isinstance(c["a"], str) and isinstance(c["b"], str),
             "overlaps correlated record malformed (needs string a and b, their sleeves and "
             "weights, and corr)")
        a, b = inst.get(c["a"]), inst.get(c["b"])
        _req(a is not None and b is not None and a is not b
             and c["a_sleeves"] == sorted({l["sleeve"] for l in a["lines"]})
             and c["b_sleeves"] == sorted({l["sleeve"] for l in b["lines"]})
             and _isnum(c["a_weight"]) and abs(c["a_weight"] - a["weight"]) <= SUM_TOL
             and _isnum(c["b_weight"]) and abs(c["b_weight"] - b["weight"]) <= SUM_TOL,
             "overlaps correlated record does not match the instruments")
        pair = f"{c['a']}/{c['b']}"
        _req(pos[c["a"]] < pos[c["b"]], f"overlaps correlated pair {pair} is not in instrument order")
        _req((c["a"], c["b"]) not in seen, f"overlaps correlated pair {pair} repeats")
        seen.add((c["a"], c["b"]))
        _req(not (c["a_sleeves"] == c["b_sleeves"] and len(c["a_sleeves"]) == 1),
             f"overlaps correlated pair {pair} is a same-sleeve pair, not a cross-sleeve overlap")
        _req(c["corr"] >= CORR_DISPLAY_THRESHOLD,
             f"overlaps correlated pair {pair} correlation {c['corr']} is below the display "
             f"threshold {CORR_DISPLAY_THRESHOLD}")
        order.append((-c["corr"], pos[c["a"]], pos[c["b"]]))
    _req(order == sorted(order), "overlaps correlated pairs are not in descending correlation order")


def _check_shares(rows: list, key: str, defined: bool, what: str) -> None:
    for r in rows:
        v = r.get(key)
        if defined:
            _req(_isnum(v), f"{what} {key} must be a number when attribution is defined")
        else:
            _req(v is None, f"{what} {key} must be null (undefined), not {v!r}")


def validate_output(obj: dict) -> None:
    """Nested output contract and strict JSON. Raises ValueError naming the
    first violation. Risk "available" requires a recorded same-basis
    reconciliation pass and complete dependency evidence; at zero portfolio
    variance every attribution figure must be null."""
    _req(isinstance(obj, dict), "artefact is not an object")
    _walk_strict(obj)
    for k, t in REQUIRED.items():
        _req(isinstance(obj.get(k), t), f"artefact field {k!r} missing or not {t.__name__}")
    _req(obj["schema"] == SCHEMA,
         f"schema {obj['schema']!r} is not {SCHEMA}: an artefact from an earlier calculator "
         "cannot be verified; rebuild with `python scripts/combined_book_risk.py build`")
    _req(bool(HEX64.fullmatch(obj["calc_sha256"])), "calc_sha256 is malformed")
    fp = obj["fingerprint"]
    _req(fp.get("schema") == obj["schema"] and fp.get("calc_sha256") == obj["calc_sha256"],
         "fingerprint calculator identifiers disagree with the artefact")
    _req(obj["run_status"] in ("ok", "error"), "run_status must be ok or error")
    _req(obj["capital_status"] in ("available", "unavailable")
         and obj["risk_status"] in ("available", "unavailable"),
         "status fields must be available or unavailable")
    _req(all(isinstance(r, str) for r in obj["reasons"]), "reasons must be strings")
    risk = obj["risk_status"] == "available"
    capital = obj["capital_status"] == "available"
    if obj["run_status"] == "error":
        _req(not capital and not risk and obj["reasons"],
             "a failed run must carry reasons and no capital or risk")
        return
    _req(risk or obj["reasons"], "an unavailable estimate must carry reasons")
    basis, sleeves, instruments = obj["basis"], obj["sleeves"], obj["instruments"]
    _req(all(isinstance(s, dict) for s in sleeves) and all(isinstance(i, dict) for i in instruments),
         "sleeves and instruments must be objects")
    cutoff = fp.get("anchor")
    _req(cutoff is None or parse_iso_date(cutoff) is not None, "fingerprint anchor malformed")
    prices_fp = fp.get("prices", {})
    _req(isinstance(prices_fp, dict), "fingerprint prices must be an object")
    for name, fs in prices_fp.items():
        _check_series_fp(fs, name, cutoff)
    fx_fp = fp.get("fx")
    if fx_fp is not None:
        _req(isinstance(fx_fp, dict) and isinstance(fx_fp.get("id"), str)
             and isinstance(fx_fp.get("provenance_sha256"), str)
             and HEX64.fullmatch(fx_fp["provenance_sha256"]), "fingerprint fx is malformed")
        _check_series_fp(fx_fp, "EUR/USD", cutoff)
    fx_basis = basis.get("fx")
    _req(fx_basis is None or isinstance(fx_basis, dict), "basis fx must be an object")
    if isinstance(fx_basis, dict) and fx_basis.get("provenance") is not None:
        _check_provenance(fx_basis["provenance"])
    if capital:
        _req(isinstance(cutoff, str), "fingerprint anchor missing")
        _req(isinstance(fp.get("held_basis_sha256"), str) and HEX64.fullmatch(fp["held_basis_sha256"]),
             "fingerprint held_basis_sha256 missing")
        _req(basis.get("anchor") == cutoff == basis.get("price_cutoff"),
             "basis anchor and cutoff disagree with the fingerprint")
        _req(sleeves and all(isinstance(s.get("sleeve"), str) and _isnum(s.get("capital"))
                             and s["capital"] > 0 for s in sleeves), "sleeve capital malformed")
        _req(_isnum(obj.get("capital_total")) and _isnum(obj.get("residual_nav"))
             and abs(sum(s["capital"] for s in sleeves) - obj["capital_total"]) <= SUM_TOL,
             "capital total does not equal the sleeve capital")
        rec = obj.get("reconciliation")
        _req(isinstance(rec, dict) and rec.get("status") in ("pass", "fail", "not checked"),
             "reconciliation record missing")
    else:
        _req(not sleeves and not instruments and not risk, "capital unavailable but rows present")
    for i in instruments:
        _req(isinstance(i.get("proxy"), str) and i.get("fx") in ("none", "eur_to_usd")
             and _isnum(i.get("weight")) and i["weight"] > 0 and isinstance(i.get("lines"), list),
             "instrument record malformed")
    ov = obj["overlaps"]
    _req(all(isinstance(ov.get(k), list) for k in ("exact", "proxy", "correlated")),
         "overlaps malformed")
    for k in ("exact", "proxy", "correlated"):
        _req(all(isinstance(r, dict) for r in ov[k]), f"overlaps {k} entries must be objects")
    _req(_isnum(ov.get("corr_threshold")) and ov["corr_threshold"] == CORR_DISPLAY_THRESHOLD,
         f"overlaps corr_threshold is not the configured display threshold {CORR_DISPLAY_THRESHOLD}")
    for c in ov["correlated"]:
        _req(_isnum(c.get("corr")) and -1 - 1e-9 <= c["corr"] <= 1 + 1e-9,
             "correlation outside [-1, 1] or undefined")
    if capital:
        _check_book(obj, fp, risk)

    if not risk:
        _req(obj.get("portfolio") is None and obj.get("sensitivity") is None,
             "risk unavailable but a portfolio or sensitivity is present")
        _check_shares(sleeves, "share", False, "sleeve")
        _check_shares(sleeves, "vol_contrib", False, "sleeve")
        _check_shares(instruments, "share", False, "instrument")
        _check_shares(instruments, "vol_contrib", False, "instrument")
        _check_shares(instruments, "vol", False, "instrument")
        _req(not ov["correlated"], "risk unavailable but correlations listed")
        return

    # Risk available: same-basis reconciliation pass and complete evidence.
    _req(not obj["reasons"], "risk available with reasons")
    rec = obj["reconciliation"]
    _req(rec["status"] == "pass" and _isnum(rec.get("max_abs_diff"))
         and rec.get("tolerance") == WEIGHT_TOL and rec["max_abs_diff"] <= WEIGHT_TOL,
         "risk available without a recorded same-basis reconciliation pass")
    _req(rec.get("live_targets_held") in ("pass", "not applicable (other as-of)"),
         "risk available without a passed or inapplicable live_targets cross-check")
    _req(isinstance(fp.get("live_track_sha256"), str) and HEX64.fullmatch(fp["live_track_sha256"])
         and fp["live_track_sha256"] == rec.get("live_track_sha256"),
         "live_track evidence missing or inconsistent")
    lt_sha = fp.get("live_targets_held_sha256")
    _req(lt_sha == rec.get("live_targets_held_sha256")
         and ((lt_sha is None) == (rec["live_targets_held"] != "pass")),
         "live_targets evidence missing or inconsistent")
    _req(instruments, "risk available without instruments")
    _req(abs(sum(i["weight"] for i in instruments) - obj["capital_total"]) <= SUM_TOL
         and basis.get("nav_covered") == obj["capital_total"],
         "risk estimate does not cover the whole held book")
    _req(all(i["proxy"] in prices_fp for i in instruments), "price evidence missing for an instrument")
    eur = any(i["fx"] == "eur_to_usd" for i in instruments)
    if eur:
        _req(isinstance(fx_fp, dict) and fx_fp.get("id") == "engine-export", "FX evidence missing")
        _req(isinstance(fx_basis, dict) and fx_basis.get("id") == "engine-export"
             and isinstance(fx_basis.get("provenance"), dict)
             and fx_provenance_sha(fx_basis["provenance"]) == fx_fp["provenance_sha256"]
             and fx_basis.get("label") == fx_label(fx_basis["provenance"]),
             "FX label or provenance disagrees with the fingerprint")
    _check_coverage(instruments, prices_fp, fx_fp if eur else None, basis, cutoff)
    _req(isinstance(basis.get("n_returns"), int) and basis["n_returns"] >= MIN_RETURNS,
         "too few returns recorded for an available estimate")
    port = obj.get("portfolio")
    _req(isinstance(port, dict) and _isnum(port.get("variance")) and _isnum(port.get("vol"))
         and port["variance"] >= 0 and port["vol"] >= 0
         and abs(port["vol"] - math.sqrt(port["variance"])) <= 1e-12 * max(1.0, port["vol"]),
         "portfolio variance or vol malformed")
    defined = port["variance"] > 0
    _req(port.get("attribution") == ("defined" if defined else UNDEFINED_ATTRIBUTION),
         "portfolio attribution label disagrees with the variance")
    _check_shares(sleeves, "share", defined, "sleeve")
    _check_shares(sleeves, "vol_contrib", defined, "sleeve")
    _check_shares(instruments, "share", defined, "instrument")
    _check_shares(instruments, "vol_contrib", defined, "instrument")
    _req(all(_isnum(i.get("vol")) and i["vol"] >= 0 for i in instruments), "instrument vol malformed")
    if defined:
        _req(abs(sum(i["share"] for i in instruments) - 1) <= SUM_TOL
             and abs(sum(s["share"] for s in sleeves) - 1) <= SUM_TOL
             and abs(sum(i["vol_contrib"] for i in instruments) - port["vol"]) <= SUM_TOL,
             "risk contributions do not add up")
    sens = obj.get("sensitivity")
    _req(isinstance(sens, dict), "sensitivity missing")
    for key in ("common_close", "weekly"):
        r = sens.get(key)
        _req(isinstance(r, dict) and isinstance(r.get("n_returns"), int), f"sensitivity {key} malformed")
        shares = r.get("sleeve_share")
        if _isnum(r.get("vol")) and r["vol"] > 0:
            _req(isinstance(shares, dict) and all(_isnum(v) for v in shares.values()),
                 f"sensitivity {key} shares must be numbers")
        else:
            _req(shares is None or (isinstance(shares, dict) and all(v is None for v in shares.values())),
                 f"sensitivity {key} shares must be undefined at zero variance")
    json.dumps(obj, allow_nan=False)


def _fx_series_for(fp_fx: dict, data_dir: Path) -> tuple[pd.Series, dict | None] | None:
    src = next((s for s in fx_sources(data_dir) if s["id"] == fp_fx.get("id")), None)
    return read_fx(src) if src else None


def _observations_check(name: str, saved: dict, current: pd.Series, cutoff: date,
                        problems: list[str], notes: list[str]) -> None:
    """Re-verify every used observation still present in the current export.

    Rolled out (not re-verifiable): saved dates before the current series'
    first date. Accepted only when no more have left the front than have been
    appended after the cutoff SINCE the snapshot: post-cutoff observations
    already in the export when it was taken (``after_cutoff``) do not count.
    Every other saved date must be present with the same value; no new date
    may appear inside the used window."""
    obs: dict = saved["observations"]
    after = int((current.index.date > cutoff).sum()) - saved["after_cutoff"]
    cur_first = f"{current.index[0]:%Y-%m-%d}"
    window = current[(current.index >= pd.Timestamp(saved["first_used"]))
                     & (current.index.date <= cutoff)]
    now = observation_digests(window)
    rolled = [d for d in obs if d < cur_first]
    retained = [d for d in obs if d >= cur_first]
    deleted = [d for d in retained if d not in now]
    restated = [d for d in retained if d in now and now[d] != obs[d]]
    added = sorted(set(now) - set(obs))

    def few(ds):
        return ", ".join(ds[:3]) + (f" and {len(ds) - 3} more" if len(ds) > 3 else "")
    if restated:
        problems.append(f"{name}: {len(restated)} used observation(s) restated ({few(restated)})")
    if deleted:
        problems.append(f"{name}: {len(deleted)} used observation(s) deleted ({few(deleted)})")
    if added:
        problems.append(f"{name}: {len(added)} observation(s) added inside the used window ({few(added)})")
    if rolled:
        if len(rolled) > after:
            problems.append(f"{name}: {len(rolled)} earliest used observation(s) are gone but only "
                            f"{max(after, 0)} have been appended after the cutoff since the snapshot; "
                            "not a fixed-window roll")
        else:
            notes.append(f"{name}: {len(rolled)} earliest used observation(s) ({rolled[0]} to "
                         f"{rolled[-1]}) have rolled out of the export and cannot be re-verified; "
                         f"the other {len(retained)} were re-verified")


def pending_path(artefact: Path) -> Path:
    return artefact.with_name(artefact.name + ".pending")


def _pending_check(marker: Path) -> dict:
    try:
        started = json.loads(marker.read_text(encoding="utf-8")).get("started_utc", "unknown")
    except (OSError, ValueError, AttributeError):
        started = "unknown"
    return {"state": "invalid", "notes": [], "reasons": [
        f"a calculator run started {started} has not completed ({marker.name} present); "
        "the artefact may be older than the inputs. Rerun the build"]}


def validate_snapshot(saved: dict, data_dir: Path = DATA_DIR,
                      artefact_path: Path | None = None) -> dict:
    """Display state for a saved artefact against the CURRENT files.

    current     - every recorded input still matches, same held-book anchor
    historical  - inputs for its own as-of still match, but the held book has
                  moved to a newer anchor; valid as a dated snapshot only
    invalid     - the artefact breaks its contract, an input it depends on has
                  changed or cannot be verified, the calculator changed, the
                  last run failed, or a run has not completed
    Appending observations after the cutoff changes nothing here. Expected
    data problems become reasons; programming errors propagate."""
    marker = pending_path(artefact_path or (data_dir / OUT_PATH.name))
    if marker.exists():
        return _pending_check(marker)
    problems: list[str] = []
    notes: list[str] = []
    try:
        validate_output(saved)
    except ValueError as exc:
        return {"state": "invalid", "reasons": [f"artefact fails its contract: {exc}"], "notes": []}
    if saved["run_status"] != "ok":
        return {"state": "invalid", "reasons": ["the last calculator run failed: "
                                               + "; ".join(saved["reasons"])], "notes": []}
    fp = saved["fingerprint"]
    if fp["calc_sha256"] != CALC_SHA256:
        problems.append("the calculator has changed since this snapshot was computed")
    anchor = fp.get("anchor")
    if anchor is None:
        return {"state": "invalid", "reasons": problems + ["snapshot has no valid held book"],
                "notes": []}
    cutoff = date.fromisoformat(anchor)
    state = "current"
    # The snapshot's own ETFs are re-resolved (never the newer book's), so a
    # proxy or FX handling the project resolvers would not produce is caught
    # whether the snapshot is current or historical.
    for etf, m in sorted((fp.get("mapping") or {}).items()):
        try:
            now_proxy, now_fx = instrument_for(etf)
        except InputError:
            now_proxy, now_fx = None, None
        if (m["proxy"], m["fx"]) != (now_proxy, now_fx):
            problems.append(f"{etf}: proxy mapping {m['proxy']}/{m['fx']} is not what the project "
                            f"resolvers give ({now_proxy}/{now_fx})")
    try:
        basis = read_object(data_dir / "component_held_basis.json")
        _check_basis_shape(basis)
        cur_anchor = basis["anchor"]
        if cur_anchor != anchor:
            state = "historical"
            notes.append(f"the model-held book has moved from {anchor} to {cur_anchor}")
        elif canonical_sha(held_canonical(basis)) != fp.get("held_basis_sha256"):
            problems.append("held amounts changed for the same anchor")
    except InputError as exc:
        problems.append(f"current held basis unreadable: {exc}")
    if state == "current":
        try:
            if canonical_sha(live_track_canonical(data_dir)) != fp.get("live_track_sha256"):
                problems.append("live_track effective weights or anchor changed")
        except InputError as exc:
            problems.append(f"live_track cannot be verified: {exc}")
        try:
            _, lt_sha = live_targets_held(data_dir, anchor)
            if lt_sha is not None and lt_sha != fp.get("live_targets_held_sha256"):
                problems.append("live_targets held lines changed for the same anchor")
        except InputError as exc:
            problems.append(f"live_targets cannot be verified: {exc}")
    try:
        prices, _ = read_prices(data_dir)
        for sym, saved_p in (fp.get("prices") or {}).items():
            entry = prices.get(sym)
            try:
                if not isinstance(entry, dict):
                    raise InputError("series is missing from the export")
                s = validated_series(entry.get("dates"), entry.get("prices"), sym)
            except InputError as exc:
                problems.append(f"{sym}: {exc}")
                continue
            _observations_check(sym, saved_p, s, cutoff, problems, notes)
    except InputError as exc:
        problems.append(str(exc))
    if fp.get("fx"):
        try:
            got = _fx_series_for(fp["fx"], data_dir)
            if got is None:
                problems.append(f"FX source {fp['fx'].get('id')} is no longer configured")
            else:
                s, prov = got
                if prov is None or fx_provenance_sha(prov) != fp["fx"].get("provenance_sha256"):
                    problems.append("EUR/USD provenance changed or cannot be established")
                _observations_check("EUR/USD", fp["fx"], s, cutoff, problems, notes)
        except InputError as exc:
            problems.append(f"EUR/USD cannot be verified: {exc}")
    if problems:
        return {"state": "invalid", "reasons": problems, "notes": notes}
    return {"state": state, "reasons": [], "notes": notes}


READ_ATTEMPTS = 3


def read_snapshot(artefact: Path, data_dir: Path = DATA_DIR) -> tuple[dict | None, dict]:
    """Read and validate the artefact as one operation, without taking the run
    lock (a reader must never refuse a build, nor leave a lock file in a
    directory the scheduled job commits).

    A run or recovery creates the marker before it replaces the artefact and
    removes it only after the replacement, so a read is accepted only if the
    marker is absent before the read and again after validation, and the
    bytes are unchanged across validation. A run whose marker was absent at
    the second check and had not replaced the file by the second read began
    after this read. A changed file is re-read up to READ_ATTEMPTS times; then
    the artefact is withheld. Returns (blob, check); blob is None unless the
    bytes were stable and met the contract, and then only check["reasons"]
    may be shown. Unreadable or malformed JSON raises InputError."""
    marker = pending_path(artefact)

    def withheld(reason: str) -> tuple[None, dict]:
        return None, {"state": "invalid", "reasons": [reason], "notes": []}

    for _ in range(READ_ATTEMPTS):
        if marker.exists():
            return None, _pending_check(marker)
        try:
            raw = artefact.read_bytes()
        except FileNotFoundError:
            raise InputError(f"{artefact.name} is missing") from None
        blob = _loads_strict(raw.decode("utf-8"), artefact.name)
        check = validate_snapshot(blob, data_dir, artefact)
        if marker.exists():
            return withheld(f"a calculator run or recovery started while {artefact.name} was "
                            "being validated; the estimate is withheld. Reload once it completes")
        try:
            stable = artefact.read_bytes() == raw
        except FileNotFoundError:
            stable = False
        if not stable:
            continue
        try:
            validate_output(blob)
        except ValueError:
            return None, check
        return blob, check
    return withheld(f"{artefact.name} changed while being validated on {READ_ATTEMPTS} "
                    "successive reads; the estimate is withheld. Reload once runs have finished")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def write_atomic(obj: dict, path: Path) -> None:
    """Strict JSON, validated, written to a temporary file then renamed. A
    failed rename removes the temporary file and re-raises."""
    validate_output(obj)
    text = json.dumps(obj, indent=1, allow_nan=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class RunActive(Exception):
    """Another run owns the artefact (active), or an interrupted run's marker
    awaits ``recover``."""


def lock_path(artefact: Path) -> Path:
    return artefact.with_name(artefact.name + ".lock")


class RunLock:
    """Exclusive execution ownership of one artefact: an operating-system
    byte-range lock on ``<artefact>.lock`` (msvcrt on Windows, flock
    elsewhere), held for the whole of a run or a recovery. The operating
    system releases it when the owning process ends for any reason, so a free
    lock beside a ``.pending`` marker means the marker's run is no longer
    executing; a held lock means it is. A second handle is refused even within
    one process. The lock file itself is empty and left in place: deleting it
    while another process has it open would race."""

    def __init__(self, artefact: Path):
        self.artefact = artefact
        self.path = lock_path(artefact)
        self.fd: int | None = None

    def acquire(self) -> bool:
        """True when acquired, False when another owner holds it; any other
        OSError propagates."""
        import errno
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK):
                return False
            raise
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _marker_owner(marker: Path) -> dict:
    try:
        rec = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def begin_run(artefact: Path, run_id: str, now: datetime) -> RunLock:
    """Take exclusive ownership BEFORE any calculation, then create the
    persistent marker. While the marker exists validate_snapshot rejects
    whatever artefact is on disk. A held lock (another run or a recovery is
    executing) raises RunActive. A marker found under a free lock belongs to a
    run that ended without completing; it is never taken over or overwritten
    here, only cleared by ``recover``. Any other OSError aborts the run before
    it calculates anything. The caller releases the returned lock."""
    run = RunLock(artefact)
    if not run.acquire():
        raise RunActive(f"another run or recovery is executing ({run.path.name} is held)")
    marker = pending_path(artefact)
    try:
        if marker.exists():
            owner = _marker_owner(marker)
            raise RunActive(f"another run ({owner.get('run_id', 'unknown id')}, started "
                            f"{owner.get('started_utc', 'unknown')}) did not complete and left "
                            f"{marker.name}; it is no longer executing. Clear it with "
                            "`python scripts/combined_book_risk.py recover --run-id "
                            f"{owner.get('run_id', '<id>')}`")
        with marker.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": run_id,
                                 "started_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}))
    except BaseException:
        run.release()
        raise
    return run


def end_run(run: RunLock, run_id: str) -> None:
    """Clear the marker once its own run has replaced the artefact. Called
    with the lock held, so no other run or recovery can change the marker
    between the ownership check and the removal."""
    if run.fd is None:
        raise RuntimeError("end_run called without holding the run lock")
    marker = pending_path(run.artefact)
    if _marker_owner(marker).get("run_id") != run_id:
        raise RuntimeError(f"{marker.name} belongs to another run; left in place")
    marker.unlink()


def recover(artefact: Path, run_id: str) -> str:
    """Explicit recovery from a run that ended without completing. Refused
    while any process holds the run lock, whatever run id is named: an
    operator-supplied id does not establish that the run has stopped. Under
    the lock, and only when the named id is the marker's, the artefact is
    first REPLACED by a failure record (so the earlier result cannot become
    current), then the marker is removed. The view stays invalid until the
    next successful run."""
    marker = pending_path(artefact)
    lock = RunLock(artefact)
    if not lock.acquire():
        raise SystemExit(f"run {run_id} or another run is still active ({lock.path.name} is held); "
                         "nothing recovered, the marker is left in place")
    try:
        if not marker.exists():
            raise SystemExit(f"{marker.name} is not present; nothing to recover")
        owner = _marker_owner(marker).get("run_id")
        if owner != run_id:
            raise SystemExit(f"run id {run_id} does not match the marker's run ({owner}); left in place")
        now = datetime.now(timezone.utc)
        write_atomic(failed_run(RuntimeError(
            f"run {run_id} was interrupted before completing; recovered at "
            f"{now:%Y-%m-%dT%H:%M:%SZ}. Rerun the build"), now, run_id), artefact)
        marker.unlink()
    finally:
        lock.release()
    return f"cleared {marker.name} of interrupted run {run_id}; the artefact records the interruption; rerun the build"


def failed_run(exc: BaseException, now: datetime, run_id: str) -> dict:
    return {"schema": SCHEMA, "calc_sha256": CALC_SHA256, "run_status": "error",
            "run_id": run_id,
            "computed_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "reporting_only": True,
            "capital_status": "unavailable", "risk_status": "unavailable",
            "reasons": [f"{type(exc).__name__}: {exc}"], "basis": {},
            "fingerprint": {"calc_sha256": CALC_SHA256, "schema": SCHEMA},
            "sleeves": [], "instruments": [], "portfolio": None, "sensitivity": None,
            "overlaps": {"exact": [], "proxy": [], "correlated": [],
                         "corr_threshold": CORR_DISPLAY_THRESHOLD}}


def export_engine_fx(data_dir: Path = DATA_DIR) -> None:
    """Operator-run, NOT part of any build: re-derive the engine's EUR/USD on
    the held Xetra lines' closes through the engine converter (a yfinance
    fetch) and write it, with structured provenance, for the risk view."""
    from run_europe_rotation import _fx_convert_eur_to_usd
    prices, _ = read_prices(data_dir)
    sym = next(s for s in sorted(prices) if s.endswith(".DE"))
    closes_eur = validated_series(prices[sym]["dates"], prices[sym]["prices"], sym).to_frame()
    usd = _fx_convert_eur_to_usd(closes_eur)
    implied = (usd.iloc[:, 0] / closes_eur.iloc[:, 0]).dropna()
    validated_series([d.strftime("%Y-%m-%d") for d in implied.index],
                     [float(v) for v in implied.values], "EUR/USD")
    obj = {"schema": FX_EXPORT_SCHEMA,
           "provenance": {**ENGINE_FX_PROVENANCE, "aligned_to": sym,
                          "exported_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
           "dates": [d.strftime("%Y-%m-%d") for d in implied.index],
           "close": [float(v) for v in implied.values]}
    fx_provenance(obj, ENGINE_FX_EXPORT)  # the export must satisfy its own reader
    path = data_dir / ENGINE_FX_EXPORT
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    print(f"wrote {path} ({len(implied)} rates, last {implied.index[-1].date()})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="build",
                    choices=["build", "export-engine-fx", "check", "recover"])
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--run-id", help="recover: the interrupted run named in the marker")
    args = ap.parse_args(argv)
    if args.command == "export-engine-fx":
        export_engine_fx(args.data_dir)
        return 0
    if args.command == "recover":
        if not args.run_id:
            raise SystemExit("recover needs --run-id (the id recorded in the .pending marker)")
        print(recover(args.out, args.run_id))
        return 0
    if args.command == "check":
        _, v = read_snapshot(args.out, args.data_dir)
        print(f"{args.out.name}: {v['state']}")
        for r in v["reasons"] + v["notes"]:
            print(f"  - {r}")
        return 0 if v["state"] == "current" else 1
    now = datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex
    try:
        run = begin_run(args.out, run_id, now)
    except RunActive as exc:
        raise SystemExit(f"not started: {exc}; nothing calculated, the artefact is unchanged") from None
    except OSError as exc:
        raise SystemExit(f"cannot establish the run marker ({exc}); nothing calculated, "
                         "the previous artefact is unchanged") from exc
    try:
        try:
            result = build(args.data_dir, now)
            result["run_id"] = run_id
            write_atomic(result, args.out)
        except BaseException as exc:
            # A failed run must not leave an earlier estimate eligible: record the
            # failure, and if even that cannot be written, keep the marker so the
            # old artefact validates as invalid. The original error is re-raised.
            try:
                write_atomic(failed_run(exc, now, run_id), args.out)
            except BaseException as exc2:
                print(f"could not record the failed run ({type(exc2).__name__}: {exc2}); "
                      f"{pending_path(args.out).name} left in place", file=sys.stderr)
            else:
                end_run(run, run_id)
            raise
        end_run(run, run_id)
    finally:
        run.release()
    print(f"wrote {args.out}: capital {result['capital_status']}, risk {result['risk_status']}")
    for r in result["reasons"]:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
