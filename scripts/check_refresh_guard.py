"""Post-refresh guard: is the state refresh_all.py just produced coherent?

The vault rule (CLAUDE.md, session discipline): any run whose output is
trusted without a human re-deriving it needs a verification layer that
can catch a silently-wrong step. refresh_all.py is that run — 40+ steps,
~50 minutes, and every panel it writes is trusted downstream by the
dashboard, the factsheet email and the strategy engines. Each step
already fails loudly on its OWN errors; what none of them can see is
cross-panel incoherence: one ETF's fetch quietly serving an older week,
a panel whose endpoint died mid-run, a breadth series that stopped a
session short, or a rewrite that silently DROPPED history that the
previous commit still had (the 2026-08-04 SPY case: a later refresh lost
two sessions and nothing noticed).

This script runs after the refresh, before the operator commits, and
asserts over the whole 24-panel deployed set:

  G1  every constituents panel shares ONE end_friday, equal to the
      expected target Friday (latest completed Friday) — AND, since
      2026-08-30, the price side must corroborate the claim: no panel's
      constituent price cache may carry rows past its last POPULATED
      session. That night the vendor's batch download served a Friday
      row that merely EXISTED (IUFS 0 of 76 roster names non-NaN, EXH9
      1 of 28, while single-ticker requests returned real Friday bars);
      compute_breadth correctly capped the tail back to Thursday, G4
      honoured the declared cap — and this check printed "24 panels all
      end 2026-08-28" off the roster stamp alone. A row that exists is
      not a capture. The check now reads the cache and FAILS a traded
      panel whose tail is hollow, warns when the hollow panel is
      monitored-only (the G6 split), and warns when the gitignored cache
      is not on this machine to read. "Populated" is
      compute_breadth.priced_sessions — the writer's own tail-cap floor,
      imported, not re-derived;
  G2  no panel reports endpoint_health.status != "ok";
  G3  no panel is critically stale (staleness.status == "critical" or
      "no_real_fetches" fails; "warning" warns);
  G4  every breadth panel's end_date is a real session on that ETF's OWN
      trading calendar, and lands in the band the writer is allowed to
      produce: at least the last session on or before the shared
      end_friday, and at most that calendar's last COMPLETED session
      (XETR panels legitimately end on a US-holiday Friday that NYSE
      panels do not, and vice versa — see the 2026-07-03 boundary).

      A BAND, NOT AN EQUALITY, SINCE 2026-08-21. This was an equality
      against the end_friday bound until the 2026-08-15 tail-extension
      landed (register 2026-08-15-breadth-thrust-etf-1, CONFIRMED):
      compute_breadth now takes schedule_end = max(end_friday,
      last_completed_session), optionally capped back down by price
      coverage but never below end_friday. The guard was not updated with
      the writer, so from 2026-08-15 it failed EVERY panel on EVERY run —
      24 of 24 on 2026-08-21, all in the same direction, which is the
      signature of a rule change rather than corruption. A guard that
      fires on every run is a guard nobody reads, which is worse than no
      guard at all.

      The band keeps both failures the equality caught. Below the lower
      bound is a TRUNCATED panel — the silent-data-loss case, and still
      the one that matters most. Above the upper bound is a bar whose
      close has not happened — the partial-bar / look-ahead case. And the
      session-membership test keeps the phantom-bar case (a NYSE-calendar
      panel dated a US holiday) that the old equality caught only as a
      side effect of comparing against a session-derived date.

      THE LOWER BOUND HONOURS A DECLARED CAP, SINCE 2026-08-22. The floor
      above asks the ROSTER how far the panel should reach. On 2026-08-22
      the iShares roster for the European panels published Friday's
      holdings while the vendor had the constituents priced only to
      Thursday, so the floor was unreachable and G4 called five DEPLOYED
      panels TRUNCATED for ending exactly where their data ends. When a
      panel records a ``tail_cap`` — compute_breadth writes one whenever it
      pulls the tail back for price coverage — the floor drops to the
      session its constituents were actually priced for. A panel shorter
      than its OWN declared cap is still TRUNCATED, so the silent-loss case
      is unaffected; and a panel with no cap keeps the strict bound. One
      definition, stated by the writer, checked by the guard — rather than
      both re-deriving it and drifting apart;
  G5  no panel lost state versus the previous COMMITTED version (HEAD):
      no constituents snapshot key disappears, breadth n_trading_days
      never decreases, breadth end_date never moves backwards;
  G6  no panel's breadth was computed on a thin vendor download — roster
      coverage (share of CURRENT constituents carrying a 50-day average)
      must clear compute_breadth.MIN_ROSTER_COVERAGE_WARN. That writer
      refuses to write below its own hard floor but only WARNS in the
      band between, and the warn band is not safe to commit: on
      2026-08-08 IDP6 was published at 61.5% and changed Strategy A's
      holdings, keeping IDP6 at 6.3% and ejecting IUMS. Writing a thin
      panel beats writing a stale one; committing one does not, so the
      block belongs here rather than as a stricter floor in the writer;
  W1  Friday capture, BOTH directions. Roster behind prices (warn only):
      if EVERY deployed panel's newest snapshot walked back from the
      target Friday, the refresh almost certainly ran before iShares
      published Friday's holdings (the 2026-08-08 lesson). Prices and
      breadth are still Friday-close; the roster is one day stale, which
      is defensible — hence warn, not fail. Re-run
      `fetch_constituents --etf <sym>` later to close the gap, and see
      scripts/measure_publication_lag.py for the lag measurement.

      Prices behind roster (since 2026-08-30): each panel's cache must be
      POPULATED through the last session on that panel's own calendar on
      or before the target Friday. Short on a HOLLOW cache tail is the
      2026-08-30 batch-download failure and FAILS for traded panels (the
      claim "captured the target Friday exactly" rested on a row with no
      prices in it); short with a CLEAN tail is genuine vendor lag — the
      2026-08-22 class the G4 tail cap exists for — and warns. The two
      states are told apart by whether the cache carries index rows past
      its populated end, which is the only offline discriminator there
      is: a failed batch leaves the empty row behind, a lagging vendor
      serves nothing at all.

Verdicts: FAIL -> exit 1 (the run must not be trusted or committed as-is),
WARN -> exit 0 with a printed notice, OK -> silence beyond the summary.

Scope note: the 14 EUROPE_SUPERSECTORS_CANDIDATE panels are data-only
captures, so they are deliberately NOT held to the shared end_friday; the
deployed set is refresh_all.ETFS_ALL. Since 2026-08-08 they ARE refreshed
by refresh_all (which walks ETFS_REFRESH = deployed + candidates), so the
reason they sit outside this guard is no longer "nothing refreshes them"
— it is that a screening capture carries no deployment obligation. Keep
this reading ETFS_ALL: widening it to ETFS_REFRESH would put candidates
under the cross-panel alignment checks and fail runs for panels no
strategy trades.

Pure verdict logic lives in the check_* functions and is unit-tested in
tests/test_check_refresh_guard.py; only main() touches disk/git.

Python datetime months are 1-indexed (January = 1). All calendar
arithmetic uses pandas_market_calendars / datetime — never manual
weekday computation.

Usage:
    python scripts/check_refresh_guard.py
    python scripts/check_refresh_guard.py --end-friday 2026-08-07
    python scripts/check_refresh_guard.py --baseline-ref HEAD~1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etf_registry import (  # noqa: E402
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
    get_etf,
)
from fetch_constituents import latest_completed_friday  # noqa: E402
from refresh_all import ETFS_ALL  # noqa: E402  (single source of truth)
from session_bounds import last_completed_session_on  # noqa: E402
from compute_breadth import (  # noqa: E402  (one floor, one definition)
    MIN_ROSTER_COVERAGE_WARN,
    active_roster_at,
    priced_sessions,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

# The TRADED book versus merely-monitored panels (the G6 split, 2026-08-22):
# a defect on a panel no engine reads must not block committing a book it
# cannot move — it is warned about, named, instead of failing the run. One
# definition for every check that splits on it: G6 roster coverage, and the
# G1/W1 price-side checks since 2026-08-30.
TRADED = frozenset(UNIVERSE_ETFS) | frozenset(UNIVERSE_EUROPE_SECTORS)


def verdict(check: str, status: str, evidence: str) -> dict:
    return {"check": check, "status": status, "evidence": evidence}


# ---------------------------------------------------------------------------
# Pure verdict logic — unit-tested offline
# ---------------------------------------------------------------------------
def hollow_price_tails(price_sides: dict[str, dict] | None) -> dict[str, str]:
    """Panels whose price cache carries rows PAST the last populated session.

    The 2026-08-30 signature: an index row that exists while the roster's
    prices for it never arrived (IUFS's Friday row held 0 of 76 roster
    closes). ``price_sides`` entries come from price_cache_side; anything
    without status "ok" is someone else's WARN, not a hollow tail.
    Returns {etf: evidence fragment}. ISO date strings compare correctly
    as strings, the convention G5 already relies on.
    """
    out: dict[str, str] = {}
    for etf, side in (price_sides or {}).items():
        if not isinstance(side, dict) or side.get("status") != "ok":
            continue
        idx = side.get("index_end")
        pop = side.get("populated_end")
        if idx and (not pop or pop < idx):
            out[etf] = (f"populated to {pop or 'NO session at all'}, rows to "
                        f"{idx} ({side.get('newest_row_populated', '?')} "
                        f"roster names non-NaN on the newest row)")
    return out


def unverifiable_price_sides(price_sides: dict[str, dict] | None
                             ) -> dict[str, str]:
    """Panels whose cache could not be read — {etf: reason}."""
    return {etf: (side.get("status", "malformed")
                  if isinstance(side, dict) else "malformed")
            for etf, side in (price_sides or {}).items()
            if not isinstance(side, dict) or side.get("status") != "ok"}


def check_shared_end_friday(end_fridays: dict[str, str],
                            expected: date,
                            price_sides: dict[str, dict] | None = None,
                            ) -> list[dict]:
    """G1: one end_friday across all panels, equal to ``expected`` — and,
    when ``price_sides`` is supplied, corroborated by PRICES.

    Until 2026-08-30 this read only the constituents' end_friday stamp, a
    statement about the week the ROSTER fetch targeted. That night the
    stamp was right, every roster was right, and the price caches carried
    a Friday row with no prices in it — so "24 panels all end 2026-08-28"
    printed over a book priced to Thursday. The price leg rejects any
    end-claim resting on bare row presence: a traded panel with a hollow
    cache tail FAILS, a monitored-only one warns (the G6 split), and an
    unreadable cache warns (the caches are gitignored — off the refresh
    machine there is nothing to corroborate with, and silence would be
    the same blindness this leg exists to remove).
    """
    distinct = sorted(set(end_fridays.values()))
    out = []
    if len(distinct) != 1:
        by_value = {v: sorted(k for k, vv in end_fridays.items() if vv == v)
                    for v in distinct}
        out.append(verdict(
            "G1 shared end_friday", FAIL,
            f"panels disagree on end_friday: {by_value}"))
    else:
        got = distinct[0]
        if got != expected.isoformat():
            out.append(verdict(
                "G1 shared end_friday", FAIL,
                f"all panels end {got} but the expected target Friday is "
                f"{expected.isoformat()} — the refresh captured the wrong "
                f"week"))
        else:
            out.append(verdict(
                "G1 shared end_friday", OK,
                f"{len(end_fridays)} constituents panels all stamp {got}"))
    if price_sides is None:
        return out

    hollow = hollow_price_tails(price_sides)
    hollow_traded = {e: v for e, v in hollow.items() if e in TRADED}
    hollow_monitored = {e: v for e, v in hollow.items() if e not in TRADED}
    unknown = unverifiable_price_sides(price_sides)
    if hollow_traded:
        out.append(verdict(
            "G1 populated price tail", FAIL,
            f"HOLLOW cache tail on traded panels — the newest row(s) exist "
            f"but the roster is not priced on them, so any Friday-capture "
            f"claim resting on row presence is void (2026-08-30: the batch "
            f"download served empty Fridays while single-ticker requests "
            f"had real bars): {hollow_traded}"))
    if hollow_monitored:
        out.append(verdict(
            "G1 populated price tail", WARN,
            f"hollow cache tail, but OUTSIDE the traded book so it cannot "
            f"move it (G6 split): {hollow_monitored}"))
    if not hollow:
        n_ok = sum(1 for s in price_sides.values()
                   if isinstance(s, dict) and s.get("status") == "ok")
        if n_ok:
            out.append(verdict(
                "G1 populated price tail", OK,
                f"{n_ok} price caches populated through their newest rows"))
    if unknown:
        out.append(verdict(
            "G1 populated price tail", WARN,
            f"price side unverifiable — the hollow-tail check needs the "
            f"gitignored prices_cache parquets, which exist only on the "
            f"machine that ran the refresh: {unknown}"))
    return out


ENGINE_PRICE_CACHES = {
    "B": "asset_class_prices_cache.parquet",
    "C": "thematic_prices_cache.parquet",
}


def check_decision_sessions(frames: dict[str, "pd.DataFrame | None"],
                            calendar: str = "NYSE", freq: str = "W-MON",
                            ) -> list[dict]:
    """G7: each price-signal engine's latest rebalance was decided on the
    session its venue actually closed before it.

    Added 2026-09-03. On Friday 2026-08-28 yfinance served no bar for ten of
    thirteen sleeve-B lines and for SHY; both engine caches lost the session
    (B drops holed rows, C takes its calendar from SHY) and the 2026-08-31
    rebalance was published decided on Thursday. G1 and G4 read tails and end
    dates, the panel guard reads the panel's own index, and the cache-write
    refusal reads start, end and lost columns; an interior missing session
    passed all of them. This verdict reads the VENUE calendar. FAIL when the
    decision session is absent or unpriced for a member; WARN for an older
    scheduled session missing inside the trailing window; WARN unverifiable
    when the gitignored cache is not on this machine (the CI convention G1's
    price leg already follows).
    """
    from price_panel_guard import (  # local: keeps this module importable
        FAIL as P_FAIL, SKIP as P_SKIP, decision_session_report,
    )
    out = []
    for sleeve, frame in sorted((frames or {}).items()):
        name = f"G7 decision session {sleeve}"
        if frame is None or len(frame) == 0:
            out.append(verdict(name, WARN,
                               f"engine price cache for sleeve {sleeve} not "
                               f"readable on this machine — the decision "
                               f"session cannot be corroborated"))
            continue
        rep = decision_session_report(frame, calendar, freq,
                                      pd.Timestamp(frame.index.min()),
                                      hollow_is_fail=True)
        if rep["status"] == P_SKIP:
            out.append(verdict(name, WARN, "; ".join(rep["reasons"])))
        elif rep["status"] == P_FAIL:
            out.append(verdict(name, FAIL, "; ".join(rep["reasons"])))
        elif rep["warnings"]:
            out.append(verdict(name, WARN,
                               f"the {rep['rebalance_date']} rebalance ranks "
                               f"on {rep['expected_decision']}, present and "
                               f"priced; but " + "; ".join(rep["warnings"])))
        else:
            out.append(verdict(name, OK,
                               f"the {rep['rebalance_date']} rebalance ranks "
                               f"on {calendar} {rep['expected_decision']}, "
                               f"present and priced for every member"))
    return out


def load_engine_price_caches(data_dir: Path) -> dict[str, "pd.DataFrame | None"]:
    """The two engine caches, or None where unreadable (gitignored: absent
    off the refresh machine)."""
    frames: dict[str, "pd.DataFrame | None"] = {}
    for sleeve, fname in ENGINE_PRICE_CACHES.items():
        path = data_dir / fname
        try:
            frames[sleeve] = pd.read_parquet(path) if path.exists() else None
        except Exception:
            frames[sleeve] = None
    return frames


def check_endpoint_health(health: dict[str, str]) -> list[dict]:
    """G2: every panel's endpoint_health.status must be "ok"."""
    bad = {k: v for k, v in health.items() if v != "ok"}
    if bad:
        return [verdict("G2 endpoint health", FAIL,
                        f"panels with unhealthy transport: {bad}")]
    return [verdict("G2 endpoint health", OK,
                    f"{len(health)} panels report ok")]


