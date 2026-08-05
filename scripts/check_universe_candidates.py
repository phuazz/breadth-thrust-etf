"""Book-wide overlap gate for ETF candidates, and a retrospective audit.

Two rules are in force in this project, on two different bases. Before
2026-08-05 this script enforced neither of them correctly.

  RULE 1 — within-sleeve candidate gate (Phase 5, 2026-05-24; reaffirmed
  Phase 25, 2026-05-31). Basis: weekly Friday SIGNAL correlation, where
  the signal is distance above the own 200-day MA — the same quantity the
  sleeve ranks on. Threshold: max correlation < 0.85 versus the target
  sleeve's incumbents, plus >= 5 years of overlapping history.
  Authorities: run_phase5_correlation.py:11, run_thematic_universe_screen.py:18.

  RULE 2 — book-wide overlap rule (WS2, adopted 2026-07-02). Basis:
  weekly RETURN correlation. Threshold: reject a candidate above 0.90
  versus ANY incumbent anywhere in the deployed book, unless distinct
  exposure is argued in writing. Authority: ws2_correlation.json "rule";
  STUDIES_LEDGER.md row 2026-07-02.

Two defects this rewrite fixes:

  (a) BASIS MISMATCH. The previous version computed RETURN correlation and
      compared it to 0.85, citing "the Phase 5 threshold" — but Phase 5
      measured SIGNAL correlation. Signal series are slow-moving and
      strongly autocorrelated, so their correlations sit above return
      correlations for the same pair; applying 0.85 to returns is a
      LOOSER gate than Phase 5 specified, and candidates Phase 5 would
      have rejected could pass.

  (b) SLEEVE-SCOPE ASYMMETRY. Sleeve C candidates have always been
      screened against sleeve A's sector slate as well as C's own members
      (run_phase5_correlation.py:57 loads STRATEGY_A_PROXIES) — which is
      how XOP, OIH and AMLP were rejected, all three on their correlation
      with XLE, sleeve A's energy line. Sleeve B candidates were screened
      against B incumbents ONLY. The same standard was therefore applied
      to one sleeve and not the other. Both rules now run against the
      whole deployed book, resolved from the engines.

The deployed book is resolved through scanner_universe.resolve_universe()
so this script, the daily scanner and the sleeve engines cannot disagree
about what is held. Prices for deployed lines come from committed parquet
caches (offline, no refetch, no cache rewrite); only user-supplied
candidates are fetched from yfinance.

Correlation is computed per pair on that pair's own overlap window, never
on a listwise-deleted panel: one short-history member must not be able to
truncate every other pair's sample.

Usage
-----
    python scripts/check_universe_candidates.py --strategy B SLV CPER
    python scripts/check_universe_candidates.py --strategy C XME PICK
    python scripts/check_universe_candidates.py --audit

``--audit`` runs Rule 2 retrospectively over the deployed book and lists
every incumbent pair above 0.90. The rule is prospective by construction
("reject CANDIDATES ..."), so no incumbent has ever been tested against
it; the audit is what surfaces a pair that would be rejected today if it
arrived as a candidate. Incumbent breaches are reported, never actioned —
a drop is a strategy change and belongs in a pre-registered ablation
(see run_ws8_reit_overlap.py for the worked example).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

DATA = ROOT / "data"

from etf_registry import ETF_REGISTRY  # noqa: E402
from scanner_universe import resolve_universe  # noqa: E402
import run_asset_class_rotation as B_engine  # noqa: E402
import run_thematic_rotation as C_engine  # noqa: E402

SIGNAL_GATE_MAX_CORR = 0.85     # Rule 1, weekly signal basis
OVERLAP_RULE_MAX_CORR = 0.90    # Rule 2, weekly return basis
MIN_YEARS_HISTORY = 5
MIN_PAIR_WEEKS = 52
MA_PERIOD = 200
PANEL_START = "2018-01-01"
FX_CACHE = DATA / "ws1_fx_eurusd_cache.parquet"


# ---------------------------------------------------------------------------
# Deployed book — engine tickers, priced from committed caches
# ---------------------------------------------------------------------------

def _ohlc_close(ticker: str) -> pd.Series:
    cache = DATA / f"{ticker.lower()}_ohlc_cache.parquet"
    if not cache.exists():
        raise FileNotFoundError(f"no committed OHLC cache for {ticker}")
    s = pd.read_parquet(cache)["Close"].astype(float)
    return s[~s.index.duplicated(keep="first")].sort_index()


def _eur_to_usd(s: pd.Series) -> pd.Series:
    """EURUSD=X quotes USD per EUR, so EUR price * rate = USD (multiply)."""
    if not FX_CACHE.exists():
        raise FileNotFoundError(
            "missing data/ws1_fx_eurusd_cache.parquet — required to put the "
            "Xetra lines on the same USD basis as the rest of the book")
    fx = pd.read_parquet(FX_CACHE)["EURUSD"]
    return s * fx.reindex(s.index, method="ffill").bfill()


def deployed_panel() -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """USD closes for every line the deployed book ranks, plus sleeve map.

    Keyed by ENGINE ticker, not scanned ticker: sleeve C ranks BTC-USD
    while the scanner watches IBIT, and it is the ranked series that a
    correlation gate is about.
    """
    b_cache = pd.read_parquet(B_engine.PRICE_CACHE)
    c_cache = pd.read_parquet(C_engine.PRICE_CACHE)
    for df in (b_cache, c_cache):
        df.index = pd.to_datetime(df.index).tz_localize(None)

    closes: dict[str, pd.Series] = {}
    sleeves: dict[str, list[str]] = {}
    for row in resolve_universe():
        for origin in row.origins:
            et = origin.engine_ticker
            sleeves.setdefault(et, [])
            if origin.sleeve not in sleeves[et]:
                sleeves[et].append(origin.sleeve)
            if et in closes:
                continue
            if origin.sleeve == "B":
                closes[et] = b_cache[et].astype(float)
            elif origin.sleeve == "C":
                closes[et] = c_cache[et].astype(float)
            else:
                # A, D and the overlay price through the registry proxy.
                proxy = (ETF_REGISTRY.get(et, {}) or {}).get(
                    "yfinance_trading_proxy") or et
                s = _ohlc_close(proxy)
                closes[et] = _eur_to_usd(s) if proxy.endswith(".DE") else s

    panel = pd.DataFrame(closes).sort_index()
    return panel.loc[PANEL_START:], sleeves


def fetch_candidates(tickers: list[str]) -> pd.DataFrame:
    """Candidate closes from yfinance — the only network call here."""
    import yfinance as yf
    from datetime import date

    raw = yf.download(tickers, start=PANEL_START, end=date.today().isoformat(),
                      auto_adjust=True, progress=False, threads=True,
                      group_by="ticker")
    out = {}
    for t in tickers:
        try:
            s = raw[t]["Close"] if (t, "Close") in raw.columns else raw["Close"]
            s = s.dropna()
            if len(s):
                out[t] = s.astype(float)
            else:
                print(f"  WARN: no data for {t}")
        except Exception:
            print(f"  WARN: no data for {t}")
    df = pd.DataFrame(out)
    if len(df):
        df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


# ---------------------------------------------------------------------------
# Bases
# ---------------------------------------------------------------------------

def weekly_returns(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.resample("W-FRI").last().pct_change(fill_method=None)


def weekly_signal(panel: pd.DataFrame) -> pd.DataFrame:
    """Distance above own 200d MA, weekly Friday — the ranked quantity.

    Computed PER COLUMN on that column's own observed dates. A whole-frame
    rolling(200, min_periods=200) silently returns an all-NaN panel here:
    the book spans the NYSE, Xetra, Shenzhen and 24x7 crypto calendars, so
    the union index leaves NaNs inside every 200-row window of every
    column and no window ever reaches 200 observations. The result is not
    an error — it is empty correlations and confident PASS verdicts on
    candidates nothing was actually compared against. Same defence as
    run_thematic_universe_screen.py:148.
    """
    out = {}
    for col in panel.columns:
        s = panel[col].dropna()
        if len(s) < MA_PERIOD:
            continue
        ma = s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean()
        out[col] = ((s - ma) / ma).resample("W-FRI").last()
    sig = pd.DataFrame(out)
    if sig.empty or not sig.notna().any().any():
        raise RuntimeError("signal panel is empty — refusing to gate on it")
    return sig


def pairwise(target: pd.Series, others: pd.DataFrame) -> list[tuple[str, float]]:
    """Correlation of `target` against each column, each on its own overlap."""
    out = []
    for col in others.columns:
        paired = pd.concat([target, others[col]], axis=1).dropna()
        if len(paired) < MIN_PAIR_WEEKS:
            continue
        out.append((col, float(paired.corr().iloc[0, 1])))
    return sorted(out, key=lambda x: -x[1])


def _tag(ticker: str, sleeves: dict[str, list[str]]) -> str:
    return f"{ticker} ({'/'.join(sleeves.get(ticker, ['?']))})"


def proxy_identity_pairs() -> set[frozenset[str]]:
    """Pairs whose correlation is 1.0 by construction, not by measurement.

    Sleeves A and D are PRICED through their registry trading proxies
    (CSP1 -> SPY, CNDX -> QQQ, IDP6 -> IJR). Where sleeve B or C holds
    that same proxy ticker outright, the panel carries one price series
    under two names and the pair self-correlates.

    The economic overlap is real — sleeve A does hold S&P 500 exposure
    and sleeve B does hold SPY, which is what WS2 quantified as the
    US-beta cluster (mean 46.8% of NAV, peak 83.5%). What is NOT real is
    the coefficient: the true CSP1-vs-SPY correlation is unmeasured here
    and cannot be recovered from this panel. Labelling the pairs keeps
    the audit honest and stops three structural 1.000s from sitting
    permanently at the top of the list, which is how a guard trains its
    readers to skip it.
    """
    out: set[frozenset[str]] = set()
    for engine, entry in ETF_REGISTRY.items():
        proxy = (entry or {}).get("yfinance_trading_proxy")
        if proxy and proxy != engine:
            out.add(frozenset({engine, proxy}))
    return out


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def audit_incumbents(panel: pd.DataFrame, sleeves: dict[str, list[str]],
                     json_out: Path | None = None) -> int:
    wret = weekly_returns(panel)
    cols = list(wret.columns)
    identity = proxy_identity_pairs()
    breaches = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            paired = wret[[a, b]].dropna()
            if len(paired) < MIN_PAIR_WEEKS:
                continue
            c = float(paired.corr().iloc[0, 1])
            if c > OVERLAP_RULE_MAX_CORR:
                cross = not set(sleeves.get(a, [])) & set(sleeves.get(b, []))
                breaches.append((c, a, b, cross,
                                 frozenset({a, b}) in identity, len(paired)))
    breaches.sort(reverse=True)

    print(f"\nRule 2 audit — deployed book, {len(cols)} lines, weekly return "
          f"correlation > {OVERLAP_RULE_MAX_CORR} ({panel.index.min().date()} "
          f"-> {panel.index.max().date()})\n")
    if not breaches:
        print("  no incumbent pair breaches the rule")
        return 0
    measured = [x for x in breaches if not x[4]]
    for c, a, b, cross, ident, n in breaches:
        kind = ("PROXY-IDENTITY" if ident
                else ("CROSS-SLEEVE" if cross else "within-sleeve"))
        print(f"  {c:.3f}  {_tag(a, sleeves):22s} ~ {_tag(b, sleeves):22s} "
              f"[{kind}, n={n}w]")
    n_ident = len(breaches) - len(measured)
    if n_ident:
        print(f"\n  {n_ident} PROXY-IDENTITY pair(s): sleeve A/D is priced "
              "through the very ticker the other sleeve holds, so the panel "
              "carries one series twice and the coefficient is structural, "
              "not measured. The exposure overlap is real (WS2 US-beta "
              "cluster, mean 46.8% / peak 83.5% of NAV); the number is not "
              "evidence about it.")
    print(f"\n  {len(measured)} genuinely measured pair(s) above the rule. "
          "These are INCUMBENTS: the rule is prospective, so none was ever "
          "tested against it. Reported only — a drop is a strategy change "
          "and needs a pre-registered ablation "
          "(run_ws8_reit_overlap.py is the worked example).")

    if json_out is not None:
        import json
        from datetime import datetime, timezone
        json_out = json_out.resolve()
        json_out.write_text(json.dumps({
            "computed_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "window": {"start": str(panel.index.min().date()),
                       "end": str(panel.index.max().date())},
            "rule": (f"weekly return correlation > {OVERLAP_RULE_MAX_CORR} "
                     "(WS2 2026-07-02, prospective — incumbents never tested)"),
            "n_lines": len(cols),
            "pairs": [{"a": a, "b": b, "corr": round(c, 4),
                       "sleeves_a": sleeves.get(a, []),
                       "sleeves_b": sleeves.get(b, []),
                       "cross_sleeve": cross, "proxy_identity": ident,
                       "n_weeks": n}
                      for c, a, b, cross, ident, n in breaches],
        }, indent=1) + "\n", encoding="utf-8")
        print(f"  wrote {json_out.relative_to(ROOT)}")
    return 0


def gate_candidates(candidates: list[str], strategy: str,
                    panel: pd.DataFrame, sleeves: dict[str, list[str]]) -> int:
    cand_px = fetch_candidates(candidates)
    if not len(cand_px.columns):
        print("no candidate data — nothing to gate")
        return 1

    # A candidate may already be deployed — re-testing an incumbent as if
    # it arrived fresh is a legitimate and useful thing to ask. Drop its
    # own line from the comparison set: a duplicate column name breaks the
    # per-column maths, and a self-correlation of 1.000 says nothing.
    incumbent = [t for t in cand_px.columns if t in panel.columns]
    if incumbent:
        for t in incumbent:
            print(f"  NOTE: {t} is already deployed in sleeve "
                  f"{'/'.join(sleeves.get(t, ['?']))} — screening it against "
                  "the rest of the book, excluding its own line")
        panel = panel.drop(columns=incumbent)

    book = list(panel.columns)
    in_sleeve = [t for t, s in sleeves.items()
                 if strategy in s and t in panel.columns]
    full = pd.concat([panel, cand_px], axis=1).sort_index()
    wret, wsig = weekly_returns(full), weekly_signal(full)

    print(f"\nGating {list(cand_px.columns)} for sleeve {strategy}")
    print(f"  Rule 1  signal corr < {SIGNAL_GATE_MAX_CORR} vs all {len(book)} "
          f"deployed lines (book-wide, as Phase 5 screened C candidates "
          f"against sleeve A), >= {MIN_YEARS_HISTORY}y history")
    print(f"  Rule 2  return corr <= {OVERLAP_RULE_MAX_CORR} vs all "
          f"{len(book)} deployed lines (book-wide)\n")

    verdicts = []
    for c in cand_px.columns:
        valid = cand_px[c].dropna()
        years = (valid.index.max() - valid.index.min()).days / 365.25
        sig_pairs = pairwise(wsig[c], wsig[book])
        sig_in_sleeve = pairwise(wsig[c], wsig[in_sleeve])
        ret_pairs = pairwise(wret[c], wret[book])
        s_max = sig_pairs[0] if sig_pairs else ("—", float("nan"))
        r_max = ret_pairs[0] if ret_pairs else ("—", float("nan"))

        fails = []
        if not (s_max[1] < SIGNAL_GATE_MAX_CORR):
            fails.append(f"Rule 1 signal corr {s_max[1]:.3f} vs "
                         f"{_tag(s_max[0], sleeves)}")
        if not (r_max[1] <= OVERLAP_RULE_MAX_CORR):
            fails.append(f"Rule 2 return corr {r_max[1]:.3f} vs "
                         f"{_tag(r_max[0], sleeves)}")
        if fails:
            verdict, reason = "FAIL", "; ".join(fails)
        elif years < MIN_YEARS_HISTORY:
            verdict, reason = "DEFER", f"history {years:.1f}y < {MIN_YEARS_HISTORY}y"
        else:
            verdict, reason = "PASS", "clears both rules"

        verdicts.append((c, verdict, reason))
        print(f"  {c}: {verdict} — {reason}")
        print(f"    history {valid.index.min().date()} -> "
              f"{valid.index.max().date()} ({years:.1f}y)")
        print("    top signal corr (book-wide): " + ", ".join(
            f"{_tag(t, sleeves)} {v:+.3f}" for t, v in sig_pairs[:3]))
        print(f"    top signal corr (sleeve {strategy} only, for context): "
              + ", ".join(f"{t} {v:+.3f}" for t, v in sig_in_sleeve[:3]))
        print("    top return corr (book-wide): " + ", ".join(
            f"{_tag(t, sleeves)} {v:+.3f}" for t, v in ret_pairs[:3]))

    print("\nSummary:")
    for c, v, r in verdicts:
        print(f"  {c:10s}  {v:6s}  {r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["A", "B", "C", "D"])
    ap.add_argument("--audit", action="store_true",
                    help="run Rule 2 retrospectively over the deployed book")
    ap.add_argument("--json", type=Path, default=None,
                    help="with --audit, also write the breach list as JSON "
                         "so charts and monitors read committed data")
    ap.add_argument("candidates", nargs="*")
    args = ap.parse_args()

    if not args.audit and not (args.strategy and args.candidates):
        ap.error("give --strategy plus candidate tickers, or --audit")

    panel, sleeves = deployed_panel()
    if args.audit:
        return audit_incumbents(panel, sleeves, args.json)
    return gate_candidates(args.candidates, args.strategy, panel, sleeves)


if __name__ == "__main__":
    raise SystemExit(main())
