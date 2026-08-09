"""Build the plain-language portfolio page -> build/portfolio.html.

A second, deliberately reduced public surface for friends and peers. It answers
three questions and nothing else: what the book holds today, how it is split,
and what the simulated record looks like. It carries no method, no parameters,
no trade history and no per-sleeve diagnostics — a weekly trade log against a
known universe is the fastest way to infer a ranking rule, and it is also the
most overwhelming table on the main dashboard.

The reduction is presentational only. Nothing here is a defence of the strategy
rules, which are stated in full in README.md on a public MIT-licensed repo.

Same shape as build_scanner_page.py: the template is the source file and is what
gets edited; the built artefact is generated and never hand-touched. The payload
is also written to data/portfolio_simple.json so the template's fetch fallback
works standalone during development (`npx serve .` from the project root) and so
the parity test has a clean artefact to assert against.

The output is NOT under docs/ — see OUT_PATH. This repo no longer serves the
page; phuazz/portfolio does, and pulls this file.

Deliberately separate from pipeline.py. This page must not be able to fail the
main dashboard build, and the main dashboard must not be able to publish a
holdings table this page disagrees with — the parity test in
tests/test_simple_page_parity.py is what enforces the second half of that.

Usage:
    python scripts/build_simple_page.py
    python scripts/build_simple_page.py --check   # verify, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_registry import display_ticker  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "simple_template.html"
# NOT under docs/. GitHub Pages serves docs/, and this page is deliberately no
# longer published from this repo — it lives at phuazz.github.io/portfolio/,
# away from the research dashboard, so a non-specialist reader has no sibling
# path to wander into. This file is the transport: the portfolio repo fetches
# it from raw.githubusercontent.com and commits it as its own index.html, which
# is why it must stay committed even though nothing here serves it.
OUT_PATH = ROOT / "build" / "portfolio.html"
PAYLOAD_PATH = ROOT / "data" / "portfolio_simple.json"

LIVE_TRACK = ROOT / "data" / "live_track.json"
RISK_OVERLAY = ROOT / "data" / "risk_overlay.json"
ETF_NAMES = ROOT / "data" / "etf_names.json"

PLACEHOLDER_START = "// __PORTFOLIO_DATA_START__"
PLACEHOLDER_END = "// __PORTFOLIO_DATA_END__"

MAX_TEMPLATE_BYTES = 200 * 1024
CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")

# Weights are floats summed over 23 positions; 1e-6 is far tighter than any
# real error and far looser than float noise over that many terms.
WEIGHT_TOLERANCE = 1e-6

# Plain-English sleeve labels. The mechanism behind each is not disclosed here
# and must not be added — that is the whole point of this page.
SLEEVE_LABELS = {
    "strategy_a": "US sectors",
    "strategy_b": "Asset classes",
    "strategy_c": "Thematic",
    "strategy_d": "Europe sectors",
    "tilt": "Emerging markets tilt",
}
# Fixed display order. It is also the adjacency order the categorical palette
# in simple_template.html was validated against, so do not reorder without
# re-running the palette validator.
SLEEVE_ORDER = ["strategy_a", "strategy_b", "strategy_c", "strategy_d", "tilt"]

# EEM is held solely via the overlay and so appears in no sleeve's own weights.
TILT_TICKER = "EEM"

# The page is read line by line, so the curve is downsampled to roughly weekly.
# The last point is always kept — it is the one figure a reader checks.
MAX_CURVE_POINTS = 420

DEPLOYED_KEY = "blend_35_35_10_20_gated_eem_tilted"


class SimplePageError(RuntimeError):
    """Raised when the page cannot be built safely."""


def downsample(dates: list[str], equity: list[float], limit: int) -> tuple[list[str], list[float]]:
    """Even stride, first and last always kept."""
    n = len(dates)
    if n <= limit:
        return list(dates), list(equity)
    stride = -(-n // limit)  # ceil
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return [dates[i] for i in idx], [equity[i] for i in idx]


def build_payload() -> dict:
    for path in (LIVE_TRACK, RISK_OVERLAY, ETF_NAMES):
        if not path.exists():
            raise SimplePageError(f"missing {path.relative_to(ROOT)}")

    live = json.loads(LIVE_TRACK.read_text(encoding="utf-8"))
    overlay = json.loads(RISK_OVERLAY.read_text(encoding="utf-8"))
    names = json.loads(ETF_NAMES.read_text(encoding="utf-8"))

    effective = live.get("effective_weights") or {}
    if not effective:
        raise SimplePageError("live_track.json carries no effective_weights")

    # ---- sleeve membership -------------------------------------------------
    # Derived, never hardcoded: a ticker's sleeve is whichever sleeve's own
    # weights contain it. EEM is the one position held purely by the overlay.
    membership: dict[str, str] = {}
    duplicates: list[str] = []
    for sleeve_key, sleeve in (live.get("sleeve_extensions") or {}).items():
        for ticker in sleeve.get("weights") or {}:
            if ticker in membership:
                duplicates.append(ticker)
            membership[ticker] = sleeve_key
    if duplicates:
        raise SimplePageError(
            f"tickers claimed by more than one sleeve: {sorted(set(duplicates))} — "
            f"the sleeve split on the page would double-count them"
        )
    if TILT_TICKER in effective:
        membership.setdefault(TILT_TICKER, "tilt")

    unassigned = sorted(set(effective) - set(membership))
    if unassigned:
        raise SimplePageError(
            f"held but in no sleeve: {unassigned} — the split would not sum to NAV"
        )

    # ---- holdings ----------------------------------------------------------
    holdings = []
    missing_names = []
    for ticker, weight in sorted(effective.items(), key=lambda kv: -kv[1]):
        name = names.get(ticker)
        if not name:
            missing_names.append(ticker)
            continue
        sleeve_key = membership[ticker]
        holdings.append({
            "ticker": display_ticker(ticker),
            "name": name,
            "sleeve": sleeve_key,
            "sleeve_label": SLEEVE_LABELS[sleeve_key],
            "weight": round(weight, 6),
        })
    if missing_names:
        raise SimplePageError(
            f"no display name for {missing_names} — run "
            f"`python scripts/fetch_etf_names.py`; the page must not print a "
            f"bare ticker to a non-specialist reader"
        )

    # ---- sleeve split ------------------------------------------------------
    totals: dict[str, float] = {}
    for ticker, weight in effective.items():
        totals[membership[ticker]] = totals.get(membership[ticker], 0.0) + weight
    sleeves = [
        {"key": k, "label": SLEEVE_LABELS[k], "weight": round(totals[k], 6)}
        for k in SLEEVE_ORDER if k in totals
    ]

    # ---- deployed simulated record ----------------------------------------
    variant = (overlay.get("gated_variants") or {}).get(DEPLOYED_KEY)
    if not variant:
        raise SimplePageError(
            f"risk_overlay.json has no {DEPLOYED_KEY} — the page would have to "
            f"show a variant the book does not run"
        )
    dates, equity = downsample(variant["dates"], variant["equity"], MAX_CURVE_POINTS)

    as_of = live.get("anchor_date")
    return {
        "as_of": as_of,
        "panel_end_date": overlay.get("panel_end_date"),
        "computed_at_utc": live.get("computed_at_utc"),
        "holdings": holdings,
        "sleeves": sleeves,
        "n_positions": len(holdings),
        "defensive_engaged": overlay.get("current_state") != "RISK_ON",
        "curve": {"dates": dates, "equity": [round(e, 6) for e in equity]},
        "stats": {
            "sharpe": round(variant["sharpe"], 4),
            "cagr": round(variant["cagr"], 6),
            "max_dd": round(variant["max_dd"], 6),
            "start": variant["dates"][0],
            "end": variant["dates"][-1],
        },
    }


def assert_payload_usable(payload: dict) -> None:
    """Refuse to publish a page the build already knows is wrong."""
    problems: list[str] = []

    total = sum(h["weight"] for h in payload["holdings"])
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        problems.append(f"holdings sum to {total:.8f}, not 1.0")

    sleeve_total = sum(s["weight"] for s in payload["sleeves"])
    if abs(sleeve_total - 1.0) > WEIGHT_TOLERANCE:
        problems.append(f"sleeve split sums to {sleeve_total:.8f}, not 1.0")

    if not payload.get("as_of"):
        problems.append("no as_of date")

    # The two sources are refreshed by different steps. If they disagree, one of
    # them is stale, and the page would date a holdings table against a curve
    # that ends somewhere else.
    if payload.get("as_of") != payload.get("panel_end_date"):
        problems.append(
            f"live_track anchor {payload.get('as_of')} != risk_overlay panel end "
            f"{payload.get('panel_end_date')} — one source is stale"
        )
    if payload["curve"]["dates"] and payload["curve"]["dates"][-1] != payload.get("as_of"):
        problems.append(
            f"curve ends {payload['curve']['dates'][-1]} but positions are as of "
            f"{payload.get('as_of')}"
        )

    if len(payload["curve"]["dates"]) != len(payload["curve"]["equity"]):
        problems.append("curve dates and equity are different lengths")

    if any(h["weight"] <= 0 for h in payload["holdings"]):
        problems.append("a holding has a non-positive weight")

    if problems:
        raise SimplePageError(
            "portfolio payload is not publishable:\n  - " + "\n  - ".join(problems)
        )


def inject(template_text: str, payload: dict) -> str:
    start = template_text.find(PLACEHOLDER_START)
    end = template_text.find(PLACEHOLDER_END)
    if start == -1 or end == -1:
        raise SimplePageError(
            f"placeholder markers missing from {TEMPLATE.name}; expected "
            f"{PLACEHOLDER_START!r} and {PLACEHOLDER_END!r}"
        )
    if end < start:
        raise SimplePageError("placeholder markers are in the wrong order")

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # </script> inside a JSON string would close the host script element early.
    body = body.replace("</", "<\\/")
    replacement = (
        f"{PLACEHOLDER_START}\n"
        f"const PORTFOLIO_DATA_INLINE = {body};\n"
        f"{PLACEHOLDER_END}"
    )
    return template_text[:start] + replacement + template_text[end + len(PLACEHOLDER_END):]


def assert_output_clean(text: str) -> None:
    for marker in CONFLICT_MARKERS:
        if marker in text:
            raise SimplePageError(
                f"built page contains a merge conflict marker {marker!r} — "
                f"the fault is in simple_template.html or the source JSON"
            )
    if "PORTFOLIO_DATA_INLINE = null" in text:
        raise SimplePageError("injection did not take — data is still null")
    # The page exists to be readable on a phone; a missing viewport meta is the
    # exact failure MOBILE_CHECK.md was written for.
    if 'name="viewport"' not in text:
        raise SimplePageError("built page has no viewport meta tag")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="validate inputs and the render, but write nothing")
    args = parser.parse_args(argv)

    if not TEMPLATE.exists():
        raise SimplePageError(f"missing template: {TEMPLATE}")

    template_bytes = TEMPLATE.stat().st_size
    if template_bytes > MAX_TEMPLATE_BYTES:
        raise SimplePageError(
            f"{TEMPLATE.name} is {template_bytes / 1024:.0f} KB, over the "
            f"{MAX_TEMPLATE_BYTES // 1024} KB source-file limit"
        )

    payload = build_payload()
    assert_payload_usable(payload)

    out = inject(TEMPLATE.read_text(encoding="utf-8"), payload)
    assert_output_clean(out)

    print(f"template {template_bytes / 1024:.0f} KB, "
          f"{payload['n_positions']} positions as of {payload['as_of']}")
    print("  " + " · ".join(
        f"{s['label']} {s['weight'] * 100:.1f}%" for s in payload["sleeves"]))
    print(f"  curve {len(payload['curve']['dates'])} points "
          f"{payload['stats']['start']} -> {payload['stats']['end']}")
    if args.check:
        print(f"check only — would write {len(out) / 1024:.0f} KB")
        return 0

    PAYLOAD_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"wrote {PAYLOAD_PATH.relative_to(ROOT)} "
          f"({PAYLOAD_PATH.stat().st_size / 1024:.0f} KB)")
    print(f"wrote {OUT_PATH.relative_to(ROOT)} "
          f"({OUT_PATH.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplePageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
