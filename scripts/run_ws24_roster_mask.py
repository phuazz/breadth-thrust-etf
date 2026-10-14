"""WS24 runner — pinned snapshot, integrity gates, arms, descriptives.

Registration: KICKOFF_ws24-pit-roster-mask.md, countersigned 2026-10-14,
frozen at 04607653 (on main as 07889864, merge c1bb52e8). The arithmetic of
the arms lives in scripts/ws24_roster_mask.py; this module owns the inputs,
the engines and the output, and implements §4, §5 and §7 and nothing else.

Two subcommands
---------------
  snapshot --out DIR [--source DIR]
      Pins EVERY input the scoring run reads (§5.3): the 19 constituent
      caches, roster files and breadth_<etf>.json files, the 19 proxy OHLC
      caches, multi_strategy.json (sleeves B and C as published, plus the
      blend calendar), risk_overlay.json, gate_states_norgate.json,
      breadth_csp1.json, the SHY fallback cache, em_regime_context.parquet,
      and the raw EURUSD=X series fetched ONCE with the deployed converter's
      own request. Runs the G4 identity scan against Norgate (local service
      required) on the 14 US panels. Writes manifest.json and SHA256SUMS.
      Refuses inside a refresh window, while refresh_all.py or
      scheduled_refresh.py is live, on an existing --out, or when any panel's
      breadth file was built from another roster (RosterMismatch).

  run --snapshot DIR [--smoke]
      Offline. Verifies SHA256SUMS, asserts both staging flags unset, makes
      every download raise except the pinned EURUSD series, then runs, in
      order and kill-on-contact: G0 parity, G1 roster reconciliation, G2
      reconciliation to the 2026-10-14 finding, G4 (read from the snapshot).
      A failure writes a HOLD payload and exits 2 before any performance
      figure is computed. Then the arms M0, M0s, M1 and M2 on sleeves A and D
      at 1x and 2x cost, the ungated and deployed blends, the §4 decision,
      D1-D3 and the wiring check. Output data/ws24_roster_mask.json.

Sleeves B and C, the gate states and the tilt series do not depend on any
arm, so they are read once from the snapshot: B and C as published in
multi_strategy.json, the overlay through run_risk_overlay's own functions.
The deployed blend for an arm is run_risk_overlay._build_eem_tilted_blend on
that arm's A and D, then the Phase 19 gate formula of run_risk_overlay.main
(lines 650-656 and 701-705), unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import ws24_roster_mask as R  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "ws24_roster_mask.json"
SCHED_DATA = Path(r"C:\dev\breadth-thrust-etf-sched\data")

FREEZE_COMMIT = "04607653"
MAIN_COMMIT = "07889864"
REGISTRATION = "KICKOFF_ws24-pit-roster-mask.md"
COMMON_START = pd.Timestamp("2018-11-08")
SPLIT_DATE = pd.Timestamp("2022-09-08")
RESTATEMENT_BAND = 0.01          # §4, O1: deployed blend and each sleeve
K_A_REGISTERED, K_D_REGISTERED = 7, 3
STAGING_FLAGS = ("BTE_APPLY_STAGED_ROSTER", "BTE_C_BTC_BASIS")

# G2: the 2026-10-14 single-session finding, as recorded in §2. Breadth
# figures are fractions; the tolerance is 0.1pp.
G2_SESSION = pd.Timestamp("2026-10-12")
G2_ROSTER = "2026-10-09"
G2_TOL = 0.001
G2_EXPECTED = {
    "levels": {"SOXX": {"unmasked": 0.708, "masked": 0.857}},
    "diffs_unmasked_minus_masked": {"SOXX": -0.149, "IUCD": 0.085,
                                    "IUMS": -0.083, "CNDX": -0.057},
    "priced_not_on_roster": {"IUFS": 22, "IUCS": 8, "IUMS": 9, "CSP1": 139},
    "unmasked_selection": ["CNDX", "CSP1", "IUCS", "IUES", "IUFS", "IUHC",
                           "SOXX"],
    "dropped_by_mask": "IUCS",
}

SHARED_FILES = ["multi_strategy.json", "risk_overlay.json",
                "gate_states_norgate.json", "breadth_csp1.json",
                "asset_class_prices_cache.parquet",
                "em_regime_context.parquet"]
FX_FILE = "fx_eurusd_raw.parquet"
G4_FILE = "g4_identity.json"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def refresh_processes() -> list[str]:
    """Command lines of live refresh processes. Fails closed: a query error
    raises rather than reading as 'none running'."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "ForEach-Object { $_.CommandLine }")
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"cannot list processes: {res.stderr.strip()}")
    return [ln for ln in res.stdout.splitlines()
            if "refresh_all.py" in ln or "scheduled_refresh.py" in ln]


