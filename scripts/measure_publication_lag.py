"""Measure the iShares product-data API publication lag empirically.

Why this exists (2026-08-08): the Saturday ~03:00-04:00 UTC refresh found
0 of 38 funds with Friday 2026-08-07 holdings; all 38 walked back to
Thursday 2026-08-06, and a manual re-probe at 04:21 UTC confirmed Friday
was still unpublished while Thursday was served. The historical walkback
rate is 1.02% (173 of 16,929 snapshots) and 38 of those 173 are this one
week, so Fridays normally resolve exactly — the run was simply earlier
than iShares' publication. One observation is not a lag measurement;
this script is the instrument that produces one.

What one run does: for a small representative sample of funds (default
CSP1 UK-listed S&P 500, SOXX US-listed semis, EXV1 Europe supersector,
IJPN Japan — spanning the three publication regimes: US underlying / UK
listing, US/US, Europe/UK, Asia/UK), it asks the endpoint which of the
last N calendar days currently have holdings and appends one observation
row per ETF to data/publication_lag_log.jsonl. Repeated a few times a
day over one to two weeks, the rows sweep out the publication curve: for
each trade date T, the earliest probe timestamp at which T's holdings
appear brackets the publication time (published between the last probe
that missed it and the first that saw it).

Method notes:
  - Transport and parsing reuse scripts/fetch_constituents.py:
    ``fetch_product_data`` (retries + throttle) and ``parse_holdings_json``
    (the date-parity-enforcing parser). For a no-data date the API
    silently rewrites asOfDate to the latest available date; the parser
    rejects that, so an empty result here means "this exact date has no
    holdings yet", never "the API answered with a different date".
  - The newest probed date's payload additionally yields the asOfDate the
    API itself echoes (extracted via the contract-checked
    ``_holdings_datapoints`` helper, not ad-hoc payload spelunking).
    Recorded as ``api_reported_latest`` — a one-request cross-check of
    the per-date sweep; ``cross_check_mismatch`` flags disagreement.
  - No cache files are read or written. Every probe hits the network, so
    each row is a true observation at its own timestamp. This is why the
    probe does NOT go through ``load_snapshot_tickers`` (cache-first).
  - The log is APPEND-ONLY JSONL and deliberately NOT gitignored:
    observations at a past timestamp cannot be refetched, which makes
    them source data, not cache. Commit it with the weekly outputs.

Cadence decision framework this instrument feeds
------------------------------------------------
The operating goal is "every Saturday in Singapore, breadth as of US
Friday close". Prices/breadth already meet that (Friday close bars are
available Saturday morning SGT); the open question is only the
membership ROSTER, whose publication lag L (Friday close 21:00 UTC ->
holdings served for Friday's asOfDate) is what this script measures.

  - If L is reliably under ~7h  : the current Saturday ~03:00-04:00 UTC
    run captures Friday's roster; nothing changes.
  - If L is ~7-24h              : move the refresh later on Saturday SGT
    (e.g. Saturday 12:00 UTC = 20:00 SGT) or re-run fetch-only in the
    afternoon. Costs: factsheet email lands later (the weekly_factsheet
    workflow fires on the push touching data/breadth_csp1.json, so
    refresh timing and factsheet timing are coupled); operator loses the
    Saturday-morning slot.
  - If L is >24h or erratic     : either (a) accept the Thursday roster
    with Friday prices — defensible for a quarterly-rebalancing index,
    membership is one day stale at worst and the walkback array records
    it — or (b) move the run to Sunday/Monday SGT. Cost of (a): a
    permanent ~1-session roster lag on Fridays, immaterial for breadth;
    cost of (b): the factsheet slips a day-plus and Monday runs collide
    with the 21:30 UTC hard-guard window.

Trustworthiness bar: a few probes per day for one to two weeks, spanning
at least two weekends, before setting any cadence. Each Friday
contributes ONE lag observation, so two weekends give n=2 Friday
observations plus ~10 weekday ones (weekday lag is visible too: probe
day T+0 evening vs T+1 morning). Treat n<2 Fridays as anecdote, not
measurement. Scheduling of repeated probes is deliberately NOT wired up
here — cadence for unattended jobs is the owner's call, and per the
vault rule any scheduled run needs its own guard layer first.

Run:
    python scripts/measure_publication_lag.py                     # default sample, 8 days
    python scripts/measure_publication_lag.py --etfs CSP1 SOXX --days 5
    python scripts/measure_publication_lag.py --dry-run           # print, do not append

Python datetime months are 1-indexed (January = 1). All date arithmetic
uses datetime.timedelta / pandas — never manual day offsets.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etf_registry import get_etf  # noqa: E402
from fetch_constituents import (  # noqa: E402
    EndpointUnavailable,
    PayloadContractError,
    _holdings_datapoints,
    fetch_product_data,
    parse_holdings_json,
)
from nyse_sessions import last_completed_session, sessions_behind  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "data" / "publication_lag_log.jsonl"

# One fund per publication regime. CSP1: US underlying, UK listing (the
# gating panel). SOXX: US/US via targetSite=ishares-us. EXV1: Europe
# underlying, UK listing. IJPN: Asia underlying (Tokyo closes 06:00 UTC,
# far ahead of NYSE), UK listing.
DEFAULT_ETFS = ["CSP1", "SOXX", "EXV1", "IJPN"]
DEFAULT_DAYS = 8


# ---------------------------------------------------------------------------
# Pure logic — unit-tested offline in tests/test_measure_publication_lag.py
# ---------------------------------------------------------------------------
def probe_window(today: date, days: int) -> list[date]:
    """The ``days`` calendar dates ending at ``today``, oldest first.

    Calendar days, not trading days: weekend/holiday absences are part of
    the signal (they confirm the parser's no-data reading), and the
    window must not depend on any exchange calendar to stay comparable
    across US, Europe and Asia funds.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def summarise(dates_probed: list[date],
              has_data: dict[date, bool]) -> tuple[list[date], date | None]:
    """(dates that had data, most recent date with data or None)."""
    with_data = [d for d in dates_probed if has_data.get(d)]
    return with_data, (max(with_data) if with_data else None)


def echoed_iso(payload: dict) -> str | None:
    """The asOfDate the API echoed, as ISO YYYY-MM-DD, or None.

    Uses fetch_constituents' contract-checked descent; a payload whose
    shape has drifted raises there rather than being misread here.
    """
    dps = _holdings_datapoints(payload)
    raw = dps["asOfDate"].get("value")
    if raw is None:
        return None
    return datetime.strptime(str(raw), "%Y%m%d").date().isoformat()


def cross_check_mismatch(latest_with_data: date | None,
                         api_reported_latest: str | None) -> bool:
    """True when the per-date sweep and the API's own fallback answer
    disagree. Only meaningful when both sides produced an answer."""
    if latest_with_data is None or api_reported_latest is None:
        return False
    return latest_with_data.isoformat() != api_reported_latest


# ---------------------------------------------------------------------------
# Network probe
# ---------------------------------------------------------------------------
def probe_etf(symbol: str, dates: list[date]) -> dict:
    """Probe one ETF across ``dates``; return the observation row (dict).

    Never raises: transport failures are recorded in the row so a dead
    endpoint is itself an observation, not a lost one.
    """
    cfg = get_etf(symbol)
    overrides = cfg.get("ticker_overrides", {})
    apply_suffix = cfg.get("apply_exchange_suffix", False)
    t0 = time.perf_counter()

    has_data: dict[date, bool] = {}
    n_tickers: dict[date, int] = {}
    errors: dict[str, str] = {}
    newest_payload: dict | None = None
    for d in dates:
        try:
            payload = fetch_product_data(d, cfg)
        except EndpointUnavailable as exc:
            errors[d.isoformat()] = str(exc)
            continue
        if d == dates[-1]:
            newest_payload = payload
        try:
            tickers = parse_holdings_json(
                payload, d, ticker_overrides=overrides,
                apply_exchange_suffix=apply_suffix,
            )
        except PayloadContractError as exc:
            errors[d.isoformat()] = str(exc)
            continue
        has_data[d] = bool(tickers)
        n_tickers[d] = len(tickers)

    with_data, latest = summarise(dates, has_data)
    api_latest: str | None = None
    if newest_payload is not None:
        try:
            api_latest = echoed_iso(newest_payload)
        except PayloadContractError as exc:
            errors["api_reported_latest"] = str(exc)

    now_utc = datetime.now(timezone.utc)
    nyse_expected = last_completed_session(now_utc)
    row = {
        "probe_utc": now_utc.isoformat(timespec="seconds"),
        "etf": symbol,
        "region": cfg.get("ishares_region", "uk"),
        "trading_calendar": cfg.get("trading_calendar", "NYSE"),
        "dates_probed": [d.isoformat() for d in dates],
        "dates_with_data": [d.isoformat() for d in with_data],
        "latest_with_data": latest.isoformat() if latest else None,
        "n_tickers_latest": n_tickers.get(latest) if latest else None,
        "api_reported_latest": api_latest,
        "cross_check_mismatch": cross_check_mismatch(latest, api_latest),
        "last_completed_nyse_session": nyse_expected.isoformat(),
        "sessions_behind_nyse": (
            sessions_behind(latest, nyse_expected) if latest else None
        ),
        "errors": errors,
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe which recent asOfDates the iShares product-data "
        "API currently serves, and append the observations to the "
        "publication-lag log.",
    )
    parser.add_argument("--etfs", nargs="*", default=DEFAULT_ETFS,
                        help=f"registry symbols to probe (default: "
                             f"{' '.join(DEFAULT_ETFS)})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"calendar days to probe, ending today UTC "
                             f"(default: {DEFAULT_DAYS})")
    parser.add_argument("--log", default=str(DEFAULT_LOG),
                        help="JSONL file to append to")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the observation rows, do not append")
    args = parser.parse_args(argv)

    # Derive today from the clock, never from a literal (2026-07 digest
    # mis-date lesson). UTC date: the API's asOfDate grid is exchange-side,
    # and UTC keeps rows comparable regardless of where the probe ran.
    today = datetime.now(timezone.utc).date()
    dates = probe_window(today, args.days)
    print(f"Probing {len(args.etfs)} ETFs x {len(dates)} dates "
          f"({dates[0].isoformat()} -> {dates[-1].isoformat()}), "
          f"~{len(args.etfs) * len(dates) * 2}s of throttle ahead ...",
          flush=True)

    rows = []
    any_total_failure = False
    for symbol in args.etfs:
        row = probe_etf(symbol.upper(), dates)
        rows.append(row)
        served = ", ".join(row["dates_with_data"]) or "none"
        print(f"  {row['etf']:<5} latest={row['latest_with_data']} "
              f"(api says {row['api_reported_latest']}, "
              f"{'MISMATCH' if row['cross_check_mismatch'] else 'agree'}); "
              f"served: {served}", flush=True)
        if row["errors"]:
            print(f"        errors: {row['errors']}", flush=True)
        if row["latest_with_data"] is None and row["errors"]:
            any_total_failure = True

    if args.dry_run:
        print(json.dumps(rows, indent=2))
        return 1 if any_total_failure else 0

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    print(f"\nAppended {len(rows)} observation(s) to "
          f"{log_path.relative_to(ROOT) if log_path.is_relative_to(ROOT) else log_path}")
    return 1 if any_total_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