def check_staleness(staleness: dict[str, str]) -> list[dict]:
    """G3: critical / no-real-fetch staleness fails; warning warns."""
    out = []
    critical = sorted(k for k, v in staleness.items()
                      if v in ("critical", "no_real_fetches"))
    warning = sorted(k for k, v in staleness.items() if v == "warning")
    if critical:
        out.append(verdict("G3 roster staleness", FAIL,
                           f"critically stale panels: {critical}"))
    elif warning:
        out.append(verdict("G3 roster staleness", WARN,
                           f"panels in the warning band: {warning}"))
    else:
        out.append(verdict("G3 roster staleness", OK,
                           f"{len(staleness)} panels fresh"))
    return out


def expected_panel_end(cal_name: str, end_friday: date) -> date:
    """Last session of ``cal_name`` on or before ``end_friday``.

    This is what a breadth panel's end_date must equal after a refresh:
    the target Friday itself on a normal week, or the prior session when
    that Friday is a holiday ON THAT ETF'S OWN CALENDAR (a Friday-holiday
    panel dated Thursday is correct, not stale — cadence rule).
    """
    cal = mcal.get_calendar(cal_name)
    # 10 calendar days comfortably spans any run of weekend + holidays
    # around a single Friday.
    sched = cal.schedule(
        start_date=(end_friday - timedelta(days=10)).isoformat(),
        end_date=end_friday.isoformat(),
    )
    if sched.empty:
        raise RuntimeError(
            f"no {cal_name} session in the 10 days up to {end_friday} — "
            f"calendar data is broken")
    return sched.index[-1].date()


