"""WS6 A3 — prove the JSON weight route reproduces the frozen CSV basis.

The A3 weight tables were built from the "Weight (%)" column of the raw iShares
holdings CSV. That endpoint began serving anti-bot HTML for UCITS and US lines
alike (measured 2026-09-09), which froze every adopted line's weights and, under
the WS6b SS6.3 STRICT ruling, reverted every line every week — a shadow that
measured nothing. ``fetch_ws6_weights.parse_holdings_weights_json`` reads the
same quantity from the product-data API's ``holdingPercent`` instead.

Substituting a second source inside a BINDING register is only admissible if it
is shown to reproduce the first, so this measures that rather than asserting it.

WHY THIS NEEDS THE NETWORK. The two caches on disk are disjoint: every
(line, date) is stored as CSV or as JSON, never both — 0 overlapping pairs
across all 11 lines when measured 2026-09-10. So the overlap has to be CREATED,
by requesting JSON for historical dates that already hold a CSV. The API serves
those (asOfDate echoes), and each fetched payload is cached under the deployed
pipeline's own name, so a re-run costs nothing and the fetches are not wasted.

What is compared, per (line, date), on the SAME date from the two formats:
  1. the ticker KEY SET, which is what joins the weight table to the membership
     snapshots — a mismatch here breaks the basket, not just a number;
  2. the per-name weight, at the CSV's published precision, where the bar is
     EXACT equality — the difference between the routes is a publication
     artefact (2 dp against 4-5 dp), not a different number, and a tolerance
     would have concealed exactly that;
  3. the renormalised top-M pool the engine actually consumes, because that is
     the quantity the register's construction reads and a difference too small
     to matter per-name could still reorder the pool.

Run:
  python scripts/prove_ws6_weight_parity.py [--per-line N] [--lines SOXX ...]
  python scripts/prove_ws6_weight_parity.py --offline   # cached pairs only
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import get_etf  # noqa: E402
from fetch_constituents import (  # noqa: E402
    RAW_DIR,
    looks_like_ishares_holdings_csv,
    parse_holdings,
    parse_holdings_json,
)
from fetch_ws6_weights import (  # noqa: E402
    _json_cache_path,
    _load_json_payload,
    parse_holdings_weights,
    parse_holdings_weights_json,
)
from single_name_impl import M_POOL, SINGLE_NAMED_LINES, load_constituents  # noqa: E402

REPORT = PROJECT_ROOT / "data_local" / "ws6" / "weight_parity_report.json"

# EXACT equality is the bar, not a tolerance. The CSV publishes Weight (%) to
# two decimals and the API publishes the same quantity to four or five; measured
# across every pair below, the CSV value IS the API value rounded to 2 dp, on
# every name. parse_holdings_weights_json therefore stores at that precision and
# the two routes must agree exactly. A tolerance would have hidden the one thing
# worth knowing here — that the difference is a publication artefact and not a
# different number. Any non-zero difference is a FAILURE and is reported as one.
WEIGHT_TOL_PCT = 0.0


def _renormalised_pool(w: dict[str, float]) -> dict[str, float]:
    """The top-M by weight, renormalised — what the A3 construction consumes."""
    top = sorted(w.items(), key=lambda kv: (-kv[1], kv[0]))[:M_POOL]
    total = sum(v for _, v in top)
    return {t: v / total for t, v in top} if total else {}


def compare_one(line: str, key: str, target, cfg: dict, offline: bool) -> dict | None:
    """Compare both routes on one (line, date). None when no pair is available."""
    symbol = cfg["symbol"]
    overrides = cfg.get("ticker_overrides", {})
    suffix = bool(cfg.get("apply_exchange_suffix", False))

    csv_path = RAW_DIR / f"{symbol}_{target.strftime('%Y%m%d')}.csv"
    if not csv_path.exists():
        return None
    body = csv_path.read_text(encoding="utf-8")
    if not looks_like_ishares_holdings_csv(body):
        return None

    if offline and not _json_cache_path(symbol, target).exists():
        return None
    got = _load_json_payload(symbol, target, cfg)
    if got is None:
        return {"line": line, "snapshot": key, "status": "json_unavailable"}
    payload = got["payload"]

    cw, cno = parse_holdings_weights(body, ticker_overrides=overrides,
                                     apply_exchange_suffix=suffix)
    jw, jno = parse_holdings_weights_json(payload, target,
                                          ticker_overrides=overrides,
                                          apply_exchange_suffix=suffix)
    if not jw and not jno:
        # asOfDate did not echo: the API has no holdings for this date.
        return {"line": line, "snapshot": key, "status": "json_no_asof_echo"}

    cm = parse_holdings(body, ticker_overrides=overrides,
                        apply_exchange_suffix=suffix)
    jm = parse_holdings_json(payload, target, ticker_overrides=overrides,
                             apply_exchange_suffix=suffix, symbol=symbol,
                             strict_exchanges=False)

    only_csv = sorted(set(cw) - set(jw))
    only_json = sorted(set(jw) - set(cw))
    shared = sorted(set(cw) & set(jw))
    diffs = {t: abs(cw[t] - jw[t]) for t in shared}
    worst = max(diffs.values()) if diffs else 0.0

    cpool, jpool = _renormalised_pool(cw), _renormalised_pool(jw)
    pool_same_set = set(cpool) == set(jpool)
    pool_worst = (max(abs(cpool[t] - jpool.get(t, 0.0)) for t in cpool)
                  if cpool else 0.0)

    return {
        "line": line, "snapshot": key, "status": "compared",
        "n_csv": len(cw), "n_json": len(jw),
        "keys_identical": not only_csv and not only_json,
        "only_csv": only_csv[:8], "only_json": only_json[:8],
        "membership_identical": sorted(cm) == sorted(jm),
        "max_weight_diff_pct": round(worst, 6),
        "exact_match": worst == 0.0,
        "sum_csv": round(sum(cw.values()), 4),
        "sum_json": round(sum(jw.values()), 4),
        "no_weight_csv": sorted(cno)[:8], "no_weight_json": sorted(jno)[:8],
        "pool_same_set": pool_same_set,
        "pool_max_diff": round(pool_worst, 8),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lines", nargs="*", default=None)
    ap.add_argument("--per-line", type=int, default=30,
                    help="snapshots sampled per line (default 30)")
    ap.add_argument("--offline", action="store_true",
                    help="compare only pairs already cached; fetch nothing")
    ap.add_argument("--seed", type=int, default=20260910)
    args = ap.parse_args()

    lines = tuple(args.lines) if args.lines else SINGLE_NAMED_LINES
    rng = random.Random(args.seed)
    rows: list[dict] = []

    for line in lines:
        cfg = get_etf(line)
        symbol = cfg["symbol"]
        snaps = load_constituents(line)["snapshots"]
        # Sample only dates that HAVE a CSV, since those are the frozen basis
        # the JSON route has to reproduce. Spread across the whole window by
        # sampling from an ordered list rather than the tail.
        havecsv = [k for k in sorted(snaps)
                   if (RAW_DIR / f"{symbol}_"
                       f"{(snaps[k].get('actual_date') or k).replace('-', '')}"
                       ".csv").exists()]
        if not havecsv:
            print(f"  {line:<5} no cached CSV — nothing to prove against")
            continue
        pick = (havecsv if args.per_line >= len(havecsv)
                else sorted(rng.sample(havecsv, args.per_line)))
        n_ok = n_bad = 0
        for key in pick:
            actual = snaps[key].get("actual_date") or key
            target = datetime.strptime(actual, "%Y-%m-%d").date()
            r = compare_one(line, key, target, cfg, args.offline)
            if r is None:
                continue
            rows.append(r)
            if r["status"] != "compared":
                continue
            if (r["keys_identical"] and r["exact_match"]
                    and r["pool_same_set"]):
                n_ok += 1
            else:
                n_bad += 1
        print(f"  {line:<5} sampled={len(pick):>3}  clean={n_ok:>3}  "
              f"MISMATCH={n_bad}")

    compared = [r for r in rows if r["status"] == "compared"]
    failures = [r for r in compared
                if not (r["keys_identical"] and r["exact_match"]
                        and r["pool_same_set"])]
    worst = max((r["max_weight_diff_pct"] for r in compared), default=None)
    pool_worst = max((r["pool_max_diff"] for r in compared), default=None)

    verdict = "PARITY PROVEN" if compared and not failures else (
        "NO PAIRS COMPARED" if not compared else "PARITY FAILED")
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": verdict,
        "pairs_compared": len(compared),
        "pairs_failed": len(failures),
        "lines": sorted({r["line"] for r in compared}),
        "date_range": [min((r["snapshot"] for r in compared), default=None),
                       max((r["snapshot"] for r in compared), default=None)],
        "weight_criterion": "exact equality at the CSV published precision",
        "max_weight_diff_pct_measured": worst,
        "max_renormalised_pool_diff_measured": pool_worst,
        "keys_identical_on_every_pair": all(r["keys_identical"] for r in compared),
        "pool_same_set_on_every_pair": all(r["pool_same_set"] for r in compared),
        "skipped": {s: sum(1 for r in rows if r["status"] == s)
                    for s in sorted({r["status"] for r in rows} - {"compared"})},
        "failures": failures[:20],
        "rows": rows,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{verdict}: {len(compared)} pair(s) compared, {len(failures)} failed")
    if compared:
        print(f"  keys identical on every pair : "
              f"{report['keys_identical_on_every_pair']}")
        print(f"  top-{M_POOL} pool set identical  : "
              f"{report['pool_same_set_on_every_pair']}")
        print(f"  max per-name weight diff     : {worst} pct "
              f"(criterion: exact)")
        print(f"  max renormalised pool diff   : {pool_worst}")
        print(f"  window                       : {report['date_range'][0]} .. "
              f"{report['date_range'][1]}")
    if report["skipped"]:
        print(f"  skipped                      : {report['skipped']}")
    print(f"  report                       : {REPORT.relative_to(PROJECT_ROOT)}")
    return 0 if verdict == "PARITY PROVEN" else 1


if __name__ == "__main__":
    sys.exit(main())
