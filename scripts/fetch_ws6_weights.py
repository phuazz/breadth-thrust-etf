"""WS6 A3 Step-0 — historical constituent Weight (%) per weekly snapshot.

Amendment A3 (KICKOFF_ws6-single-name-implementation.md §5b, signed ZH
2026-07-19) switches the WS6 basket weighting from equal weight to TRUE
snapshot weight renormalised over the selected members. This stage builds the
weight tables the engine consumes: for every single-named line and every
in-window snapshot (the harness's EXISTING snapshot dates — nothing is added
or moved), the Equity-row constituent weights parsed from the same raw iShares
holdings CSV the membership snapshot was built from.

Sources, in order. The order is load-bearing: the cached CSV is the basis every
already-published weight was built from, so trying it first reproduces those
weights byte for byte and the later routes can only ADD a date the CSV route
never had. Reversing it would silently rebase 4,836 published snapshots.
  1. RAW-CSV CACHE FIRST — data/raw_ishares/ (gitignored) retains the
     "Weight (%)" column the constituent snapshots discarded. A 2026-07-19 scan
     found ALL 4,836 in-window snapshot CSVs present.
  2. PRODUCT-DATA JSON, cache then network (added 2026-09-10). The CSV holdings
     endpoint began serving anti-bot HTML for UCITS AND US lines alike, which
     froze every line's weights from mid-2026 — and under the WS6b SS6.3 STRICT
     ruling that reverted every adopted line every week, so the shadow measured
     nothing. The product-data API the deployed membership pipeline migrated to
     keeps serving and carries the same quantity as ``holdingPercent``. It is
     read through ``parse_holdings_weights_json``, which mirrors
     ``parse_holdings_json``'s filtering exactly and stores at the CSV's
     published precision. Substituting a second source inside a binding
     register is only admissible once shown to reproduce the first:
     ``scripts/prove_ws6_weight_parity.py`` measures that on real snapshots and
     the bar is EXACT equality, not a tolerance.
  3. Network CSV, last resort — via the production
     fetch_constituents.fetch_with_retry, which is cache-first, validates
     against anti-bot HTML, retries on backoff [5, 10, 30] seconds and
     throttles >= 1.5 s (plus jitter) after every successful fetch. This
     endpoint feeds the production pipeline; politeness is non-negotiable.
     Normally walled, and kept so the route revives by itself if that lifts.
     A date that still fails is recorded and left absent — the engine carries
     the line's last known weights forward (logged there), so a partial run
     is never wasted.

Provenance travels with the table: ``source.route_by_snapshot`` names the route
each snapshot came from, so a reader can tell the frozen CSV basis from the
JSON route without re-deriving it from whatever is on disk that day.

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
from decimal import ROUND_HALF_UP, Decimal
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from etf_registry import get_etf  # noqa: E402
from fetch_constituents import (  # noqa: E402  (production fetch + parse, reused)
    RAW_DIR,
    PayloadContractError,
    _holdings_datapoints,
    fetch_product_data,
    fetch_with_retry,
    looks_like_ishares_holdings_csv,
    parse_holdings,
    parse_holdings_json,
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

# The holdings CSV publishes Weight (%) to two decimal places; the product-data
# API publishes the same quantity to four or five. Measured across a 330-snapshot
# sample spanning 2018-01-05 to 2026-07-10 and all eleven lines
# (scripts/prove_ws6_weight_parity.py), the CSV value IS the API value rounded
# HALF-UP to 2 dp, on every name — so the JSON route stores at that precision,
# with that rounding rule. See _round_like_csv: the half-up part is not a detail,
# it is the whole of the 9 disagreements the first proof run reported.
#
# Matching the CSV's precision rather than keeping the API's is deliberate, and
# it is the conservative choice rather than the accurate one: 4,836 snapshots are
# already on the 2 dp basis, and a table that silently changed precision
# part-way through would put a step into exactly the basis-point-scale
# divergence series WS6b exists to measure. The finer precision stays in the
# cached payload for anyone who later wants it.
CSV_WEIGHT_DECIMALS = 2


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


def _round_like_csv(value: float) -> float:
    """Round to the CSV's published precision the way the PUBLISHER rounds.

    Half-up, not Python's ``round``. Python rounds half to EVEN, so an exact
    half-way weight goes the other way: holdingPercent 0.435 becomes 0.43 under
    ``round`` and 0.44 in the published CSV. The parity proof found 9 such ties
    in a 330-snapshot sample across 7 lines — every failure it reported, and all
    of them exactly one unit in the last decimal place. Every one agrees under
    half-up.

    ``Decimal(repr(value))`` is deliberate: ``Decimal(0.435)`` would take the
    binary float 0.434999... and round DOWN, reintroducing the same defect.
    ``repr`` recovers the shortest decimal that round-trips, which is the number
    the publisher actually serialised.
    """
    q = Decimal(1).scaleb(-CSV_WEIGHT_DECIMALS)     # 0.01 at 2 dp
    return float(Decimal(repr(value)).quantize(q, rounding=ROUND_HALF_UP))


def parse_holdings_weights_json(payload: dict, target,
                                ticker_overrides: dict | None = None,
                                apply_exchange_suffix: bool = False
                                ) -> tuple[dict[str, float], list[str]]:
    """Equity-row (ticker -> Weight (%)) map from a product-data API payload.

    The CSV twin of this function reads the "Weight (%)" column of the raw
    holdings CSV. That endpoint began serving anti-bot HTML for UCITS AND US
    lines alike (measured 2026-09-09), while the JSON product-data API the
    deployed membership pipeline migrated to keeps serving — and carries the
    same quantity as ``holdingPercent``. This reads it, so a walled CSV route no
    longer freezes the A3 weight tables.

    Filtering mirrors ``fetch_constituents.parse_holdings_json`` row for row,
    exactly as the CSV weight parser mirrors ``parse_holdings``: the asOfDate
    echo check, Asset Class == "Equity", the "-" placeholder skip, overrides,
    dot -> dash, the ``-.`` guard and first-occurrence dedup. That is what lets
    the weight keys join the membership snapshots one for one, and the caller
    asserts the parity on every snapshot either way.

    The asOfDate echo is load-bearing, not defensive. For a weekend, holiday,
    pre-inception or future date the API silently returns the LATEST holdings
    rather than erroring, so accepting an unechoed payload would write today's
    weights onto a historical Friday — a look-ahead defect in a point-in-time
    register. An unechoed payload yields no weights, which the caller treats as
    a missing snapshot exactly as it treats the empty CSV template.
    """
    if apply_exchange_suffix:
        raise ValueError("weight parser supports US-constituent lines only "
                         "(apply_exchange_suffix must be False)")
    dps = _holdings_datapoints(payload)
    echoed = dps["asOfDate"].get("value")
    if echoed is None or str(echoed) != target.strftime("%Y%m%d"):
        return {}, []

    def col(name):
        return (dps.get(name) or {}).get("value")

    tickers, classes, pcts = col("ticker"), col("assetClass"), col("holdingPercent")
    if tickers is None:
        return {}, []
    n = len(tickers)
    for nm, v in (("assetClass", classes), ("holdingPercent", pcts)):
        if v is not None and len(v) != n:
            raise PayloadContractError(
                f"holdings column {nm!r} has {len(v)} rows, expected {n}")

    overrides = ticker_overrides or {}
    weights: dict[str, float] = {}
    no_weight: list[str] = []
    for i in range(n):
        if classes is not None and str(classes[i] or "").strip() != "Equity":
            continue
        raw = str(tickers[i] or "").strip()
        if raw in {"", "-"}:
            continue
        sym = overrides.get(raw, raw.replace(".", "-"))
        if sym is None or sym in {"", "-"} or sym.startswith("-."):
            continue
        if sym in weights or sym in no_weight:
            continue           # first occurrence wins, as in parse_holdings
        try:
            val = float(pcts[i]) if pcts is not None else float("nan")
        except (TypeError, ValueError):
            no_weight.append(sym)
            continue
        if val != val:                        # NaN: absent weight column/cell
            no_weight.append(sym)
            continue
        weights[sym] = _round_like_csv(val)
    return weights, no_weight


def _json_cache_path(symbol: str, target) -> Path:
    return RAW_DIR / f"{symbol}_{target.strftime('%Y%m%d')}.json"


def _load_json_payload(symbol: str, target, cfg: dict) -> dict | None:
    """Cache-first product-data payload, or None if it cannot be obtained.

    Writes a fetched payload into the SAME cache the deployed membership
    pipeline reads, under the same name, so the two share one cache rather than
    keeping rival copies of the same bytes.
    """
    path = _json_cache_path(symbol, target)
    if path.exists():
        try:
            return {"payload": json.loads(path.read_text(encoding="utf-8")),
                    "source": "json_cache"}
        except json.JSONDecodeError:
            pass                      # corrupt cache: re-fetch below
    try:
        payload = fetch_product_data(target, cfg)
    except Exception:                 # noqa: BLE001 — caller records the miss
        return None
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass                          # a cache write failure must not fail the run
    return {"payload": payload, "source": "json_network"}


def build_line(line: str, force: bool,
               window_end: "pd.Timestamp | str | None" = None) -> dict:
    """Build (or resume) one line's weight table. Returns the report dict.

    ``window_end`` extends the snapshot window past the frozen study end —
    required by the WS6b T3 shadow, whose live weeks need current weights.
    Default None keeps the frozen WINDOW_END (register semantics unchanged).
    Existing in-window snapshots are re-parsed from the same raw cache, so an
    extension is a superset, never a rewrite, of the frozen table.
    """
    cfg = get_etf(line)
    symbol = cfg["symbol"]
    overrides = cfg.get("ticker_overrides", {})
    apply_suffix = bool(cfg.get("apply_exchange_suffix", False))
    we = pd.Timestamp(window_end) if window_end is not None else WINDOW_END
    snaps = load_constituents(line)["snapshots"]
    in_window = [k for k in sorted(snaps) if pd.Timestamp(k) <= we]

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
    source_by_key: dict[str, str] = {}
    for key in in_window:
        actual = snaps[key].get("actual_date") or key
        target = datetime.strptime(actual, "%Y-%m-%d").date()

        # Route order is deliberate and is what keeps this a SUPERSET of the
        # frozen table rather than a restatement of it. The cached CSV is the
        # basis every existing weight was built from, so it is tried first and
        # reproduces those weights byte for byte; the JSON route can only fill
        # a date the CSV route never had. Reversing this would silently rebase
        # 4,836 already-published snapshots onto a second source.
        w = expected = no_weight = None
        src = None
        cache_path = RAW_DIR / f"{symbol}_{target.strftime('%Y%m%d')}.csv"
        if cache_path.exists():
            cached = cache_path.read_text(encoding="utf-8")
            if looks_like_ishares_holdings_csv(cached):
                w, no_weight = parse_holdings_weights(
                    cached, ticker_overrides=overrides,
                    apply_exchange_suffix=apply_suffix)
                expected = parse_holdings(cached, ticker_overrides=overrides,
                                          apply_exchange_suffix=apply_suffix)
                from_cache += 1
                src = "csv_cache"

        if w is None:
            payload = _load_json_payload(symbol, target, cfg)
            if payload is not None:
                try:
                    w, no_weight = parse_holdings_weights_json(
                        payload["payload"], target, ticker_overrides=overrides,
                        apply_exchange_suffix=apply_suffix)
                    expected = parse_holdings_json(
                        payload["payload"], target, ticker_overrides=overrides,
                        apply_exchange_suffix=apply_suffix, symbol=symbol,
                        strict_exchanges=False)
                except PayloadContractError as exc:
                    print(f"    {line} {key}: JSON payload contract changed "
                          f"({exc}) — falling through")
                    w = None
                else:
                    # An unechoed asOfDate yields no rows: the API answered with
                    # the LATEST holdings for a date it has none for. That is a
                    # miss, not an empty snapshot, and must not be stored as {}.
                    if not w and not no_weight:
                        w = None
                    else:
                        src = payload["source"]
                        if src == "json_cache":
                            from_cache += 1
                        else:
                            from_network += 1

        if w is None:
            # Last resort: the legacy CSV endpoint. Walled since 2026-09 for
            # UCITS and US lines alike, so this normally records a failure —
            # kept so the route revives by itself if the wall ever lifts.
            try:
                body = fetch_with_retry(target, cfg)
                from_network += 1
                src = "csv_network"
                w, no_weight = parse_holdings_weights(
                    body, ticker_overrides=overrides,
                    apply_exchange_suffix=apply_suffix)
                expected = parse_holdings(body, ticker_overrides=overrides,
                                          apply_exchange_suffix=apply_suffix)
            except Exception as exc:  # noqa: BLE001 — record, leave absent
                fetch_failed.append(key)
                print(f"    {line} {key}: fetch failed ({exc}) — snapshot "
                      "left absent; the engine carries weights forward")
                continue

        source_by_key[key] = src

        # Filter-parity guard: the weight parser must reproduce the production
        # membership parser's ticker list exactly on the same body. Runs on
        # BOTH routes, against that route's own membership parser, so a JSON
        # snapshot whose keys would not join the membership table is caught the
        # same way a CSV one is.
        got = list(w.keys()) + no_weight
        if sorted(got) != sorted(expected):
            anomalies["parity_mismatch"].append(
                {"snapshot": key, "source": src,
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
        "window_end": we.strftime("%Y-%m-%d"),
        "n_snapshots": len(weights_by_key),
        "source": {"from_cache": from_cache, "from_network": from_network,
                   "fetch_failed": fetch_failed,
                   # Per-snapshot provenance. A binding register has to be able
                   # to say which weights came from the frozen CSV basis and
                   # which from the JSON route adopted 2026-09-10, without
                   # re-deriving it from what happens to be on disk today.
                   "by_route": {r: sum(1 for v in source_by_key.values() if v == r)
                                for r in sorted(set(source_by_key.values()))},
                   "route_by_snapshot": source_by_key},
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
    ap.add_argument("--window-end", default=None,
                    help="ISO date; extend the snapshot window past the frozen "
                         "study end (WS6b T3 shadow use). Default: frozen.")
    args = ap.parse_args()
    lines = tuple(args.lines) if args.lines else SINGLE_NAMED_LINES
    unknown = [L for L in lines if L not in SINGLE_NAMED_LINES]
    if unknown:
        raise SystemExit(f"not WS6 single-named lines: {unknown}")

    print(f"WS6 A3 Step-0 — constituent weights ({WEIGHTING_BASIS}) "
          f"-> {WS6_WEIGHTS_DIR}")
    totals = {"from_cache": 0, "from_network": 0}
    for line in lines:
        rep = build_line(line, force=args.force, window_end=args.window_end)
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