def latest_admissible_panel_end(cal_name: str, now_utc: datetime) -> date | None:
    """Upper G4 bound: the last session on ``cal_name`` that has CLOSED.

    Shares compute_breadth's own definition rather than re-deriving one, so
    the guard and the writer cannot drift apart again — which is exactly how
    G4 came to fail every panel between 2026-08-15 and 2026-08-21.

    None when the calendar yields nothing in the horizon; the caller then
    drops the upper bound rather than failing, matching every other
    last_completed_session_on caller.
    """
    ts = last_completed_session_on(mcal.get_calendar(cal_name), now_utc)
    return None if ts is None else ts.date()


def is_session(cal_name: str, day: date) -> bool:
    """Is ``day`` an actual trading session on ``cal_name``?"""
    sched = mcal.get_calendar(cal_name).schedule(
        start_date=day.isoformat(), end_date=day.isoformat())
    return not sched.empty


def check_breadth_ends(breadth_ends: dict[str, str],
                       calendars: dict[str, str],
                       end_friday: date,
                       now_utc: datetime | None = None,
                       tail_caps: dict[str, dict | None] | None = None,
                       ) -> list[dict]:
    """G4: each breadth panel ends inside the band the writer may produce.

    ``now_utc`` is injectable so the band is testable; it defaults to the
    wall clock. A guard running after midnight UTC sees a later upper bound
    than the writer did, which can only LOOSEN the check — it cannot
    manufacture a false failure.
    """
    now = now_utc or datetime.now(timezone.utc)
    out = []
    bad = {}
    for etf, got in breadth_ends.items():
        cal_name = calendars.get(etf, "NYSE")
        floor = expected_panel_end(cal_name, end_friday)
        ceiling = latest_admissible_panel_end(cal_name, now)
        # HONOUR A DECLARED CAP instead of re-deriving the floor.
        #
        # The floor above asks the ROSTER how far the panel should reach. When
        # the roster leads the prices — the 21 August roster published while
        # the European constituents were priced only to the 20th — that floor
        # is unreachable, and G4 called five DEPLOYED panels TRUNCATED for
        # ending exactly where their data ends. compute_breadth now records
        # why it stopped; this reads that rather than guessing.
        #
        # Narrow on purpose: the floor drops only as far as the sessions the
        # writer says its constituents were priced for, and only when the
        # panel actually ends there. A panel shorter than its own declared cap
        # is still TRUNCATED, so genuine data loss is still caught.
        cap = (tail_caps or {}).get(etf) or {}
        priced_to = cap.get("constituents_priced_to")
        if priced_to:
            try:
                pd_date = date.fromisoformat(priced_to)
                if pd_date < floor:
                    floor = pd_date
            except (TypeError, ValueError):
                pass
        try:
            got_date = date.fromisoformat(got)
        except (TypeError, ValueError):
            bad[etf] = f"end_date {got!r} is not an ISO date"
            continue
        if not is_session(cal_name, got_date):
            bad[etf] = f"ends {got}, which is not a {cal_name} session"
        elif got_date < floor:
            bad[etf] = (f"ends {got}, TRUNCATED — must reach at least "
                        f"{floor.isoformat()}")
        elif ceiling is not None and got_date > ceiling:
            bad[etf] = (f"ends {got}, past the last completed {cal_name} "
                        f"session {ceiling.isoformat()} — partial bar")
    if bad:
        out.append(verdict("G4 breadth end dates", FAIL,
                           f"panels outside the admissible band "
                           f"[{end_friday.isoformat()} session .. last "
                           f"completed session]: {bad}"))
    else:
        out.append(verdict("G4 breadth end dates", OK,
                           f"{len(breadth_ends)} panels end on a real session "
                           f"at or after their {end_friday.isoformat()} bound "
                           f"and no later than their last completed session"))
    return out


