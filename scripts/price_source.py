"""Which price source an engine run is on, and whether its cache agrees.

WHY THIS EXISTS (2026-09-03, WS19c adoption). Sleeves B and C read
``BTE_PRICE_SOURCE`` to choose between yfinance and the locally licensed
Norgate feed, and the choice had two silent failure modes:

1. THE CACHE-REUSE BRANCH IGNORED IT. Each engine reuses its parquet cache
   whenever the cache is current through the last completed session, and
   that branch returns before the source selection runs. WS19 measured the
   consequence directly: under ``BTE_PRICE_SOURCE=norgate`` with a current
   yfinance cache, neither engine touched Norgate — the switch was vacuous,
   and a cache holed by the vendor (the 2026-08-28 withheld Friday) stayed
   holed under a flag that promised otherwise. The cache now carries a
   sidecar naming the source it was built from, and a request for a
   different source refuses the reuse.

2. AN UNREACHABLE FEED FELL BACK SILENTLY. ``select_columns`` returns the
   frame unchanged when Norgate is down, so a scheduled run asked for Norgate
   could publish a yfinance-basis book with nothing in the log but one line.
   A basis flip is a restatement; it must be chosen, not suffered. A request
   for ``norgate`` now FAILS when the feed is unreachable, and ``auto`` is the
   explicit way to accept the fallback — with the fallback recorded.

Python datetime months are 1-indexed (January = 1).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ENV_VAR = "BTE_PRICE_SOURCE"
SOURCES = ("yfinance", "norgate", "auto")
DEFAULT = "yfinance"


def requested_source(env: dict | None = None) -> str:
    """The source the environment asks for; ``yfinance`` when unset."""
    value = (env if env is not None else os.environ).get(ENV_VAR, DEFAULT)
    value = (value or DEFAULT).strip().lower()
    if value not in SOURCES:
        raise ValueError(
            f"{ENV_VAR}={value!r} is not a source (expected one of {SOURCES})")
    return value


def resolve_source(requested: str, available=None) -> tuple[str, str]:
    """(effective source, reason). Raises when ``norgate`` is asked for and
    the feed cannot be reached — a basis flip must be chosen, not suffered."""
    if requested not in SOURCES:
        raise ValueError(
            f"{requested!r} is not a source (expected one of {SOURCES})")
    if requested == "yfinance":
        return "yfinance", "requested"
    if available is None:
        import norgate_prices  # local: keeps this module importable anywhere
        available = norgate_prices.available
    reachable = bool(available())
    if requested == "norgate":
        if not reachable:
            raise RuntimeError(
                f"{ENV_VAR}=norgate but the Norgate feed is unreachable. "
                f"Refusing to fall back silently: a yfinance-basis run under a "
                f"Norgate flag is a restatement nobody chose. Start the Norgate "
                f"Data Updater, or set {ENV_VAR}=yfinance to accept the "
                f"yfinance basis explicitly, or {ENV_VAR}=auto to fall back "
                f"with the fallback recorded.")
        return "norgate", "requested and reachable"
    if reachable:
        return "norgate", "auto: feed reachable"
    return "yfinance", "auto: feed unreachable, fell back"


def sidecar_path(cache_path: Path) -> Path:
    """``x.parquet`` -> ``x.source.json``, beside the cache, gitignored with it."""
    cache_path = Path(cache_path)
    return cache_path.with_name(cache_path.stem + ".source.json")


def read_cache_source(cache_path: Path) -> str | None:
    """The source a cache was built from, or None when nothing recorded it.
    Caches written before 2026-09-03 have no sidecar; every one of them was
    a yfinance download, which is how callers should read None."""
    path = sidecar_path(cache_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("source")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def write_cache_source(cache_path: Path, source: str,
                       report: dict | None = None) -> Path:
    """Record beside the cache which source built it and, for Norgate, which
    columns it took, so a later reader can tell a mixed frame from a swap."""
    path = sidecar_path(cache_path)
    payload = {
        "source": source,
        "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if report:
        payload["columns_from_norgate"] = list(report.get("replaced") or [])
        payload["columns_kept_on_incumbent"] = list(report.get("kept") or [])
        payload["unresolved"] = list(report.get("unresolved") or [])
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def cache_matches(recorded: str | None, effective: str) -> bool:
    """May a current cache be reused for a run on ``effective``?

    An unrecorded cache is a yfinance cache (see read_cache_source)."""
    return (recorded or "yfinance") == effective


# ---------------------------------------------------------------------------
# Reachable is not serving (2026-09-03 review)
#
# ``norgate_prices.available()`` answers one question: is the local service
# up. ``select_columns`` then returns the incumbent frame UNCHANGED when every
# symbol comes back unserved, and keeps a column on the incumbent when
# Norgate's dates are not a superset of yfinance's — which is exactly what a
# Norgate feed one session behind at 09:00 looks like. Before this guard a
# strict ``norgate`` run could pass the preflight, take nothing from Norgate,
# and still record source "norgate" in the sidecar and the payload: the
# vacuous switch WS19 measured, in a new costume. The mode is real — on
# 2026-09-03 the gate-states publisher got "access denied" on a symbol at
# 14:12 SGT with the service up, and the same symbol served again by evening.
#
# A strict run therefore asserts that every plain US listing it asked for
# was TAKEN from Norgate, and fails otherwise. ``auto`` is the explicit way
# to accept a partial or absent take, with the fallback recorded.
# ---------------------------------------------------------------------------
def plain_us_listing(ticker: str) -> bool:
    """A plain US listing: no '.XX' venue suffix, no '-USD' pair, no '=X' FX.
    The lines Norgate covers, and the same rule ``repair_price_gaps.us_listed``
    applies when choosing a secondary source."""
    t = str(ticker)
    return "." not in t and "-" not in t and "=" not in t


def norgate_shortfall(report: dict | None, tickers) -> dict[str, list[str]]:
    """The plain US listings among ``tickers`` that a strict-Norgate run did
    NOT take from Norgate, sorted by reason: ``kept_on_incumbent`` (served,
    but not a date superset — usually Norgate a session behind),
    ``unresolved`` (no Norgate symbol) and ``unserved`` (resolved, but the
    feed returned nothing, or no selection ran at all)."""
    rep = report or {}
    replaced = set(rep.get("replaced") or [])
    kept = set(rep.get("kept") or [])
    unresolved = set(rep.get("unresolved") or [])
    out: dict[str, list[str]] = {"kept_on_incumbent": [], "unresolved": [],
                                 "unserved": []}
    for t in tickers:
        t = str(t)
        if not plain_us_listing(t) or t in replaced:
            continue
        if t in kept:
            out["kept_on_incumbent"].append(t)
        elif t in unresolved:
            out["unresolved"].append(t)
        else:
            out["unserved"].append(t)
    return out


def assert_norgate_complete(report: dict | None, tickers, label: str = "") -> None:
    """Raise unless every plain US listing in ``tickers`` was taken from
    Norgate. Called by a strict ``norgate`` run right after the column
    selection and BEFORE the cache and its sidecar are written, so a frame
    that is not on the Norgate basis is never recorded as one."""
    short = norgate_shortfall(report, tickers)
    expected = [str(t) for t in tickers if plain_us_listing(str(t))]
    missing = sum(len(v) for v in short.values())
    if not missing:
        return
    taken = len(expected) - missing
    prefix = f"{label}: " if label else ""
    raise RuntimeError(
        f"{prefix}{ENV_VAR}=norgate but Norgate supplied {taken} of "
        f"{len(expected)} US lines. Kept on the incumbent (not a date "
        f"superset — usually Norgate a session behind): "
        f"{short['kept_on_incumbent']}; unresolved: {short['unresolved']}; "
        f"served nothing: {short['unserved']}. Refusing to record yfinance "
        f"columns under a Norgate label. Wait for the Norgate Data Updater "
        f"to catch up and re-run, or set {ENV_VAR}=yfinance to accept that "
        f"basis explicitly, or {ENV_VAR}=auto to accept a partial take with "
        f"the fallback recorded."
    )
