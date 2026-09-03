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