def panel_roster_coverage(breadth: dict) -> float | None:
    """Share of CURRENT constituents carrying a 50-day average, or None.

    Prefers ``data_quality.roster_coverage_latest``, which compute_breadth
    records from 2026-08-09. Falls back to deriving it from the series,
    because every panel written before that date lacks the field and a
    check that silently skipped 23 of 24 panels would be worse than none.
    """
    dq = breadth.get("data_quality") or {}
    recorded = dq.get("roster_coverage_latest")
    if isinstance(recorded, (int, float)):
        return float(recorded)
    ser = breadth.get("series") or {}
    ma, const = ser.get("n_with_ma50") or [], ser.get("n_constituents") or []
    if not ma or not const or not const[-1]:
        return None
    return ma[-1] / const[-1]


def check_roster_coverage(coverages: dict[str, float | None],
                          floor: float) -> list[dict]:
    """G6: no DEPLOYED panel may be committed on a thin vendor download.

    compute_breadth already refuses to WRITE below its own hard floor, but
    it only warns in the band between. That band is not safe to commit: on
    2026-08-08 IDP6 was published at 61.5% coverage, which was inside the
    warn band, and it changed Strategy A's holdings — the sleeve kept IDP6
    at 6.3% and ejected IUMS entirely. A warning did not stop that reaching
    main. Writing a thin panel is tolerable because the alternative is a
    stale one; COMMITTING one is not, which is why this lives here rather
    than as a stricter floor in the writer.
    """
    # FAIL ONLY ON THE TRADED BOOK; WARN ON THE REST.
    #
    # The docstring says DEPLOYED and means it — the 2026-08-08 incident it
    # records was IDP6, a sleeve A member, changing Strategy A's holdings. But
    # ETFS_ALL carries 24 panels, five of which are not traded: the four
    # countries, whose sleeve was REJECTED (register record
    # 2026-07-02-breadth-thrust-etf-2), and IUIT, pruned 2026-05-23. A thin
    # panel outside the book cannot move the book, and on 2026-08-22 NDIA at
    # 72.7% failed the whole refresh on exactly that basis.
    #
    # The same over-broad "deployed" also mislabelled those four panels on the
    # published Data tab until it was corrected earlier the same day. One
    # definition, two places, and this is the second.
    #
    # They are still MEASURED and still reported — silence would be a
    # different error — but they cannot block a refresh of a book they are
    # not in.
    traded = TRADED
    out = []
    thin = {e: f"{c:.1%}" for e, c in coverages.items()
            if c is not None and c < floor and e in traded}
    thin_other = {e: f"{c:.1%}" for e, c in coverages.items()
                  if c is not None and c < floor and e not in traded}
    unknown = sorted(e for e, c in coverages.items() if c is None)
    if thin:
        out.append(verdict("G6 roster coverage", FAIL,
                           f"deployed panels below the {floor:.0%} floor — "
                           f"breadth computed on a thin sample can move the "
                           f"deployed book: {thin}"))
    elif thin_other:
        out.append(verdict("G6 roster coverage", WARN,
                           f"thin, but OUTSIDE the traded book so it cannot "
                           f"move it: {thin_other}"))
    else:
        out.append(verdict("G6 roster coverage", OK,
                           f"{len(coverages) - len(unknown)} panels at or "
                           f"above the {floor:.0%} floor"))
    if unknown:
        out.append(verdict("G6 roster coverage readable", WARN,
                           f"coverage indeterminable for {unknown} — "
                           f"neither the recorded field nor n_with_ma50 / "
                           f"n_constituents was usable"))
    return out