def guard_machine_load() -> dict:
    now = datetime.now(ZoneInfo("Asia/Singapore"))
    if R.refresh_window_open(now):
        raise SystemExit(f"refused: {now:%a %Y-%m-%d %H:%M} SGT is inside a "
                         "BreadthThrust refresh window (§7)")
    live = refresh_processes()
    if live:
        raise SystemExit(f"refused: refresh process live: {live}")
    return {"checked_at_sgt": now.strftime("%Y-%m-%d %a %H:%M"),
            "refresh_processes": 0}


def assert_staging_flags_unset() -> dict:
    seen = {f: os.environ.get(f) for f in STAGING_FLAGS}
    bad = {f: v for f, v in seen.items() if v not in (None, "", "0")}
    if bad:
        raise SystemExit(f"refused: staging flag(s) set: {bad} (§5.3)")
    return {f: "unset" for f in STAGING_FLAGS}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_checksums(snap: Path) -> int:
    lines = (snap / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    for ln in lines:
        digest, name = ln.split("  ", 1)
        if sha256(snap / name) != digest:
            raise SystemExit(f"refused: checksum mismatch on {name}")
    listed = {ln.split("  ", 1)[1] for ln in lines}
    extra = {p.name for p in snap.iterdir()} - listed - {"SHA256SUMS"}
    if extra:
        raise SystemExit(f"refused: unlisted files in snapshot: {sorted(extra)}")
    return len(lines)


def install_network_guard(fx_raw: pd.DataFrame) -> None:
    """Every download raises except the pinned EURUSD=X request, which is
    served from the snapshot so the DEPLOYED converter's alignment runs."""
    import yfinance

    def pinned_download(tickers, *a, **k):
        if tickers == "EURUSD=X":
            return fx_raw.copy()
        raise RuntimeError(f"network refused in WS24 run: yfinance {tickers}")

    yfinance.download = pinned_download
    import backtest
    import run_asset_class_rotation as B
    import run_thematic_rotation as C

    def refuse(*a, **k):
        raise RuntimeError("network refused in WS24 run")

    backtest.download_soxx_ohlc = refuse
    B.download_prices = refuse
    C.download_prices = refuse


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def universe() -> dict:
    from etf_registry import get_etf, UNIVERSE_ETFS, UNIVERSE_EUROPE_SECTORS
    proxy = {e: (get_etf(e).get("yfinance_trading_proxy") or e)
             for e in list(UNIVERSE_ETFS) + list(UNIVERSE_EUROPE_SECTORS)}
    return {"A": list(UNIVERSE_ETFS), "D": list(UNIVERSE_EUROPE_SECTORS),
            "proxy": proxy}


def panel_files(etf: str, proxy: str) -> list[str]:
    e = etf.lower()
    return [f"constituents_{e}.json", f"prices_cache_{e}.parquet",
            f"breadth_{e}.json", f"{proxy.lower()}_ohlc_cache.parquet"]


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def _eur_closes(snap: Path, uni: dict) -> pd.DataFrame:
    out = {}
    for e in uni["D"]:
        o = pd.read_parquet(snap / f"{uni['proxy'][e].lower()}_ohlc_cache.parquet")
        c = o["Close"].astype(float)
        out[e] = c[~c.index.duplicated(keep="first")].sort_index()
    return pd.DataFrame(out).sort_index()


def fetch_fx_raw(closes_eur: pd.DataFrame) -> pd.DataFrame:
    """The deployed converter's own request (run_europe_rotation.py:174)."""
    import yfinance as yf
    raw = yf.download("EURUSD=X",
                      start=closes_eur.index.min().strftime("%Y-%m-%d"),
                      end=(closes_eur.index.max() + pd.Timedelta(days=5))
                      .strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False, threads=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    if raw.empty or "Close" not in raw.columns:
        raise SystemExit("refused: EURUSD=X fetch returned no Close")
    return raw[["Close"]].astype(float)


def _norgate_close(sym: str, cache: dict) -> pd.Series | None:
    if sym in cache:
        return cache[sym]
    import norgatedata
    from norgate_prices import _ADJUSTMENT
    try:
        df = norgatedata.price_timeseries(
            sym, stock_price_adjustment_setting=getattr(
                norgatedata.StockPriceAdjustmentType, _ADJUSTMENT),
            start_date="2017-01-01", end_date=date.today().isoformat(),
            format="pandas-dataframe")
        s = df["Close"].astype(float)
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        s = s.dropna()
    except Exception:
        s = None
    cache[sym] = s
    return s


def g4_scan(snap: Path, etf: str, ncache: dict) -> dict:
    """Identity of every (roster symbol, membership run) cell on one panel.

    Within a run the dated resolver is asked at every snapshot key, so a run
    that crosses a reuse or a rename splits into segments by Norgate symbol.
    A cell spans its first key to seven days after its last, which covers the
    sessions the strict rule counts it on.
    """
    import norgate_symbols as NS
    roster = json.loads((snap / f"constituents_{etf.lower()}.json")
                        .read_text(encoding="utf-8"))
    keys = sorted(roster["snapshots"])
    smap = roster["snapshots"]
    px = pd.read_parquet(snap / f"prices_cache_{etf.lower()}.parquet")
    symbols = sorted({t for k in keys for t in smap[k]["tickers"]})
    cells = []
    for t in symbols:
        col = R.column_for(t, px.columns)
        if col is None:
            continue
        for k0, k1 in R.membership_runs(keys, smap, t):
            run = keys[keys.index(k0): keys.index(k1) + 1]
            segs: list[list] = []
            for k in run:
                sym = NS.resolve(t, date.fromisoformat(k))
                if segs and segs[-1][0] == sym:
                    segs[-1][2] = k
                else:
                    segs.append([sym, k, k])
            for sym, s0, s1 in segs:
                lo = pd.Timestamp(s0)
                hi = pd.Timestamp(s1) + pd.Timedelta(days=7)
                if sym is None:
                    res = {"status": "unverified", "n": 0, "corr": None,
                           "reason": "no Norgate symbol on these dates"}
                else:
                    ref = _norgate_close(sym, ncache)
                    if ref is None:
                        res = {"status": "unverified", "n": 0, "corr": None,
                               "reason": "Norgate series unavailable"}
                    else:
                        res = R.identity_test(px[col].loc[lo:hi],
                                              ref.loc[lo:hi])
                cells.append({"symbol": t, "column": col,
                              "norgate_symbol": sym,
                              "start": lo.strftime("%Y-%m-%d"),
                              "end": hi.strftime("%Y-%m-%d"), **res})
    counts = {s: sum(c["status"] == s for c in cells)
              for s in ("pass", "fail", "unverified")}
    return {"panel": etf, "n_cells": len(cells), "counts": counts,
            "fail": [c for c in cells if c["status"] == "fail"],
            "unverified": [c for c in cells if c["status"] == "unverified"]}


def cmd_snapshot(args) -> int:
    load = guard_machine_load()
    flags = assert_staging_flags_unset()
    src, out = Path(args.source), Path(args.out)
    if out.exists():
        raise SystemExit(f"refused: {out} exists; a snapshot is written once")
    uni = universe()
    names = list(SHARED_FILES)
    for e in uni["A"] + uni["D"]:
        names += panel_files(e, uni["proxy"][e])
    missing = [n for n in names if not (src / n).exists()]
    if missing:
        raise SystemExit(f"refused: missing in {src}: {missing}")
    out.mkdir(parents=True)
    for n in names:
        shutil.copy2(src / n, out / n)

    from validated_breadth import validated_end
    bounds = {e: str(validated_end(out, e).date())
              for e in uni["A"] + uni["D"]}            # raises RosterMismatch

    fx = fetch_fx_raw(_eur_closes(out, uni))
    fx.to_parquet(out / FX_FILE)

    import norgate_symbols as NS
    NS._nd()                                           # raises if unavailable
    ncache: dict = {}
    g4 = {e: g4_scan(out, e, ncache) for e in uni["A"]}
    for e, r in g4.items():
        print(f"  G4 {e:5s} cells {r['n_cells']:5d}  {r['counts']}")
    (out / G4_FILE).write_text(json.dumps(
        {"rule": {"min_corr": R.IDENTITY_MIN_CORR,
                  "min_obs": R.IDENTITY_MIN_OBS},
         "scope": ("the 14 US panels; Norgate carries no European product, so "
                   "sleeve D's five panels have no dated resolver"),
         "panels": g4}, indent=1), encoding="utf-8")

    git = subprocess.run(["git", "-C", str(src.parent), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    manifest = {
        "written_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "source": str(src), "source_head": git.stdout.strip() or None,
        "registration": REGISTRATION, "freeze_commit": FREEZE_COMMIT,
        "machine_load": load, "staging_flags": flags,
        "validated_end": bounds,
        "fx_range": [str(fx.index.min().date()), str(fx.index.max().date())],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1),
                                       encoding="utf-8")
    files = sorted(p.name for p in out.iterdir())
    (out / "SHA256SUMS").write_text(
        "".join(f"{sha256(out / n)}  {n}\n" for n in files), encoding="utf-8")
    print(f"snapshot written: {out} ({len(files)} files)")
    return 0


# ---------------------------------------------------------------------------
# run — inputs
# ---------------------------------------------------------------------------

class Inputs:
    def __init__(self, snap: Path, uni: dict):
        from compute_breadth import ERA_BARRIERS
        from validated_breadth import validated_end
        self.snap, self.uni = snap, uni
        self.barriers = dict(ERA_BARRIERS)
        self.cons, self.keys, self.smap, self.bound = {}, {}, {}, {}
        self.n50, self.proxy_close = {}, {}
        for e in uni["A"] + uni["D"]:
            low = e.lower()
            self.cons[e] = pd.read_parquet(snap / f"prices_cache_{low}.parquet")
            ro = json.loads((snap / f"constituents_{low}.json")
                            .read_text(encoding="utf-8"))
            self.keys[e] = sorted(ro["snapshots"])
            self.smap[e] = ro["snapshots"]
            self.bound[e] = validated_end(snap, e)
            br = json.loads((snap / f"breadth_{low}.json")
                            .read_text(encoding="utf-8"))["series"]
            self.n50[e] = pd.Series(br["n_with_price"],
                                    index=pd.to_datetime(br["dates"]))
            o = pd.read_parquet(
                snap / f"{uni['proxy'][e].lower()}_ohlc_cache.parquet")
            c = o["Close"].astype(float)
            c = c[~c.index.duplicated(keep="first")].sort_index()
            if c.empty or c.index.min() > COMMON_START:
                raise SystemExit(f"refused: {e} proxy cache cannot reach "
                                 f"{COMMON_START.date()}")
            self.proxy_close[e] = c
        self.multi = json.loads((snap / "multi_strategy.json")
                                .read_text(encoding="utf-8"))
        self.fx_raw = pd.read_parquet(snap / FX_FILE)
        self.g4 = json.loads((snap / G4_FILE).read_text(encoding="utf-8"))

    def cells(self, etf: str) -> list[dict]:
        p = self.g4["panels"].get(etf)
        return [] if p is None else p["fail"]


# ---------------------------------------------------------------------------
# run — gates
# ---------------------------------------------------------------------------

def gate_g0(inp: Inputs) -> dict:
    from run_ma200_sweep import compute_ma200_breadth
    bad = {}
    for e, px in inp.cons.items():
        deployed = compute_ma200_breadth(px, R.MA_PERIOD)
        everyone = pd.DataFrame(True, index=px.index, columns=px.columns)
        split, applied = R.era_split(px, {})
        for label, got in (
                ("M1_all_columns", R.masked_breadth(px, everyone)),
                ("M2_all_columns_no_barriers",
                 R.masked_breadth(split, everyone))):
            same = (got.isna().equals(deployed.isna())
                    and bool((got.dropna() == deployed.dropna()).all()))
            if not same or applied:
                bad.setdefault(e, []).append(label)
    return {"pass": not bad, "failures": bad, "n_panels": len(inp.cons)}


def gate_g1(inp: Inputs) -> dict:
    per = {e: R.g1_reconcile(px, inp.keys[e], inp.smap[e], inp.n50[e])
           for e, px in inp.cons.items()}
    return {"pass": all(r["pass"] for r in per.values()), "panels": per}


def _static_masked(px: pd.DataFrame, roster: list[str],
                   exclude: pd.DataFrame | None = None) -> pd.Series:
    """The 2026-10-14 generator's masked side: one roster held constant,
    columns matched by exact name, NO alias map (it inherited the gaps)."""
    member = pd.DataFrame(False, index=px.index, columns=px.columns)
    member[[c for c in roster if c in px.columns]] = True
    return R.masked_breadth(px, member, exclude)


def _top_positive(row: pd.Series, k: int) -> list[str]:
    from run_portfolio import top_k_breadth_weight
    w = top_k_breadth_weight(k)(row)
    return sorted(w[w > 1e-12].index)


def gate_g2(inp: Inputs) -> dict:
    """Reproduce the finding on the pinned snapshot, then hold M1 to it."""
    d = G2_SESSION
    unm, msk, priced_off = {}, {}, {}
    for e in inp.uni["A"]:
        px = inp.cons[e]
        roster = R.roster_symbols(inp.smap[e], G2_ROSTER)
        unm[e] = float(R.masked_breadth(px).get(d, np.nan))
        msk[e] = float(_static_masked(px, roster).get(d, np.nan))
        row = px.loc[d] if d in px.index else pd.Series(dtype=float)
        priced_off[e] = int(sum(1 for c in px.columns
                                if c not in set(roster) and pd.notna(row.get(c))))
    fails = []
    for e, v in G2_EXPECTED["levels"].items():
        for side, got in (("unmasked", unm[e]), ("masked", msk[e])):
            if not abs(got - v[side]) <= G2_TOL:
                fails.append(f"{e} {side} {got:.4f} vs {v[side]}")
    for e, v in G2_EXPECTED["diffs_unmasked_minus_masked"].items():
        got = unm[e] - msk[e]
        if not abs(got - v) <= G2_TOL:
            fails.append(f"{e} diff {got:+.4f} vs {v:+.3f}")
    for e, v in G2_EXPECTED["priced_not_on_roster"].items():
        if priced_off[e] != v:
            fails.append(f"{e} priced-not-on-roster {priced_off[e]} vs {v}")
    rel = lambda s: (s - s.mean())  # noqa: E731
    sel_u = _top_positive(rel(pd.Series(unm)), K_A_REGISTERED)
    sel_m = _top_positive(rel(pd.Series(msk)), K_A_REGISTERED)
    if sel_u != sorted(G2_EXPECTED["unmasked_selection"]):
        fails.append(f"unmasked selection {sel_u}")
    drop = G2_EXPECTED["dropped_by_mask"]
    if not (drop in sel_u and drop not in sel_m):
        fails.append(f"masked selection {sel_m} does not drop {drop}")
    # M1 on the same Monday resolves to the same roster; compare on the
    # recorded panels, with G4 cells removed from both sides.
    m1_check = {}
    for e in G2_EXPECTED["diffs_unmasked_minus_masked"]:
        px = inp.cons[e]
        cells = inp.cells(e)
        exc = R.exclusion_matrix(px.index, px.columns, cells)
        ref = float(_static_masked(px, R.roster_symbols(inp.smap[e],
                                                        G2_ROSTER), exc)
                    .get(d, np.nan))
        m1 = float(R.arm_breadth("M1", px, inp.keys[e], inp.smap[e],
                                 inp.barriers, cells).get(d, np.nan))
        m1_check[e] = {"M1": round(m1, 4), "static": round(ref, 4)}
        if not abs(m1 - ref) <= G2_TOL:
            fails.append(f"{e} M1 {m1:.4f} vs static {ref:.4f}")
    return {"pass": not fails, "failures": fails,
            "unmasked": {k: round(v, 4) for k, v in unm.items()},
            "masked_static": {k: round(v, 4) for k, v in msk.items()},
            "priced_not_on_roster": priced_off,
            "selection_unmasked": sel_u, "selection_masked": sel_m,
            "m1_on_session": m1_check}


def gate_g4(inp: Inputs) -> dict:
    panels = inp.g4.get("panels", {})
    missing = [e for e in inp.uni["A"] if e not in panels]
    return {"pass": not missing, "missing_panels": missing,
            "scope": inp.g4.get("scope"), "rule": inp.g4.get("rule"),
            "failing_cells": {e: len(p["fail"]) for e, p in panels.items()},
            "unverified_cells": {e: len(p["unverified"])
                                 for e, p in panels.items()}}


# ---------------------------------------------------------------------------
# run — arms
# ---------------------------------------------------------------------------

def sleeve_panels(inp: Inputs, sleeve: str, arm: str):
    """_build_panels_for (run_portfolio.py:96-160) with the arm's breadth:
    cap at the validated bound, align with the 7-day stale cap, cap again."""
    from run_ma200_sweep import align_breadth_to_index
    from validated_breadth import cap_signal
    etfs = inp.uni[sleeve]
    raw = {e: cap_signal(R.arm_breadth(arm, inp.cons[e], inp.keys[e],
                                       inp.smap[e], inp.barriers,
                                       inp.cells(e)), inp.bound[e])
           for e in etfs}
    closes = pd.DataFrame({e: inp.proxy_close[e] for e in etfs}).sort_index()
    if sleeve == "D":
        from run_europe_rotation import _fx_convert_eur_to_usd
        closes = _fx_convert_eur_to_usd(closes)
    breadths = pd.DataFrame({
        e: cap_signal(align_breadth_to_index(b, closes.index), inp.bound[e])
        for e, b in raw.items()})
    return closes, breadths, raw


def run_sleeve(closes, breadths, sleeve: str, cost_mult: int) -> dict:
    from run_portfolio import run_portfolio, top_k_breadth_weight
    if sleeve == "A":
        import run_topk_robustness as TA
        assert TA.HEADLINE_K == K_A_REGISTERED and TA.HEADLINE_FREQ == "W-MON"
        return run_portfolio(closes, TA._to_signal_panel(breadths),
                             top_k_breadth_weight(TA.HEADLINE_K),
                             COMMON_START, cost=TA.COST_FRAC * cost_mult,
                             rebalance_freq=TA.HEADLINE_FREQ, calendar="NYSE")
    import run_europe_rotation as TD
    assert TD.HEADLINE_K == K_D_REGISTERED and TD.HEADLINE_FREQ == "W-MON"
    return run_portfolio(closes, breadths, top_k_breadth_weight(TD.HEADLINE_K),
                         COMMON_START, cost=TD.COST_FRAC * cost_mult,
                         rebalance_freq=TD.HEADLINE_FREQ,
                         calendar=TD.CALENDAR)


class Overlay:
    """Phase 19 gate and Phase 22 tilt inputs, independent of any arm."""

    def __init__(self, inp: Inputs, common_end: pd.Timestamp):
        import run_risk_overlay as RO
        from alignment import align_series_to_index
        self.RO = RO
        RO.NORGATE_STATES_PATH = inp.snap / "gate_states_norgate.json"
        blend = inp.multi["strategies"][RO.UNDERLYING_BLEND_KEY]
        common = pd.to_datetime(blend["dates"])
        self.common = common[common <= common_end]
        csp1 = json.loads((inp.snap / "breadth_csp1.json")
                          .read_text(encoding="utf-8"))
        breadth = pd.Series(csp1["series"]["ma_breadth"],
                            index=pd.to_datetime(csp1["series"]["dates"])
                            ).dropna()
        breadth = align_series_to_index(breadth, self.common,
                                        max_stale_days=RO.GATE_MAX_STALE_DAYS)
        shy = pd.read_parquet(inp.snap / "asset_class_prices_cache.parquet")
        fb = shy[RO.FALLBACK_TICKER].dropna().reindex(self.common,
                                                      method="ffill")
        self.fallback_ret = fb.pct_change().fillna(0)
        states, _ = RO._load_norgate_states(self.common)
        self.gate_feed = "norgate-local" if states is not None else "csp1-scrape"
        if states is None:
            states = RO._compute_states(breadth, RO.OFF_THRESHOLD,
                                        RO.ON_THRESHOLD)
        lagged = states.shift(1).fillna(1.0)
        self.switch_cost = lagged.diff().fillna(0).abs() * (
            RO.SWITCH_COST_BPS / 10_000.0)
        self.blend_w = lagged + (1.0 - lagged) * (1.0 - RO.DERISK_FRACTION)
        self.fallback_w = (1.0 - lagged) * RO.DERISK_FRACTION
        em = pd.read_parquet(inp.snap / RO.EEM_RATIO_CACHE)
        self.eem = em[RO.EEM_TICKER].dropna()
        ratio = (em[RO.EEM_TICKER] / em[RO.EEM_REFERENCE_TICKER]).dropna()
        self.eem_signal = RO._compute_eem_tilt_signal(ratio)
        self.multi = inp.multi

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for s in (self.blend_w, self.fallback_w, self.switch_cost,
                  self.fallback_ret, self.eem, self.eem_signal):
            h.update(np.ascontiguousarray(s.to_numpy(dtype=float)).tobytes())
        for k in ("strategy_b", "strategy_c"):
            h.update(json.dumps(self.multi["strategies"][k]["equity"])
                     .encode())
        return h.hexdigest()

    def blends(self, eq_a: pd.Series, eq_d: pd.Series) -> dict:
        def series(eq):
            return {"dates": [d.strftime("%Y-%m-%d") for d in eq.index],
                    "equity": [float(v) for v in eq.values]}
        strategies = dict(self.multi["strategies"])
        strategies["strategy_a"] = series(eq_a)
        strategies["strategy_d"] = series(eq_d)
        tilted = self.RO._build_eem_tilted_blend(
            {"strategies": strategies}, self.eem, self.eem_signal,
            self.common)
        tr = tilted.pct_change().fillna(0)
        gated = (self.blend_w * tr + self.fallback_w * self.fallback_ret
                 - self.switch_cost)
        # Ungated: the published blend_35_35_10_20 construction
        # (run_multi_strategy.py:395), with its own blend cadence and cost.
        from run_multi_strategy import fixed_blend_4way
        eqs = [pd.Series(strategies[k]["equity"],
                         index=pd.to_datetime(strategies[k]["dates"]))
               .reindex(self.common).ffill().bfill()
               for k in ("strategy_a", "strategy_b", "strategy_c",
                         "strategy_d")]
        ungated = fixed_blend_4way(*[s / s.iloc[0] for s in eqs],
                                   0.35, 0.35, 0.10)
        return {"deployed": (1 + gated).cumprod(), "ungated": ungated}


def report(eq: pd.Series, weights, end: pd.Timestamp) -> dict:
    import ws1_common as W
    return W.full_report(eq.loc[:end], None if weights is None
                         else weights.loc[:end], COMMON_START, end)


def run_arms(inp: Inputs, arms: list[str]) -> dict:
    # One window for every arm: the latest session all inputs reach.
    ends = [inp.bound[e] for e in inp.uni["A"] + inp.uni["D"]]
    ends += [inp.proxy_close[e].index.max() for e in inp.proxy_close]
    for k in ("strategy_b", "strategy_c"):
        ends.append(pd.Timestamp(inp.multi["strategies"][k]["dates"][-1]))
    end = min(ends)
    ov = Overlay(inp, end)
    fp0 = ov.fingerprint()
    out, runs = {}, {}
    for arm in arms:
        res = {}
        for sleeve in ("A", "D"):
            closes, breadths, raw = sleeve_panels(inp, sleeve, arm)
            r1 = run_sleeve(closes, breadths, sleeve, 1)
            r2 = run_sleeve(closes, breadths, sleeve, 2)
            runs[(arm, sleeve)] = {"r1": r1, "closes": closes, "raw": raw}
            rep = report(r1["equity"], r1["weights"], end)
            rep["sharpe_2x_cost"] = report(r2["equity"], None,
                                           end)["full"]["sharpe"]
            res[sleeve] = rep
        b1 = ov.blends(runs[(arm, "A")]["r1"]["equity"],
                       runs[(arm, "D")]["r1"]["equity"])
        res["blend_deployed"] = report(b1["deployed"], None, end)
        res["blend_ungated"] = report(b1["ungated"], None, end)
        out[arm] = res
        print(f"  {arm:4s} A {res['A']['full']['sharpe']:+.4f}  "
              f"D {res['D']['full']['sharpe']:+.4f}  deployed blend "
              f"{res['blend_deployed']['full']['sharpe']:+.4f}  ungated "
              f"{res['blend_ungated']['full']['sharpe']:+.4f}")
    if ov.fingerprint() != fp0:
        raise SystemExit("wiring check failed: an arm-independent series moved")
    return {"window": {"start": str(COMMON_START.date()),
                       "end": str(end.date()),
                       "split": str(SPLIT_DATE.date())},
            "gate_feed": ov.gate_feed, "overlay_fingerprint": fp0,
            "arms": out, "_runs": runs, "_end": end}


def delta(a: dict, b: dict) -> dict:
    keys = ("A", "D", "blend_deployed", "blend_ungated")
    return {k: round(a[k]["full"]["sharpe"] - b[k]["full"]["sharpe"], 4)
            for k in keys}


def decision(d_m2: dict) -> dict:
    hit = {k: abs(v) >= RESTATEMENT_BAND
           for k, v in d_m2.items() if k in ("A", "D", "blend_deployed")}
    return {"rule": ("ADOPT M2; RESTATEMENT if |delta| >= 0.01 Sharpe on the "
                     "deployed blend, sleeve A or sleeve D (M2 minus M0s, 1x, "
                     "symmetric basis); otherwise footnoted correction"),
            "legs_at_or_above_band": hit,
            "verdict": ("ADOPT M2 — RESTATEMENT" if any(hit.values())
                        else "ADOPT M2 — FOOTNOTED CORRECTION")}


# ---------------------------------------------------------------------------
# run — descriptives
# ---------------------------------------------------------------------------

def descriptives(inp: Inputs, scored: dict) -> dict:
    end = scored["_end"]
    idx_full = None
    d1 = {}
    for e in inp.uni["A"] + inp.uni["D"]:
        px = inp.cons[e]
        member = R.membership_matrix(px.index, px.columns, inp.keys[e],
                                     inp.smap[e], "strict")
        exc = R.exclusion_matrix(px.index, px.columns, inp.cells(e))
        classes = R.contamination_classes(member)
        dup = R.duplicate_lineage(px, member)
        win = px.index[(px.index >= COMMON_START) & (px.index <= end)]
        last52 = win[win > end - pd.Timedelta(weeks=52)]
        allc = dict(classes, duplicate_lineage=dup)
        b1 = R.arm_breadth("M1", px, inp.keys[e], inp.smap[e], inp.barriers,
                           inp.cells(e))
        b2 = R.arm_breadth("M2", px, inp.keys[e], inp.smap[e], inp.barriers,
                           inp.cells(e))
        d1[e] = {
            "full": R.class_contributions(px, member, exc, allc, win),
            "last_52_weeks": R.class_contributions(px, member, exc, allc,
                                                   last52),
            "barrier_effect_M2_minus_M1": R._pp(b2 - b1, win),
            "dup_lineage_columns": sorted(c for c in dup.columns
                                          if dup[c].any()),
        }
    from rebalance_calendar import engine_rebalance_dates
    d3 = {}
    for sleeve, cal in (("A", "NYSE"), ("D", "XETR")):
        closes = scored["_runs"][("M0s", sleeve)]["closes"]
        reb = pd.DatetimeIndex(engine_rebalance_dates(
            closes.index, COMMON_START, "W-MON", cal))
        reb = reb[reb <= end]
        w0 = scored["_runs"][("M0s", sleeve)]["r1"]["weights"]
        d3[sleeve] = {arm: R.selection_agreement(
            w0, scored["_runs"][(arm, sleeve)]["r1"]["weights"], reb)
            for arm in ("M1", "M2")}
    return {"D1_D2": d1, "D3": d3}


def drift_vs_published(inp: Inputs, scored: dict) -> dict:
    end = scored["_end"]
    out = {}
    for sleeve, key in (("A", "strategy_a"), ("D", "strategy_d")):
        s = inp.multi["strategies"][key]
        pub = pd.Series(s["equity"], index=pd.to_datetime(s["dates"]))
        out[sleeve] = {
            "published": report(pub, None, end)["full"]["sharpe"],
            "M0_rebuilt": scored["arms"]["M0"][sleeve]["full"]["sharpe"]}
    ro = json.loads((inp.snap / "risk_overlay.json").read_text(encoding="utf-8"))
    out["risk_overlay_gated_variants"] = {
        k: v.get("sharpe") for k, v in (ro.get("gated_variants") or {}).items()
        if isinstance(v, dict)}
    out["note"] = ("printed, not absorbed (§5.3); published series run from "
                   "their own eligible start and calendar")
    return out


def cmd_run(args) -> int:
    load = guard_machine_load()
    flags = assert_staging_flags_unset()
    snap = Path(args.snapshot)
    n_files = verify_checksums(snap)
    uni = universe()
    fx_raw = pd.read_parquet(snap / FX_FILE)
    install_network_guard(fx_raw)
    inp = Inputs(snap, uni)
    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "registration": {"document": REGISTRATION,
                         "freeze_commit": FREEZE_COMMIT,
                         "main_commit": MAIN_COMMIT,
                         "countersigned": "2026-10-14"},
        "snapshot": {"path": str(snap), "files_verified": n_files,
                     "manifest": json.loads((snap / "manifest.json")
                                            .read_text(encoding="utf-8"))},
        "machine_load": load, "staging_flags": flags, "gates": {},
    }
    for name, fn in (("G0", gate_g0), ("G1", gate_g1), ("G2", gate_g2),
                     ("G4", gate_g4)):
        res = fn(inp)
        payload["gates"][name] = res
        print(f"{name}: {'PASS' if res['pass'] else 'FAIL'}")
        if not res["pass"]:
            payload["verdict"] = f"HOLD — {name} failed; no performance read"
            write(payload)
            return 2
    arms = ["M0", "M0s", "M1", "M2"] if not args.smoke else ["M0s", "M2"]
    scored = run_arms(inp, arms)
    A = scored["arms"]
    payload["window"] = scored["window"]
    payload["gate_feed"] = scored["gate_feed"]
    payload["wiring_check"] = {"overlay_fingerprint":
                               scored["overlay_fingerprint"],
                               "identical_across_arms": True}
    payload["arms"] = A
    payload["deltas"] = {"M2_minus_M0s": delta(A["M2"], A["M0s"])}
    if not args.smoke:
        payload["deltas"].update({
            "M1_minus_M0s": delta(A["M1"], A["M0s"]),
            "M0s_minus_M0_g4_effect": delta(A["M0s"], A["M0"]),
            "M2_minus_M0_deployed": delta(A["M2"], A["M0"])})
        payload["descriptives"] = descriptives(inp, scored)
        payload["drift_vs_published"] = drift_vs_published(inp, scored)
    payload["decision"] = decision(payload["deltas"]["M2_minus_M0s"])
    payload["verdict"] = payload["decision"]["verdict"]
    print(f"VERDICT: {payload['verdict']}  {payload['deltas']['M2_minus_M0s']}")
    write(payload)
    return 0


def write(payload: dict) -> None:
    import ws1_common as W
    W.write_json(OUT, payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", required=True)
    s.add_argument("--source", default=str(SCHED_DATA))
    r = sub.add_parser("run")
    r.add_argument("--snapshot", required=True)
    r.add_argument("--smoke", action="store_true",
                   help="gates, then M0s and M2 only; no descriptives")
    args = ap.parse_args()
    return cmd_snapshot(args) if args.cmd == "snapshot" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
