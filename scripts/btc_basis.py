"""Which series sleeve C ranks its Bitcoin line on (WS21, staged 2026-09-19).

WHY THIS EXISTS. Sleeve C trades Bitcoin through IBIT but ranks it on a spot
proxy: Yahoo's ``BTC-USD`` UTC-day close, reindexed to the NYSE calendar, with
a modelled 25 bp/yr expense drag (Phase 15.2, 2026-05-26). Every other sleeve C
name is measured at the 16:00 ET close; this one is measured three to four
hours later, net of a fee the model applies rather than the market. WS21 asks
whether the line should be ranked on the instrument it trades, from the first
session that instrument exists, with the proxy retained only where no ETF
existed. The registration is ``KICKOFF_ws21-c-bitcoin-basis.md``; its §4 is
frozen and this module implements exactly that construction and no other.

EVERYTHING HERE IS INERT UNLESS ``BTE_C_BTC_BASIS=ibit``. Scheduled runs never
set it, ``component_release.seal`` refuses to publish while it is set, and with
the flag unset the cache, the signal and every published number are bit-
identical to the incumbent. The default flips only after the WS7 verdict is
filed (registration §3), by changing ``DEFAULT`` below.

THE CONSTRUCTION, ONE CONSTRUCTION (registration §4):

    before c   the incumbent synthetic series exactly as the main clone's
               sleeve C cache carried it at the freeze commit, frozen into
               data/btc_proxy_history_pre_ibit.parquet with a SHA-256 and
               never refetched;
    at c       2024-01-11, IBIT's first NYSE close. S_c is the frozen value
               on that session and is preserved exactly;
    after c    S_t = S_c * IBIT_t / IBIT_c, no expense drag — IBIT's price is
               already net of the same 25 bp/yr the model charges before c.

The column key stays ``BTC-USD`` so history labels do not move; IBIT never
appears as its own column in the cache. The level is spot-like for cache
continuity and the ``ma_distance`` signal is scale-free, so the splice changes
the shape of the series and not its units.

THE ANCHOR IS THE WHOLE RISK (registration §8.1). If ``S_c`` or anything
before it moves, every value after c moves by the same ratio and the record
restates without a word. That is why the pre-c segment is a committed artefact
with its bytes hashed, why ``load_frozen_segment`` re-hashes on every read, and
why a Yahoo revision of pre-2024 Bitcoin history deliberately cannot reach the
record.

Python datetime months are 1-indexed (January = 1). Dates are compared as
dates, never as strings or weekday counts.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

ENV_VAR = "BTE_C_BTC_BASIS"
INCUMBENT = "incumbent"
IBIT = "ibit"
BASES = (INCUMBENT, IBIT)
# THE ONE LINE THAT FLIPS THE DEFAULT. Changing this to ``IBIT`` after the WS7
# verdict is filed, in a dated commit with M2 published alongside, IS the
# promotion described in registration §6. Nothing else needs to change.
DEFAULT = INCUMBENT

#: The sleeve C universe key. Unchanged by the flip, by design.
SPOT_KEY = "BTC-USD"
#: The traded line, and the symbol the fetch asks for under the flag.
TRADED = "IBIT"
#: IBIT's first NYSE close (Thursday — weekday verified with ``datetime``).
CUTOVER = date(2024, 1, 11)

FROZEN_PARQUET = DATA_DIR / "btc_proxy_history_pre_ibit.parquet"
FROZEN_SIDECAR = DATA_DIR / "btc_proxy_history_pre_ibit.source.json"

#: The production artefact's digest, REGISTERED IN CODE (2026-09-19).
#:
#: The sidecar is a writable file beside a writable parquet, so re-running the
#: freezer rewrote both together and the loader's hash check passed against a
#: replacement anchor - the guard verified internal consistency, not identity.
#: This constant is the second, independent witness: it lives in the source
#: tree, moves only through a reviewed commit, and pins the exact 1,517-session
#: segment ending 2024-01-11 at S_c = 45674.257342138306.
#:
#: If this ever has to change, the change IS the restatement, and the WS21
#: registration's frozen §4 is what it has to be argued against.
REGISTERED_SHA256 = (
    "7ab4a26a7dcf5f1471063702e6404bdee867ad8c02e6c9620eb153c26e7966b2")

#: The path the registered digest is ABOUT. Separate from FROZEN_PARQUET on
#: purpose: a test redirects FROZEN_PARQUET at a synthetic artefact, and if the
#: production check keyed on that it would fire against every fixture and say
#: nothing about production. This constant is never redirected, so the check
#: applies to exactly one file - the committed one.
PRODUCTION_PARQUET = DATA_DIR / "btc_proxy_history_pre_ibit.parquet"

#: Verbatim from registration §4, written into the frozen sidecar so the
#: derivation travels with the artefact rather than only with this file.
DERIVATION = (
    "Incumbent synthetic sleeve C Bitcoin series exactly as the main clone's "
    "data/thematic_prices_cache.parquet carried it at the freeze commit: "
    "Yahoo BTC-USD UTC-day close, reindexed to the NYSE calendar, compounded "
    "drag (1 - 0.0025)^(days/365) from its 2018 first bar. Covers NYSE "
    "sessions strictly before 2024-01-11 plus the 2024-01-11 value (S_c). "
    "Frozen and never refetched: a later Yahoo revision of pre-2024 Bitcoin "
    "history deliberately does not reach the record (WS21 §4, §8.1)."
)

#: Reader-facing label for the spliced column (registration §4).
LABEL = ("Bitcoin — IBIT from 2024-01-11; spot proxy with 25 bp/yr drag "
         "before")


class BasisError(RuntimeError):
    """The staged basis cannot be built from what is on disk."""


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------
def requested_basis(env: dict | None = None) -> str:
    """The basis the environment asks for; ``incumbent`` when unset.

    Raises on an unrecognised value rather than falling back. A typo that
    silently selected the incumbent would make a staged run look like a
    measurement of nothing, which is the failure mode WS19 recorded for
    ``BTE_PRICE_SOURCE``.
    """
    value = (env if env is not None else os.environ).get(ENV_VAR, DEFAULT)
    value = (value or DEFAULT).strip().lower()
    if value not in BASES:
        raise ValueError(
            f"{ENV_VAR}={value!r} is not a basis (expected one of {BASES})")
    return value


def is_ibit(env: dict | None = None) -> bool:
    """True when this run ranks the Bitcoin line on IBIT."""
    return requested_basis(env) == IBIT


def fetch_symbol(ticker: str, env: dict | None = None) -> str:
    """The symbol to ASK THE VENDOR for, given a universe key.

    Under the flag the spot ticker leaves the download list entirely and IBIT
    takes its place, which is what makes the tail heal's single-ticker probe
    ask IBIT rather than spot without any heal-site special case.
    """
    if ticker == SPOT_KEY and is_ibit(env):
        return TRADED
    return ticker


def fetch_list(tickers, env: dict | None = None) -> list[str]:
    """``tickers`` as the vendor should be asked for them under this basis."""
    return [fetch_symbol(str(t), env) for t in tickers]


# ---------------------------------------------------------------------------
# The frozen pre-cut-over segment
# ---------------------------------------------------------------------------
def file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes, hex."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_frozen_sidecar(sidecar: Path | None = None) -> dict:
    path = Path(sidecar) if sidecar else FROZEN_SIDECAR
    if not path.exists():
        raise BasisError(
            f"{path.name} is missing; run scripts/freeze_btc_proxy_history.py "
            f"in the main clone, where the sleeve C cache lives.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BasisError(f"{path.name} is unreadable: {exc}") from exc


def load_frozen_segment(parquet: Path | None = None,
                        sidecar: Path | None = None
                        ) -> tuple[pd.Series, dict]:
    """The frozen pre-``c`` segment and its sidecar, hash-verified.

    THE HASH IS CHECKED ON EVERY READ, not once at write time. The anchor is
    the whole risk: a changed byte moves ``S_c`` and every value after the
    cut-over shifts by the ratio, silently restating the record. A mismatch
    is a refusal, never a warning.

    Paths default to the module constants at CALL time, not at import time, so
    a caller (or a test) can point the pair at a different artefact by setting
    the module attributes.
    """
    path = Path(parquet) if parquet else FROZEN_PARQUET
    sidecar = Path(sidecar) if sidecar else FROZEN_SIDECAR
    if not path.exists():
        raise BasisError(
            f"{path.name} is missing; run scripts/freeze_btc_proxy_history.py.")
    meta = read_frozen_sidecar(sidecar)
    expected = str(meta.get("sha256") or "")
    actual = file_sha256(path)
    if not expected:
        raise BasisError(f"{Path(sidecar).name} records no sha256")
    if actual != expected:
        raise BasisError(
            f"{path.name} does not match its sidecar: sha256 {actual} on disk, "
            f"{expected} recorded. The frozen pre-cut-over segment is the "
            f"anchor for every value after 2024-01-11 — refusing to build on "
            f"bytes nobody signed. Restore the committed artefact; do NOT "
            f"re-freeze to make this pass.")
    # THE SECOND WITNESS. The check above proves the parquet and its sidecar
    # agree; re-running the freezer rewrites BOTH, so agreement alone cannot
    # tell the registered anchor from a replacement. For the production
    # artefact the digest must also equal the one registered in this file,
    # which only a reviewed commit can move. A test pointing the constants at
    # a temporary pair is exempt: it is not the production anchor.
    if path == PRODUCTION_PARQUET and actual != REGISTERED_SHA256:
        raise BasisError(
            f"{path.name} has sha256 {actual}, but the digest registered in "
            f"btc_basis.REGISTERED_SHA256 is {REGISTERED_SHA256}. The sidecar "
            f"agreeing with the file proves only that both were written "
            f"together. Restore the committed artefact; changing the "
            f"registered digest is a restatement of the WS21 record and needs "
            f"the argument that goes with one.")
    frame = pd.read_parquet(path)
    if list(frame.columns) != [SPOT_KEY]:
        raise BasisError(
            f"{path.name} should carry exactly one column {SPOT_KEY!r}, "
            f"found {list(frame.columns)}")
    series = frame[SPOT_KEY].astype(float)
    series.index = pd.to_datetime(series.index)
    cut = pd.Timestamp(CUTOVER)
    if series.index.max() != cut:
        raise BasisError(
            f"{path.name} ends {series.index.max().date()}, expected the "
            f"cut-over {CUTOVER}")
    if series.isna().any():
        raise BasisError(f"{path.name} carries blank cells")
    return series, meta


def column_basis_tag(sha256: str) -> str:
    """The per-column basis recorded in the price-cache sidecar.

    Carries the frozen artefact's hash prefix so a cache built on one anchor
    can never compare equal to a cache built on another.
    """
    return f"ibit-spliced@{str(sha256)[:12]}"


# ---------------------------------------------------------------------------
# The splice
# ---------------------------------------------------------------------------
def splice(frozen: pd.Series, traded: pd.Series,
           cutover: date = CUTOVER) -> pd.Series:
    """``S_t = S_c * IBIT_t / IBIT_c`` after ``c``; the frozen segment before.

    ``frozen`` ends at ``c`` and supplies ``S_c``; ``traded`` is IBIT's
    total-return close. Bars IBIT carries at or before ``c`` are dropped — the
    frozen segment owns that span by registration, and letting the vendor
    contribute there would reopen the anchor the freeze exists to close.

    No expense drag is applied to the post-``c`` segment: IBIT's price is
    already net of the 25 bp/yr the model charges before ``c``.
    """
    cut = pd.Timestamp(cutover)
    frozen = pd.Series(frozen).astype(float).sort_index()
    frozen.index = pd.to_datetime(frozen.index)
    traded = pd.Series(traded).astype(float).dropna().sort_index()
    traded.index = pd.to_datetime(traded.index)
    if cut not in frozen.index:
        raise BasisError(f"the frozen segment has no value at {cut.date()}")
    if cut not in traded.index:
        raise BasisError(
            f"{TRADED} has no close at the cut-over {cut.date()}; that session "
            f"is its first NYSE close and the ratio is undefined without it")
    anchor = float(traded.loc[cut])
    if not (anchor > 0):
        raise BasisError(f"{TRADED} close at {cut.date()} is {anchor!r}")
    s_c = float(frozen.loc[cut])
    post = traded.loc[traded.index > cut] * (s_c / anchor)
    out = pd.concat([frozen.loc[frozen.index <= cut], post]).sort_index()
    return out[~out.index.duplicated(keep="last")]


def build_column(traded: pd.Series, parquet: Path | None = None,
                 sidecar: Path | None = None) -> tuple[pd.Series, str]:
    """The spliced ``BTC-USD`` column and the basis tag to record for it."""
    frozen, meta = load_frozen_segment(parquet, sidecar)
    return splice(frozen, traded), column_basis_tag(meta["sha256"])


def declared_basis(env: dict | None = None, parquet: Path | None = None,
                   sidecar: Path | None = None) -> str | None:
    """The basis tag a cache built by THIS run should declare for ``BTC-USD``.

    ``None`` under the incumbent, which is also what every cache written
    before 2026-09-19 declares — so the comparison below is well defined for
    caches that predate the flag entirely.
    """
    if not is_ibit(env):
        return None
    _, meta = load_frozen_segment(parquet, sidecar)
    return column_basis_tag(meta["sha256"])