def check_no_lost_state(etf: str,
                        old_snapshot_keys: set[str],
                        new_snapshot_keys: set[str],
                        old_breadth: dict | None,
                        new_breadth: dict) -> list[dict]:
    """G5 for one panel: nothing the previous commit had may vanish."""
    out = []
    lost = sorted(old_snapshot_keys - new_snapshot_keys)
    if lost:
        out.append(verdict(
            f"G5 {etf} snapshots", FAIL,
            f"{len(lost)} snapshot(s) in HEAD are gone from the working "
            f"tree: {lost[:5]}{' ...' if len(lost) > 5 else ''}"))
    if old_breadth is not None:
        old_n = old_breadth.get("n_trading_days")
        new_n = new_breadth.get("n_trading_days")
        if isinstance(old_n, int) and isinstance(new_n, int) and new_n < old_n:
            out.append(verdict(
                f"G5 {etf} breadth length", FAIL,
                f"n_trading_days shrank {old_n} -> {new_n}: the refreshed "
                f"series silently lost sessions (2026-08-04 SPY class)"))
        old_end = old_breadth.get("end_date")
        new_end = new_breadth.get("end_date")
        if old_end and new_end and new_end < old_end:
            out.append(verdict(
                f"G5 {etf} breadth end", FAIL,
                f"end_date moved backwards {old_end} -> {new_end}"))
    return out


