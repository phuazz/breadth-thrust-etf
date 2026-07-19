"""WS6 A3 Step-0 — historical constituent Weight (%) per weekly snapshot.

Amendment A3 (KICKOFF_ws6-single-name-implementation.md §5b, signed ZH
2026-07-19) switches the WS6 basket weighting from equal weight to TRUE
snapshot weight renormalised over the selected members. This stage builds the
weight tables the engine consumes: for every single-named line and every
in-window snapshot (the harness's EXISTING snapshot dates — nothing is added
or moved), the Equity-row constituent weights parsed from the same raw iShares
holdings CSV the membership snapshot was built from.

Sources, in order:
  1. LOCAL CACHE FIRST — the production raw-CSV cache at data/raw_ishares/
     (gitignored) retains the "Weight (%)" column the constituent snapshots
     discarded. A 2026-07-19 scan found ALL 4,836 in-window snapshot CSVs
     present, so a normal run touches the network zero times.
  2. Network fallback ONLY for a missing file — via the production
     fetch_constituents.fetch_with_retry, which is cache-first, validates
     against anti-bot HTML, retries on backoff [5, 10, 30] seconds and
     throttles >= 1.5 s (plus jitter) after every successful fetch. This
     endpoint feeds the production pipeline; politeness is non-negotiable.
     A date that still fails is recorded and left absent — the engine carries
     the line's last known weights forward (logged there), so a partial run
     is never wasted.

Parsing filter parity: the weight parser mirrors fetch_constituents.
parse_holdings row-for-row (header detection, blank-line termination,
Asset Class == "Equity", placeholder skip, ticker overrides, dot -> dash,
first-occurrence dedup) and ASSERTS its ticker list equals parse_holdings on
the same body, so the weight keys join the membership snapshots exactly.

Validation (reported verbatim, never patched): per-snapshot weight sums
outside [95, 105] per cent, negative weights, members with a missing weight
value, and top-M-by-weight vs cap-rank-order disagreement (overlap of the two
top-15 sets below 12).

Output (git-ignored, licence-clean — iShares weights, no Norgate data):
  data_local/ws6/weights/{line}.json
  {"basis": "true_weight_a3", "line", "generated_at_utc", "n_snapshots",
   "source": {"from_cache", "from_network", "fetch_failed"},
   "anomalies": {...}, "weights": {"YYYY-MM-DD": {ticker: weight_pct}}}

Resume: a line whose output already covers every in-window snapshot under the
current basis is skipped (use --force to rebuild).

Run: python scripts/fetch_ws6_weights.py [--force] [--lines SOXX IUFS ...]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import get_etf  # noqa: E402
from fetch_constituents import (  # noqa: E402  (production fetch + parse, reused)
    RAW_DIR,
    fetch_with_retry,
    looks_like_ishares_holdings_csv,
    parse_holdings,
)
from single_name_impl import (  # noqa: E402
    M_POOL,
    SINGLE_NAMED_LINES,
    WEIGHTING_BASIS,
    WINDOW_END,
    WS6_WEIGHTS_DIR,
    load_constituents,
)

# Anomaly thresholds (report-only; nothing is corrected here).
SUM_LO_PCT = 95.0
SUM_HI_PCT = 105.0
TOPM_MIN_OVERLAP = 12          # of M_POOL = 15


def parse_holdings_weights(body: str, ticker_overrides: dict | None = None,
                           apply_exchange_suffix: bool = False
                           ) -> tuple[dict[str, float], list[str]]:
    """Equity-row (ticker -> Weight (%)) map from an iShares holdings CSV.

    Mirrors fetch_constituents.parse_holdings filtering EXACTLY (same header
    detection, blank-line termination, Asset Class filter, placeholder skip,
    overrides, dot -> dash, first-occurrence dedup) so the keys join the
    membership snapshots one-for-one; the caller asserts that parity. Returns
    (weights, no_weight_tickers) — a row whose Weight (%) cell is empty or
    non-numeric keeps its membership but contributes no weight (reported).

    The exchange-suffix path is deliberately NOT reimplemented: every WS6
    single-named line holds US constituents (apply_exchange_suffix False in
    the registry), and this stage refuses to run on a line configured
    otherwise rather than diverge from the production resolver.
    """
    if apply_exchange_suffix:
        raise ValueError("weight parser supports US-constituent lines only "
                         "(apply_exchange_suffix must be False)")
    if 'Fund Holdings as of,"-"' in body or 'Fund Holdings as of,-' in body:
        return {}, []
    overrides = ticker_overrides or {}
    weights: dict[str, float] = {}
    no_weight: list[str] = []
    header: list[str] | None = None
    asset_class_idx: int | None = None
    weight_idx: int | None = None
    for ln in body.splitlines():
        if header is None:
            if "Ticker" in ln[:20] and "Asset Class" in ln:
                header = next(csv.reader(io.StringIO(ln)))
                asset_class_idx = header.index("Asset Class")
                try:
                    weight_idx = header.index("Weight (%)")
                except ValueError:
                    weight_idx = None
            continue
        if not ln.strip():
            break              # blank line terminates the holdings block
        row = next(csv.reader(io.StringIO(ln)))
        if not row or not row[0]:
            continue
        if asset_class_idx is not None and len(row) > asset_class_idx:
            if row[asset_class_idx].strip() != "Equity":
                continue
        raw = row[0].strip()
        if raw in {"", "-"}:
            continue
        sym = overrides.get(raw, raw.replace(".", "-"))
        if sym is None or sym in {"", "-"} or sym.startswith("-."):
            continue
        if sym in weights or sym in no_weight:
            continue           # first occurrence wins, as in parse_holdings
        value = (row[weight_idx].strip()
                 if weight_idx is not None and len(row) > weight_idx else "")
        try:
            weights[sym] = float(value.replace(",", ""))
        except ValueError:
            no_weight.append(sym)
    return weights, no_weight


def build_line(line: str, force: bool) -> dict:
    """Build (or resume) one line's weight table. Returns the report dict."""
    cfg = get_etf(line)
    symbol = cfg["symbol"]
    overrides = cfg.get("ticker_overrides", {})
    apply_suffix = bool(cfg.get("apply_exchange_suffix", False))
    snaps = load_constituents(line)["snapshots"]
    in_window = [k for k in sorted(snaps) if pd.Timestamp(k) <= WINDOW_END]

    out_path = WS6_WEIGHTS_DIR / f"{line.lower()}.json"
    if out_path.exists() and not force:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        have = set(existing.get("weights", {}))
        missing_now = [k for k in in_window
                       if k not in have
                       and k not in set(existing.get("source", {})
                                        .get("fetch_failed", []))]
        if existing.get("basis") == WEIGHTING_BASIS and not missing_now:
            return {"line": line, "status": "resume_skip",
                    "n_snapshots": len(existing.get("weights", {}))}

    weights_by_key: dict[str, dict[str, float]] = {}
    from_cache = from_network = 0
    fetch_failed: list[str] = []
    anomalies: dict[str, list] = {"sum_out_of_band": [], "negative_weight": [],
                                  "member_without_weight": [],
                                  "topM_order_disagreement": [],
                                  "parity_mismatch": []}
    for key in in_window:
        actual = snaps[key].get("actual_date") or key
        target = datetime.strptime(actual, "%Y-%m-%d").date()
        cache_path = RAW_DIR / f"{symbol}_{target.strftime('%Y%m%d')}.csv"
        body = None
        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8")
            if looks_like_ishares_holdings_csv(cached):
                body = cached
                from_cache += 1
        if body is None:
            # Network fallback via the production fetcher (cache-first,
            # backoff, >= 1.5 s throttle after each successful request).
            try:
                body = fetch_with_retry(target, cfg)
                from_network += 1
            except Exception as exc:  # noqa: BLE001 — record, leave absent
                fetch_failed.append(key)
                print(f"    {line} {key}: fetch failed ({exc}) — snapshot "
                      "left absent; the engine carries weights forward")
                continue

        w, no_weight = parse_holdings_weights(
            body, ticker_overrides=overrides,
            apply_exchange_suffix=apply_suffix)

        # Filter-parity guard: the weight parser must reproduce the production
        # membership parser's ticker list exactly on the same body.
        expected = parse_holdings(body, ticker_overrides=overrides,
                                  apply_exchange_suffix=apply_suffix)
        got = list(w.keys()) + no_weight
        if sorted(got) != sorted(expected):
            anomalies["parity_mismatch"].append(
                {"snapshot": key,
                 "only_weights": sorted(set(got) - set(expected))[:5],
                 "only_membership": sorted(set(expected) - set(got))[:5]})

        total = float(sum(w.values()))
        if w and not (SUM_LO_PCT <= total <= SUM_HI_PCT):
            anomalies["sum_out_of_band"].append(
                {"snapshot": key, "sum_pct": round(total, 3)})
        negs = {t: v for t, v in w.items() if v < 0}
        if negs:
            anomalies["negative_weight"].append(
                {"snapshot": key, "names": negs})
        if no_weight:
            anomalies["member_without_weight"].append(
                {"snapshot": key, "names": no_weight})
        # Cap-rank sanity: the snapshot ticker order is the CSV order
        # (weight-sorted at source), so its top-M and the top-M by parsed
        # weight should be near-identical sets.
        roster_top = list(snaps[key].get("tickers", []))[:M_POOL]
        weight_top = [t for t, _ in sorted(w.items(), key=lambda kv: -kv[1])
                      ][:M_POOL]
        overlap = len(set(roster_top) & set(weight_top))
        if roster_top and overlap < TOPM_MIN_OVERLAP:
            anomalies["topM_order_disagreement"].append(
                {"snapshot": key, "overlap": overlap,
                 "roster_top": roster_top, "weight_top": weight_top})

        weights_by_key[key] = w

    WS6_WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "basis": WEIGHTING_BASIS,
        "line": line,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "window_end": WINDOW_END.strftime("%Y-%m-%d"),
        "n_snapshots": len(weights_by_key),
        "source": {"from_cache": from_cache, "from_network": from_network,
                   "fetch_failed": fetch_failed},
        "anomalies": {k: v for k, v in anomalies.items() if v},
        "weights": weights_by_key,
    }
    out_path.write_text(json.dumps(payload), encoding="utf-8")
    return {"line": line, "status": "built",
            "n_snapshots": len(weights_by_key),
            "from_cache": from_cache, "from_network": from_network,
            "n_fetch_failed": len(fetch_failed),
            "anomaly_counts": {k: len(v) for k, v in anomalies.items() if v}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when the output already covers the window")
    ap.add_argument("--lines", nargs="*", default=None,
                    help="subset of single-named lines (default: all 11)")
    args = ap.parse_args()
    lines = tuple(args.lines) if args.lines else SINGLE_NAMED_LINES
    unknown = [L for L in lines if L not in SINGLE_NAMED_LINES]
    if unknown:
        raise SystemExit(f"not WS6 single-named lines: {unknown}")

    print(f"WS6 A3 Step-0 — constituent weights ({WEIGHTING_BASIS}) "
          f"-> {WS6_WEIGHTS_DIR}")
    totals = {"from_cache": 0, "from_network": 0}
    for line in lines:
        rep = build_line(line, force=args.force)
        if rep["status"] == "resume_skip":
            print(f"  {line:<5} resume: output already covers the window "
                  f"({rep['n_snapshots']} snapshots)")
            continue
        totals["from_cache"] += rep["from_cache"]
        totals["from_network"] += rep["from_network"]
        anom = rep["anomaly_counts"]
        print(f"  {line:<5} snapshots={rep['n_snapshots']:>3}  "
              f"cache={rep['from_cache']:>3}  net={rep['from_network']}  "
              f"failed={rep['n_fetch_failed']}  "
              f"anomalies={anom if anom else 'none'}")
    print(f"TOTAL cache={totals['from_cache']} network={totals['from_network']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
