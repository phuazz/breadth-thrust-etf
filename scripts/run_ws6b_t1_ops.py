"""WS6b T1 — ops assessment runner (kickoff §2 item 2).

Reads the T1 mechanics caches under ``data_local/ws6b/`` (git-ignored,
Norgate-derived) plus the WS6 resolver tables in ``single_name_impl``, runs
the ``ws6b_ops`` classifiers, and writes
``data_local/ws6b/t1_ops_assessment.json``.

Read-only with respect to every deployed script and artefact.

Run:  python scripts/run_ws6b_t1_ops.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import single_name_impl as sni  # noqa: E402
from ws6b_ops import (  # noqa: E402
    capital_structure_events,
    death_events,
    held_mask,
    operator_time_model,
    rename_candidates,
    special_distribution_events,
    weekly_order_stats,
)

OUT_DIR = PROJECT_ROOT / "data_local" / "ws6b"
OUT_PATH = OUT_DIR / "t1_ops_assessment.json"


def main() -> int:
    mech = json.loads((OUT_DIR / "book_mechanics.json").read_text(
        encoding="utf-8"))
    eligible = pd.Timestamp(mech["window"]["eligible"])
    book = pd.read_parquet(OUT_DIR / "book_i0_partial5.parquet")
    tr_i0 = pd.read_parquet(OUT_DIR / "trades_i0_partial5.parquet")
    tr_e0 = pd.read_parquet(OUT_DIR / "trades_e0.parquet")
    unadj = pd.read_parquet(OUT_DIR / "prices_unadjusted.parquet")
    cap = pd.read_parquet(OUT_DIR / "prices_capital_close.parquet")
    capspec = pd.read_parquet(OUT_DIR / "prices_capitalspecial_close.parquet")

    etf_cols = set(sni.SINGLE_NAMED_LINES) | set(sni.BROAD_SLICES)
    names = [c for c in book.columns if c not in etf_cols]
    held = held_mask(book, names, eligible)
    end = book.index.max()
    years = float((end - eligible).days) / 365.25

    # Data smell, reported verbatim rather than patched: the special-dividend
    # wedge needs both adjustment bases per name.
    missing_capspec = sorted(set(names) - set(capspec.columns))
    if missing_capspec:
        print(f"DATA SMELL: {len(missing_capspec)} held names missing from "
              f"the CAPITALSPECIAL panel: {missing_capspec[:10]}"
              f"{' ...' if len(missing_capspec) > 10 else ''}")

    deaths = death_events(held, unadj)
    held_deaths = [d for d in deaths if d["held_at_death"]]
    specials = special_distribution_events(cap, capspec, held)
    spin_scale = [e for e in specials if e["spin_off_scale"]]
    small_specials = [e for e in specials if not e["spin_off_scale"]]
    cap_events = capital_structure_events(unadj, cap, held)
    renames = rename_candidates(sni.INSTRUMENT_RENAMES, sni.KNOWN_RENAMES,
                                set(c for c in held.columns if held[c].any()))

    touch_per_year = (len(held_deaths) + len(spin_scale)
                      + len(cap_events)) / years

    stats_i0 = weekly_order_stats(tr_i0)
    stats_e0 = weekly_order_stats(tr_e0)
    time_model = operator_time_model(stats_i0["orders_median"],
                                     stats_i0["orders_p90"], touch_per_year)

    out = {
        "_README": ("WS6b T1 ops assessment (kickoff §2 item 2). Event counts "
                    "are read off the WS6 resolver-resolved caches and are "
                    "exact for the classes defined in ws6b_ops; every minutes "
                    "figure is an ESTIMATE, marked, and superseded by "
                    "operator time MEASURED during the shadow (bar (c))."),
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "registration": "KICKOFF_ws6b-unscreened-replication.md (BINDING)",
        "adoption_set": "PARTIAL-5",
        "window": {"eligible": str(eligible.date()), "end": str(end.date()),
                   "years": round(years, 3)},
        "corporate_actions": {
            "deaths": {
                "events": deaths,
                "held_at_death": len(held_deaths),
                "not_held_at_death": len(deaths) - len(held_deaths),
                "held_per_year": round(len(held_deaths) / years, 3),
            },
            "special_distributions_held": {
                "spin_off_scale_events": spin_scale,
                "spin_off_scale_count": len(spin_scale),
                "spin_off_scale_per_year": round(len(spin_scale) / years, 3),
                "small_cash_count": len(small_specials),
                "small_cash_per_year": round(len(small_specials) / years, 2),
                "note": ("Norgate cannot split cash specials from stock "
                         "spin-offs; the size split at 2% of price stands in. "
                         "Small specials are cash landing in the account — "
                         "no operator action."),
            },
            "capital_structure_held": {
                "events": cap_events,
                "count": len(cap_events),
                "per_year": round(len(cap_events) / years, 3),
                "note": ("Broker adjusts positions automatically (fractional "
                         "shares); operator action is verification only."),
            },
            "renames_upper_bound": {
                "candidates": renames,
                "count": len(renames),
                "note": ("Resolver entries whose continuing instrument the "
                         "book ever held. Event dates live in the table "
                         "comments, not machine fields, so this is an upper "
                         "bound; renames re-symbol automatically at the "
                         "broker."),
            },
            "operator_touch_events_per_year": round(touch_per_year, 2),
            "operator_touch_definition": ("held deaths + spin-off-scale "
                                          "specials + capital-structure "
                                          "events; small cash specials and "
                                          "renames excluded"),
        },
        "weekly_order_stats": {"I0_PARTIAL5": stats_i0, "E0": stats_e0},
        "operator_time_estimate": time_model,
        "broker_mechanics": {
            "fractional": ("§4 assumption, defaulted at sign-off: IBKR, USD "
                           "base, fractional available for US names. The "
                           "four LSE-listed UCITS legs trade whole units — "
                           "unchanged from today's E0 practice. Whether the "
                           "per-order minimum applies to fractional orders "
                           "is UNRESOLVED (data/ws6b_params.json "
                           "ibkr_fractional_us_stocks.min_order) and brackets "
                           "minimum viable NAV."),
            "order_staging": ("W-FRI close discipline, t-1 snapshot reads: "
                              "weights are computable before the session, "
                              "staged via the IBKR basket trader as "
                              "market-on-close orders, reconciled after the "
                              "close."),
            "snapshot_capture": ("The weekly iShares snapshot pull is already "
                                 "produced by the deployed pipeline; the "
                                 "basket book adds zero incremental fetch "
                                 "load."),
        },
        "ops_budget": {
            "signed_min_per_week": 30.0,
            "typical_within": time_model["typical_within_budget"],
            "p90_within": time_model["p90_within_budget"],
        },
        "data_smells": {"missing_from_capitalspecial_panel": missing_capspec},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(PROJECT_ROOT)}")

    print(f"\nwindow {out['window']['eligible']} .. {out['window']['end']} "
          f"({out['window']['years']} y)")
    print(f"deaths: {len(deaths)} total, {len(held_deaths)} held at death "
          f"({out['corporate_actions']['deaths']['held_per_year']}/y)")
    for d in deaths:
        print(f"  {'HELD ' if d['held_at_death'] else 'not held'}  "
              f"{d['name']:<14} last print {d['last_price_date']}")
    print(f"spin-off-scale specials while held: {len(spin_scale)}")
    for e in spin_scale:
        print(f"  {e['name']:<8} {e['date']} "
              f"{e['distribution_frac_of_price']:+.3%}")
    print(f"capital-structure events while held: {len(cap_events)}")
    for e in cap_events:
        print(f"  {e['name']:<8} {e['date']} factor {e['factor']}")
    print(f"rename candidates (upper bound): "
          f"{[r['snapshot_ticker'] + '->' + r['instrument'] for r in renames]}")
    print(f"\noperator-touch events/year: {touch_per_year:.2f}")
    print(f"orders/week I0 median {stats_i0['orders_median']:.0f} "
          f"p90 {stats_i0['orders_p90']:.0f} max {stats_i0['orders_max']} "
          f"(inception {stats_i0['inception_orders']}); "
          f"E0 median {stats_e0['orders_median']:.0f}")
    print(f"estimated minutes/week: typical "
          f"{time_model['typical_plus_ca_min']} | p90 "
          f"{time_model['p90_plus_ca_min']} | budget "
          f"{time_model['budget_min_per_week']} — typical within: "
          f"{time_model['typical_within_budget']}, p90 within: "
          f"{time_model['p90_within_budget']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