def check_universal_walkback(latest_actuals: dict[str, str],
                             end_friday: date,
                             price_sides: dict[str, dict] | None = None,
                             expected_ends: dict[str, str] | None = None,
                             ) -> list[dict]:
    """W1: did the refresh capture the target Friday — both directions?

    Roster behind prices (warn only, the 2026-08-08 lesson): every panel
    walking back from the target Friday means the refresh predated
    iShares' publication of Friday's holdings.

    Prices behind roster (the 2026-08-30 lesson): when ``price_sides`` and
    ``expected_ends`` are supplied, each panel's cache must be POPULATED
    through its expected session — the last session on that panel's own
    calendar on or before the target Friday, so a venue-holiday Friday is
    not called short. Short on a HOLLOW tail is a failed batch download
    wearing a capture's clothes and FAILS for traded panels; short with a
    clean tail is genuine vendor lag (the 2026-08-22 class the G4 tail cap
    exists for) and warns. Until 2026-08-30 this check read only the
    roster side and printed "captured the target Friday exactly" over a
    book priced to Thursday.

    A panel absent from ``expected_ends`` has its shortness unevaluated
    (its hollow tail is still G1's to catch); a panel whose cache is
    unreadable is covered by G1's unverifiable WARN rather than double-
    reported here.
    """
    n = len(latest_actuals)
    walked = sorted(k for k, v in latest_actuals.items()
                    if v != end_friday.isoformat())
    out: list[dict] = []
    if n and len(walked) == n:
        sample = latest_actuals[walked[0]]
        out.append(verdict(
            "W1 universal walkback", WARN,
            f"all {n} panels' newest snapshot fell back from "
            f"{end_friday.isoformat()} (e.g. to {sample}) — the refresh "
            f"likely ran before iShares published Friday. Roster is one "
            f"day stale (defensible; prices/breadth are Friday-close). "
            f"Consider re-running fetch_constituents later; see "
            f"measure_publication_lag.py."))
    elif walked:
        out.append(verdict(
            "W1 universal walkback", OK,
            f"{len(walked)}/{n} panels walked back ({walked}) — normal "
            f"holiday/data-gap territory, recorded in walkbacks arrays"))
    else:
        out.append(verdict(
            "W1 universal walkback", OK,
            f"all {n} panels' rosters captured the target Friday exactly"))
    if price_sides is None:
        return out

    hollow = hollow_price_tails(price_sides)
    short_hollow: dict[str, str] = {}
    short_clean: dict[str, str] = {}
    n_evaluated = 0
    for etf, side in price_sides.items():
        if not isinstance(side, dict) or side.get("status") != "ok":
            continue
        exp = (expected_ends or {}).get(etf)
        if not exp:
            continue
        n_evaluated += 1
        pop = side.get("populated_end")
        if pop and pop >= exp:            # ISO strings order correctly
            continue
        frag = (f"populated to {pop or 'NO session at all'} vs expected "
                f"{exp}")
        if etf in hollow:
            short_hollow[etf] = (
                f"{frag}, yet a row exists to {side.get('index_end')} with "
                f"{side.get('newest_row_populated', '?')} roster names "
                f"non-NaN")
        else:
            short_clean[etf] = frag
    sh_traded = {e: v for e, v in short_hollow.items() if e in TRADED}
    sh_monitored = {e: v for e, v in short_hollow.items() if e not in TRADED}
    if sh_traded:
        out.append(verdict(
            "W1 friday price capture", FAIL,
            f"prices for the expected session NEVER arrived on traded "
            f"panels, though the cache carries a row for it — the "
            f"2026-08-30 failure: the vendor's batch download served empty "
            f"Fridays while single-ticker requests returned real bars. The "
            f"roster stamps cannot vouch for prices: {sh_traded}. Re-run "
            f"the price fetch / compute_breadth before trusting this "
            f"refresh."))
    if sh_monitored:
        out.append(verdict(
            "W1 friday price capture", WARN,
            f"the same hollow shortfall, OUTSIDE the traded book so it "
            f"cannot move it (G6 split): {sh_monitored}"))
    if short_clean:
        out.append(verdict(
            "W1 friday price capture", WARN,
            f"constituents priced short of the expected session with a "
            f"CLEAN cache tail — genuine vendor lag, the 2026-08-22 class "
            f"the G4 tail cap exists for: {short_clean}. Prices/breadth "
            f"legitimately end earlier; re-run compute_breadth once the "
            f"vendor publishes."))
    if n_evaluated and not (short_hollow or short_clean):
        out.append(verdict(
            "W1 friday price capture", OK,
            f"{n_evaluated} panels' prices populated through their "
            f"expected sessions"))
    return out


