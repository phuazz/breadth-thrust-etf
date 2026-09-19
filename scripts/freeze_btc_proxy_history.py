#!/usr/bin/env python3
"""Freeze the pre-IBIT sleeve C Bitcoin proxy history (WS21 §4, one-off).

WHAT THIS WRITES. ``data/btc_proxy_history_pre_ibit.parquet`` — one column,
``BTC-USD``, covering NYSE sessions strictly before 2024-01-11 plus the
2024-01-11 value ``S_c`` — and ``data/btc_proxy_history_pre_ibit.source.json``
carrying the SHA-256 of those bytes, the row count, first and last dates,
``S_c``, the derivation text, the source cache's sidecar and the freeze
timestamp. BOTH ARE COMMITTED, and after that neither is ever rewritten: the
registration says the pre-cut-over segment is frozen and never refetched, so a
later Yahoo revision of pre-2024 Bitcoin history cannot reach the record.

WHY A FROZEN ARTEFACT AT ALL (registration §8.1). Under the spliced basis every
value after the cut-over is ``S_c * IBIT_t / IBIT_c``. Move ``S_c`` and the
whole post-cut-over segment moves by the ratio — a silent restatement of the
record with nothing in any diff to show for it, because the cache is
gitignored. The artefact plus its hash is what makes that impossible rather
than merely unlikely.

SOURCE. The main clone's ``data/thematic_prices_cache.parquet``, which is
gitignored and local. This script refuses unless that cache is on the incumbent
basis, so a cache already rebuilt under ``BTE_C_BTC_BASIS=ibit`` cannot be
frozen as if it were the incumbent — that would anchor the splice on itself.

LICENCE. It also refuses if the ``BTC-USD`` column was taken from Norgate.
Norgate's terms allow derived values to be published but not the raw series,
which is why every engine price cache is gitignored; this artefact IS a raw
series and IS committed, so it may only ever hold the Yahoo spot proxy. Norgate
sells no crypto database, so the check should never fire — which is exactly
when a licence guard is worth writing.

    python scripts/freeze_btc_proxy_history.py            # write both files
    python scripts/freeze_btc_proxy_history.py --check    # report, write nothing

Python datetime months are 1-indexed (January = 1). Session arithmetic goes
through venue_calendars.get_calendar("NYSE"); no day counts anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import btc_basis  # noqa: E402
import price_source as ps  # noqa: E402
from venue_calendars import get_calendar  # noqa: E402

SOURCE_CACHE = PROJECT_ROOT / "data" / "thematic_prices_cache.parquet"


class FreezeRefused(RuntimeError):
    """The source cache is not the thing the registration says to freeze."""


def _nyse_sessions(start, end) -> pd.DatetimeIndex:
    """Completed NYSE sessions between two dates, inclusive."""
    sched = get_calendar("NYSE").schedule(
        start_date=pd.Timestamp(start).date(), end_date=pd.Timestamp(end).date())
    return pd.DatetimeIndex(pd.to_datetime(sched.index)).normalize()


def check_source(cache: Path = SOURCE_CACHE) -> tuple[pd.Series, dict]:
    """The source column and its sidecar, or a refusal naming the reason."""
    if not cache.exists():
        raise FreezeRefused(
            f"{cache} is absent. This is the main clone's gitignored sleeve C "
            f"cache; run the sleeve C engine there first, and never run this "
            f"from the scheduled clone.")
    sidecar_path = ps.sidecar_path(cache)
    sidecar: dict = {}
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FreezeRefused(f"{sidecar_path.name} is unreadable: {exc}") from exc

    # (1) The cache must be on the INCUMBENT Bitcoin basis. A cache rebuilt
    # under the flag would anchor the splice on a spliced series.
    declared = (sidecar.get("column_basis") or {}).get(btc_basis.SPOT_KEY)
    if declared:
        raise FreezeRefused(
            f"the source cache declares {btc_basis.SPOT_KEY} basis "
            f"{declared!r}; only the incumbent basis may be frozen. Rebuild "
            f"the cache with {btc_basis.ENV_VAR} unset and re-run.")

    # (2) Licence: this artefact is committed, so the column may not be
    # Norgate's. Norgate sells no crypto database, so this should never fire.
    if btc_basis.SPOT_KEY in (sidecar.get("columns_from_norgate") or []):
        raise FreezeRefused(
            f"{btc_basis.SPOT_KEY} was taken from Norgate in the source cache. "
            f"Norgate's licence permits publishing derived values, not the raw "
            f"series, and this artefact is committed. Refusing.")

    frame = pd.read_parquet(cache)
    if btc_basis.SPOT_KEY not in frame.columns:
        raise FreezeRefused(
            f"the source cache has no {btc_basis.SPOT_KEY} column "
            f"(columns: {sorted(frame.columns)[:6]} ...)")
    series = frame[btc_basis.SPOT_KEY].astype(float).dropna()
    series.index = pd.to_datetime(series.index).normalize()
    cut = pd.Timestamp(btc_basis.CUTOVER)
    if cut not in series.index:
        last = series.index.max()
        raise FreezeRefused(
            f"the source cache's {btc_basis.SPOT_KEY} column does not reach "
            f"the cut-over {btc_basis.CUTOVER} (last priced "
            f"{last.date() if len(series) else 'never'}). The cut-over value "
            f"IS S_c; there is nothing to anchor without it.")
    return series, sidecar


def build_segment(series: pd.Series) -> pd.Series:
    """NYSE sessions strictly before the cut-over, plus the cut-over value."""
    cut = pd.Timestamp(btc_basis.CUTOVER)
    seg = series.loc[series.index <= cut].sort_index()
    sessions = _nyse_sessions(seg.index.min(), cut)
    # The incumbent cache already sits on the equity calendar (the crypto
    # reindex runs at load time), so this should drop nothing. It is a check
    # that the frozen artefact is a NYSE-session series, made by construction
    # rather than by assumption.
    off = seg.index.difference(sessions)
    if len(off):
        print(f"  dropping {len(off)} non-NYSE row(s) from the frozen segment: "
              f"{[str(d.date()) for d in off[:6]]}", flush=True)
        seg = seg.loc[seg.index.isin(sessions)]
    if seg.index.max() != cut:
        raise FreezeRefused("the frozen segment lost its cut-over row")
    if seg.index.has_duplicates:
        raise FreezeRefused("the frozen segment carries duplicate dates")
    return seg


def freeze(cache: Path = SOURCE_CACHE,
           parquet: Path = btc_basis.FROZEN_PARQUET,
           sidecar: Path = btc_basis.FROZEN_SIDECAR,
           write: bool = True, now=None, force: bool = False) -> dict:
    # ONE-OFF MEANS ONCE (2026-09-19). This overwrote the parquet and its
    # sidecar together, so a second run replaced the anchor AND the hash that
    # certified it, and the loader's check passed on the replacement. Every
    # sleeve C Bitcoin value after 2024-01-11 is a ratio off S_c, so that is a
    # silent restatement of the whole post-cut-over segment.
    #
    # An existing artefact is therefore a refusal, not a target. `--force`
    # exists for the genuine re-freeze, which is a restatement either way and
    # still has to move btc_basis.REGISTERED_SHA256 through a reviewed commit
    # before the loader will accept the result.
    if write and not force and Path(parquet).exists():
        raise FreezeRefused(
            f"{Path(parquet).name} already exists. It is frozen by "
            f"registration and never refetched, and overwriting it would move "
            f"S_c and restate every value after {btc_basis.CUTOVER} with "
            f"nothing in any diff to show for it. Use --check to compare, or "
            f"--force if a restatement is genuinely intended - which also "
            f"needs btc_basis.REGISTERED_SHA256 updated in a reviewed commit.")
    series, source_sidecar = check_source(cache)
    seg = build_segment(series)
    cut = pd.Timestamp(btc_basis.CUTOVER)
    s_c = float(seg.loc[cut])
    report = {
        "rows": int(len(seg)),
        "first": str(seg.index.min().date()),
        "last": str(seg.index.max().date()),
        "S_c": s_c,
    }
    if not write:
        report["sha256"] = None
        return report

    Path(parquet).parent.mkdir(parents=True, exist_ok=True)
    seg.to_frame(btc_basis.SPOT_KEY).to_parquet(parquet)
    digest = btc_basis.file_sha256(parquet)
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    payload = {
        "column": btc_basis.SPOT_KEY,
        "sha256": digest,
        **report,
        "cutover": str(btc_basis.CUTOVER),
        "derivation": btc_basis.DERIVATION,
        "source_cache": str(Path(cache).name),
        "source_cache_sidecar": source_sidecar,
        "frozen_at_utc": stamp,
        "registration": "KICKOFF_ws21-c-bitcoin-basis.md §4",
        "never_refetch": (
            "Frozen by registration. A later vendor revision of pre-2024 "
            "Bitcoin history deliberately does not reach the record. Do not "
            "re-run this script to make a hash guard pass."),
    }
    payload["sha256"] = digest
    Path(sidecar).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report["sha256"] = digest
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="report what would be frozen and write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing frozen artefact — a "
                             "restatement; also needs REGISTERED_SHA256 moved")
    args = parser.parse_args(argv)
    try:
        report = freeze(write=not args.check, force=args.force)
    except (FreezeRefused, btc_basis.BasisError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    verb = "would freeze" if args.check else "froze"
    print(f"  {verb} {report['rows']} NYSE sessions "
          f"{report['first']} -> {report['last']}")
    print(f"  S_c ({btc_basis.CUTOVER}) = {report['S_c']!r}")
    if report.get("sha256"):
        print(f"  sha256 {report['sha256']}")
        print(f"  wrote {btc_basis.FROZEN_PARQUET.name} and "
              f"{btc_basis.FROZEN_SIDECAR.name} — commit both")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
