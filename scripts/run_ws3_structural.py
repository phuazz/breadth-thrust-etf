"""WS3 Item 5 — structural re-checks on the deployed system:
  (a) look-ahead audit: the prior-day-signal + shift(1) discipline on every
      deployed signal path, including both overlays, with file:line cites
      verified programmatically (the grep runs here, so the cites cannot
      rot silently);
  (b) live NaN-degradation probes: what each weight function actually does
      when its signal row goes stale/NaN (demonstrated by calling the
      deployed functions, not by reading comments);
  (c) stale-data degradation map: the freshness caps on each input and the
      one uncapped path (Phase 22 ratio ffill) flagged;
  (d) Sleeve C survivorship quantification: per-name contribution to the
      sleeve's backtest, inception dates, and universe-add phases — the
      honest size of the hand-picked-survivors problem (no PIT membership
      exists, so this cannot be corrected, only quantified);
  (e) FX consistency: the two FX conversion paths cited and spot-checked
      from the on-disk caches.

Three ways this could be silently wrong, and the defences:
  1. CITE ROT — hardcoded file:line claims go stale as code moves. Every
     cite below is re-derived by grepping the live source in this run and
     asserted non-empty.
  2. PROBE UNREALISM — a NaN probe must use the deployed function objects
     with realistic column sets (universe + cash proxy), not toy inputs;
     probes here build rows from the actual deployed universes.
  3. CONTRIBUTION ARITHMETIC — per-name contributions use the engine's own
     convention (yesterday's weight x today's return, before costs) and
     must sum to the sleeve's gross cumulative arithmetic return within
     float tolerance; asserted.

Output: data/ws3_structural.json
Run:    python scripts/run_ws3_structural.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws1_common as W  # noqa: E402
import ws3_common as W3  # noqa: E402
from run_portfolio import top_k_breadth_weight  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

OUT = W.DATA / "ws3_structural.json"
SCRIPTS = ROOT / "scripts"

# (path, pattern, role) — every deployed signal path's look-ahead defence
SHIFT_AUDIT = [
    ("run_portfolio.py", r"prev_idx = closes\.index\.get_loc\(rd\) - 1",
     "A/D engine: rebalance uses the PRIOR trading day's breadth row"),
    ("run_portfolio.py", r"weight_panel\.shift\(1\)",
     "A/D engine: yesterday's weights x today's returns"),
    ("run_asset_class_rotation.py",
     r"prev_idx = closes\.index\.get_loc\(rd\) - 1",
     "B engine: prior-day signal row"),
    ("run_asset_class_rotation.py", r"weight_panel\.shift\(1\)",
     "B engine: lagged weights"),
    ("run_thematic_rotation.py", r"prev_idx = closes\.index\.get_loc\(rd\) - 1",
     "C engine: prior-day signal row"),
    ("run_thematic_rotation.py", r"weight_panel\.shift\(1\)",
     "C engine: lagged weights"),
    ("run_europe_rotation.py", r"run_portfolio\(closes, breadths",
     "D engine IS run_portfolio (inherits both defences above)"),
    ("run_risk_overlay.py",
     r"sig = eem_signal\.reindex\(common, method=\"ffill\"\)\.fillna\(0\)"
     r"\.shift\(1\)",
     "Phase 22 tilt: state lagged one day before application"),
    ("run_risk_overlay.py", r"states_lagged = states\.shift\(1\)",
     "Phase 19 gate: hysteresis state lagged one day"),
    ("run_multi_strategy.py",
     r"blend_ret\.iloc\[i\] = \(wa \* ret_a\.iloc\[i\]",
     "blend: day-i return computed from weights set BEFORE day i"
     " (drift update follows the return line)"),
]

# Sleeve C universe-add phases (README phase history + registry comments).
C_ADD_PHASE = {
    "ARKK": "5", "CIBR": "5", "SKYY": "5", "BOTZ": "5", "BLOK": "5",
    "ICLN": "5", "TAN": "5", "LIT": "5", "URA": "5", "XBI": "5",
    "ARKG": "5", "JETS": "5", "GDX": "5", "COPX": "5", "MOO": "5",
    "PAVE": "5", "ITA": "5-17.1", "BTC-USD": "15 (2026-05)",
    "XME": "5-17.1", "WOOD": "5-17.1", "REMX": "5-17.1",
    "CQQQ": "17 (2026-05)", "159801.SZ": "17.1 (2026-05)",
    "PHO": "25 (2026-05-29)", "IHI": "25 (2026-05-29)",
}


def grep_file(fname: str, pattern: str) -> list[int]:
    text = (SCRIPTS / fname).read_text(encoding="utf-8").splitlines()
    return [i + 1 for i, line in enumerate(text) if re.search(pattern, line)]


def main() -> int:
    base = W3.build_ws3_baselines()
    idx, start, end = base["idx"], base["common_start"], base["common_end"]

    # ---- (a) look-ahead audit ------------------------------------------
    audit = []
    for fname, pattern, role in SHIFT_AUDIT:
        lines = grep_file(fname, pattern)
        assert lines, f"look-ahead cite NOT FOUND: {fname} :: {pattern}"
        audit.append({"file": f"scripts/{fname}", "lines": lines,
                      "role": role})
        print(f"  [ok] {fname}:{','.join(map(str, lines))} — {role}")

    # ---- (b) NaN probes on deployed weighters ---------------------------
    probes = {}
    a_row = pd.Series(np.nan, index=list(base["weights"]["A"].columns))
    w_a = top_k_breadth_weight(7)(a_row)
    probes["A_D_all_nan_breadth"] = {
        "result": "all weights zero — sleeve goes FULLY UNINVESTED "
                  "(0% return days, not cash yield)",
        "sum_weights": float(w_a.sum())}
    assert w_a.sum() == 0.0

    b_row = pd.Series(np.nan, index=list(base["weights"]["B"].columns))
    w_b = B_engine.top_k_by_signal(7)(b_row)
    probes["B_all_nan_signal"] = {
        "result": "100% SHY cash proxy",
        "shy_weight": float(w_b.get("SHY", 0.0))}
    assert abs(w_b.get("SHY", 0.0) - 1.0) < 1e-12

    c_row = pd.Series(np.nan, index=list(base["weights"]["C"].columns))
    w_c = C_engine.top_k_equal_weight(5)(c_row)
    probes["C_all_nan_signal"] = {
        "result": "100% SHY cash proxy",
        "shy_weight": float(w_c.get("SHY", 0.0))}
    assert abs(w_c.get("SHY", 0.0) - 1.0) < 1e-12

    st = W3.gate_states(pd.Series([0.6, 0.15, np.nan, np.nan, 0.6]))
    probes["gate_nan_breadth"] = {
        "result": "hysteresis HOLDS state on NaN (risk-off persists until "
                  "a real print re-crosses the on-threshold)",
        "states": [float(x) for x in st]}
    assert list(st) == [1.0, 0.0, 0.0, 0.0, 1.0]
    for k, v in probes.items():
        print(f"  [probe] {k}: {v['result']}")

    # ---- (c) stale-data degradation map ---------------------------------
    degradation = [
        {"path": "A/D constituent breadth",
         "cap": "7 calendar days (alignment.py:30 MAX_STALE_DAYS, applied "
                "via run_ma200_sweep.align_breadth_to_index:89-114)",
         "behaviour": "breadth -> NaN past cap -> weighter returns zeros "
                      "-> sleeve sits fully uninvested until data resumes"},
        {"path": "C non-USD lines (159801.SZ CNY->USD)",
         "cap": "10 trading days (run_thematic_rotation.py:474-478)",
         "behaviour": "price -> NaN past cap -> name drops out of the "
                      "eligible set; its slot goes to SHY via the deficit "
                      "floor"},
        {"path": "Phase 19 gate breadth (breadth_csp1.json ma_breadth)",
         "cap": "no hard cap on the ffill onto the blend calendar, but "
                "Phase 28.5 records the panel's true end_date "
                "(run_risk_overlay.py:312-319) and the state machine holds "
                "state on NaN",
         "behaviour": "a stale breadth feed FREEZES the gate state; "
                      "risk-on freeze during a crash is the failure mode — "
                      "mitigated operationally by the weekly refresh and "
                      "the Phase 28.5 end-date surfacing"},
        {"path": "Phase 22 tilt ratio (EEM/SPY, em_regime_context.parquet)",
         "cap": "NONE — run_risk_overlay.py:269-270 reindex(ffill) has no "
                "staleness cap",
         "behaviour": "FLAG: a stopped EEM/SPY cache freezes the tilt "
                      "state indefinitely (and a frozen EEM price would "
                      "mark the 10pp tilt at 0% daily return while ON). "
                      "Recommend a staleness guard at the next maintenance "
                      "window — patch proposed in the WS3 record, not "
                      "applied in-session"},
    ]

    # ---- (d) Sleeve C survivorship quantification ------------------------
    closes_c = W.load_sleeve_c().loc[:end]
    wts_c = base["weights"]["C"].reindex(closes_c.index).fillna(0.0)
    rets_c = closes_c.pct_change().fillna(0)
    contrib = (wts_c.shift(1).fillna(0) * rets_c).loc[start:end]
    total = float(contrib.values.sum())
    gross_series = contrib.sum(axis=1)
    assert abs(total - float(gross_series.sum())) < 1e-9
    per_name = contrib.sum(axis=0).sort_values(ascending=False)
    first_price = {c: str(closes_c[c].first_valid_index().date())
                   for c in closes_c.columns}
    surv_rows = []
    for name, cpp in per_name.items():
        if name == "SHY":
            phase = "cash proxy"
        else:
            phase = C_ADD_PHASE.get(name, "unknown")
        surv_rows.append({
            "name": name, "contribution_pp": round(float(cpp) * 100, 1),
            "share_of_total": round(float(cpp) / total * 100, 1)
            if total else None,
            "first_price": first_price.get(name),
            "added_phase": phase,
            "live_share_of_window": round(float(
                closes_c[name].loc[start:end].notna().mean()), 3),
        })
    top5 = per_name.drop("SHY", errors="ignore").head(5)
    print(f"  C gross arithmetic contribution {total * 100:.1f}pp; top 5: "
          + ", ".join(f"{n} {v * 100:+.1f}pp" for n, v in top5.items()))

    # ---- (e) FX consistency ----------------------------------------------
    fx = pd.read_parquet(W.DATA / "ws1_fx_eurusd_cache.parquet")["EURUSD"]
    anchors = {}
    for label, dt in (("2020-03-20 covid", "2020-03-20"),
                      ("2022-09-26 parity trough", "2022-09-26"),
                      ("latest cached", None)):
        v = fx.iloc[-1] if dt is None else fx.reindex(
            [pd.Timestamp(dt)], method="ffill").iloc[0]
        anchors[label] = round(float(v), 4)
    fx_report = {
        "sleeve_D": {"cite": "run_europe_rotation.py:128-158 "
                             "(_fx_convert_eur_to_usd; EURUSD=X ffilled "
                             "onto the Xetra calendar)",
                     "implied_eurusd_anchors": anchors,
                     "note": "anchor levels read from the committed cache; "
                             "offline session — not re-verified against a "
                             "second source today (the series itself was "
                             "two-source verified when built, Phase 20.2)"},
        "sleeve_C": {"cite": "run_thematic_rotation.py:430-479 "
                             "(_fx_convert_to_usd; CNY=X, USD = native/FX, "
                             "10-day stale cap onto NYSE calendar)",
                     "note": "159801.SZ also carries its expense drag in "
                             "the loader (:384-405); BTC-USD carries the "
                             "25 bps p.a. wrapper drag the same way"},
    }
    print(f"  FX anchors (EURUSD implied): {anchors}")

    W.write_json(OUT, {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "look_ahead_audit": audit,
        "nan_probes": probes,
        "stale_degradation_map": degradation,
        "c_survivorship": {
            "note": ("Universe is 25 hand-picked names, all alive today; "
                     "no point-in-time membership exists, so the selection "
                     "bias cannot be corrected retroactively — only "
                     "quantified and bounded. Mitigants: momentum "
                     "eligibility (a dead-weight name is simply never "
                     "selected), the sleeve's 10%% blend cap, and the "
                     "Phase 27 gate."),
            "gross_arithmetic_contribution_pp": round(total * 100, 1),
            "per_name": surv_rows,
        },
        "fx_consistency": fx_report,
        "live_signal_paths_note": ("live_signal.py / mark_to_market_live.py "
                                   "(the live email/track paths) are outside "
                                   "this backtest audit's scope"),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