# ---------------------------------------------------------------------------
# Disk / git access
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_committed_json(rel_path: str, ref: str = "HEAD") -> dict | None:
    """The committed version of ``rel_path`` at ``ref``, or None when the
    file is not in that ref (new panel) or git is unavailable (in which
    case the caller downgrades G5 to a WARN — a guard that cannot see its
    baseline must say so rather than silently pass)."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def latest_snapshot_actual(consts: dict) -> str | None:
    """actual_date of the newest snapshot in a constituents payload."""
    snaps = consts.get("snapshots") or {}
    if not snaps:
        return None
    newest_key = max(snaps)
    return (snaps[newest_key] or {}).get("actual_date")


def price_cache_side(cache_path: Path, roster: list[str]) -> dict:
    """Price-side facts for one panel, read from its constituent cache.

    status "ok" carries:
      index_end             newest row the cache HAS — bare presence,
                            which is exactly what must never be trusted
                            on its own (2026-08-30);
      populated_end         newest row the roster is actually PRICED on,
                            by compute_breadth.priced_sessions — the ONE
                            definition the writer's tail cap uses. None
                            when no row clears the floor;
      newest_row_populated  "<non-NaN roster closes>/<roster names held>"
                            on the index_end row, for the evidence line.

    Any other status means the price side cannot be verified HERE: the
    caches are gitignored, so off the refresh machine this returns
    "missing" and the caller warns instead of guessing.
    """
    if not roster:
        return {"status": "no roster"}
    if not cache_path.exists():
        return {"status": "missing"}
    try:
        prices = pd.read_parquet(cache_path)
    except Exception as exc:  # noqa: BLE001 — every unreadable cache is one fact
        return {"status": f"unreadable: {exc}"}
    if prices.empty:
        return {"status": "empty"}
    held = [t for t in roster if t in prices.columns]
    if not held:
        return {"status": "no roster columns in cache"}
    ok = priced_sessions(prices, roster)
    index_end = pd.Timestamp(prices.index.max())
    newest = prices.loc[prices.index.max(), held]
    if isinstance(newest, pd.DataFrame):  # duplicated index row: read the last
        newest = newest.iloc[-1]
    return {
        "status": "ok",
        "index_end": str(index_end.date()),
        "populated_end": (str(pd.Timestamp(ok.max()).date())
                          if len(ok) else None),
        "newest_row_populated": f"{int(newest.notna().sum())}/{len(held)}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert cross-panel coherence of the state a "
        "refresh_all.py run just produced; exit 1 when it must not be "
        "trusted.",
    )
    parser.add_argument("--end-friday", default=None,
                        help="expected target Friday YYYY-MM-DD (default: "
                        "the latest completed Friday per the clock)")
    parser.add_argument("--baseline-ref", default="HEAD",
                        help="git ref holding the previous committed state "
                        "for the loss check (default: HEAD)")
    args = parser.parse_args(argv)

    if args.end_friday:
        expected_friday = date.fromisoformat(args.end_friday)
    else:
        # Clock-derived, never a literal. date.today() matches the
        # convention fetch_constituents.main uses to pick its end_friday,
        # so guard and fetcher cannot disagree across a midnight boundary
        # any more than the fetcher disagrees with itself.
        expected_friday = latest_completed_friday(date.today())

    results: list[dict] = []
    end_fridays: dict[str, str] = {}
    health: dict[str, str] = {}
    staleness: dict[str, str] = {}
    tail_caps: dict[str, dict | None] = {}
    breadth_ends: dict[str, str] = {}
    calendars: dict[str, str] = {}
    coverages: dict[str, float | None] = {}
    latest_actuals: dict[str, str] = {}
    price_sides: dict[str, dict] = {}

    baseline_missing: list[str] = []
    n_baseline_checked = 0
    n_loss_failures = 0
    for etf in ETFS_ALL:
        key = etf.lower()
        consts_rel = f"data/constituents_{key}.json"
        breadth_rel = f"data/breadth_{key}.json"
        try:
            consts = load_json(DATA_DIR / f"constituents_{key}.json")
            breadth = load_json(DATA_DIR / f"breadth_{key}.json")
        except (OSError, json.JSONDecodeError) as exc:
            results.append(verdict(f"G0 {etf} readable", FAIL,
                                   f"panel unreadable: {exc}"))
            continue
        end_fridays[etf] = consts.get("end_friday", "<absent>")
        health[etf] = (consts.get("endpoint_health") or {}).get(
            "status", "<absent>")
        staleness[etf] = (consts.get("staleness") or {}).get(
            "status", "<absent>")
        breadth_ends[etf] = breadth.get("end_date", "<absent>")
        tail_caps[etf] = breadth.get("tail_cap")
        coverages[etf] = panel_roster_coverage(breadth)
        try:
            calendars[etf] = get_etf(etf).get("trading_calendar", "NYSE")
        except KeyError:
            calendars[etf] = "NYSE"
        actual = latest_snapshot_actual(consts)
        if actual:
            latest_actuals[etf] = actual

        # Price side for the G1/W1 populated-tail checks. The roster is the
        # one in force on the newest snapshot date — the same call the
        # writer's tail cap makes, so guard and writer count the same names.
        snaps = consts.get("snapshots") or {}
        try:
            snap_dates = sorted(snaps)
            roster_now = (active_roster_at(snap_dates, snaps, snap_dates[-1])
                          if snap_dates else [])
        except (KeyError, TypeError):
            roster_now = []
        price_sides[etf] = price_cache_side(
            DATA_DIR / f"prices_cache_{key}.parquet", roster_now)

        # G5 versus the committed baseline.
        old_consts = load_committed_json(consts_rel, args.baseline_ref)
        old_breadth = load_committed_json(breadth_rel, args.baseline_ref)
        if old_consts is None:
            baseline_missing.append(etf)
        else:
            n_baseline_checked += 1
            loss = check_no_lost_state(
                etf,
                set((old_consts.get("snapshots") or {}).keys()),
                set((consts.get("snapshots") or {}).keys()),
                old_breadth, breadth,
            )
            n_loss_failures += len(loss)
            results.extend(loss)

    results.extend(check_shared_end_friday(end_fridays, expected_friday,
                                           price_sides=price_sides))
    results.extend(check_endpoint_health(health))
    results.extend(check_staleness(staleness))
    results.extend(check_roster_coverage(coverages,
                                         MIN_ROSTER_COVERAGE_WARN))
    results.extend(check_breadth_ends(breadth_ends, calendars,
                                      expected_friday,
                                      tail_caps=tail_caps))
    # Each panel's expected session on its OWN calendar — a venue-holiday
    # Friday must not read as a price shortfall (the 2026-07-03 boundary).
    expected_ends = {etf: expected_panel_end(calendars.get(etf, "NYSE"),
                                             expected_friday).isoformat()
                     for etf in price_sides}
    results.extend(check_universal_walkback(latest_actuals, expected_friday,
                                            price_sides=price_sides,
                                            expected_ends=expected_ends))
    # G7 — the price-signal engines' decision session, read against the
    # venue calendar (2026-09-03; the 2026-08-28 withheld Friday).
    results.extend(check_decision_sessions(load_engine_price_caches(DATA_DIR)))
    if n_loss_failures == 0 and n_baseline_checked:
        results.append(verdict(
            "G5 no lost state", OK,
            f"{n_baseline_checked} panels checked against "
            f"{args.baseline_ref}; nothing lost"))
    if baseline_missing:
        results.append(verdict(
            "G5 baseline", WARN,
            f"no committed baseline at {args.baseline_ref} for "
            f"{baseline_missing} — loss check skipped for these panels "
            f"(new panel or git unavailable)"))

    n_fail = sum(1 for r in results if r["status"] == FAIL)
    n_warn = sum(1 for r in results if r["status"] == WARN)
    print(f"refresh guard @ {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
          f"— expected target Friday {expected_friday.isoformat()}, "
          f"{len(ETFS_ALL)} deployed panels")
    for r in results:
        print(f"  {r['status']:<4} {r['check']}: {r['evidence']}")
    print(f"\n{n_fail} FAIL, {n_warn} WARN, "
          f"{len(results) - n_fail - n_warn} ok")
    if n_fail:
        print("\n[REFRESH-GUARD] The refreshed state is incoherent. Do not "
              "commit or publish it; re-run the failing step or "
              "investigate before trusting any output of this refresh.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
