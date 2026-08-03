"""Cross-sectional ETF scanner — universe resolver and drift guard.

The scanner monitors the same 54 instruments the four deployed sleeves
act on. Nothing about that list is retyped here: it is derived from the
engines themselves, because a hand-maintained copy would drift the first
time a sleeve gained or lost a member and nothing would fail.

Sources, and why there are three rather than one:

* Sleeve A  -- ``etf_registry.UNIVERSE_ETFS``, mapped through each entry's
  ``yfinance_trading_proxy`` field (CSP1 -> SPY, IUES -> XLE, ...). SOXX
  has no proxy and is scanned directly.
* Sleeve D  -- ``etf_registry.UNIVERSE_EUROPE_SECTORS``, same proxy field
  (EXV1 -> EXV1.DE), Xetra-listed and EUR-denominated.
* Sleeve B  -- ``run_asset_class_rotation.UNIVERSE``. Not in the registry.
  SHY is excluded automatically: it lives in ``CASH_ONLY_TICKERS``, not
  ``TICKERS``, so it is not a rotation candidate and never enters here.
* Sleeve C  -- ``run_thematic_rotation.UNIVERSE``. Not in the registry.
* Overlay   -- ``run_risk_overlay.EEM_TICKER``.

Exactly one substitution is declared by hand (``DECLARED_SUBSTITUTIONS``),
and it is the crypto line: the engine ranks BTC-USD because IBIT's
history starts 2024-01-11 and cannot support a five-year walk-forward,
but live execution is IBIT. The scanner monitors what is traded.

Two guards, both fatal rather than advisory:

1. **Explanation invariant** -- every scanned ticker either equals its
   engine ticker, or came from the registry's proxy field, or is a
   declared substitution. A future edit that hardcodes a ticker fails.
2. **Manifest reconciliation** -- the derived set is compared against
   ``data/scanner_universe_manifest.json``. A deliberate universe change
   means regenerating the manifest in the same commit, so the change is
   visible in the diff instead of arriving silently on the page.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from etf_registry import (  # noqa: E402
    ETF_REGISTRY,
    UNIVERSE_ETFS,
    UNIVERSE_EUROPE_SECTORS,
)

MANIFEST_PATH = ROOT / "data" / "scanner_universe_manifest.json"

SLEEVE_A = "A"
SLEEVE_B = "B"
SLEEVE_C = "C"
SLEEVE_D = "D"
SLEEVE_OV = "OV"
SLEEVE_ORDER = (SLEEVE_A, SLEEVE_B, SLEEVE_C, SLEEVE_D, SLEEVE_OV)

SLEEVE_LABELS = {
    SLEEVE_A: "Sectors / concentrated (breadth)",
    SLEEVE_B: "Asset-class momentum",
    SLEEVE_C: "Thematic momentum",
    SLEEVE_D: "Europe sectors (breadth)",
    SLEEVE_OV: "Overlay",
}

# The one hand-declared substitution. Keep this dict as close to empty as
# the engines allow; every entry is a place where the page shows something
# other than what the engine ranks, and each needs a code citation.
DECLARED_SUBSTITUTIONS: dict[str, tuple[str, str]] = {
    "BTC-USD": (
        "IBIT",
        "run_thematic_rotation.py:180 — BTC-USD is the long-history "
        "backtest series (IBIT's 25bps expense applied as price drag); "
        "live execution is IBIT, so the scanner monitors IBIT",
    ),
}

# FX conversion to USD. Direction is spelled out because getting it
# backwards is a silent 2x-scale error, not a crash:
#   EURUSD=X quotes USD per 1 EUR  -> EUR price * rate = USD  (multiply)
#   USDCNY=X quotes CNY per 1 USD  -> CNY price / rate = USD  (divide)
# Matches run_europe_rotation.py:164 and check_588200ss.py:61.
FX_MULTIPLY = "multiply"
FX_DIVIDE = "divide"
FX_RULES: dict[str, tuple[str, str, str]] = {
    "EUR": ("EURUSD=X", FX_MULTIPLY, "Xetra listings quote in EUR"),
    "CNY": ("USDCNY=X", FX_DIVIDE, "Shenzhen listing quotes in CNY"),
}


class ScannerUniverseDrift(RuntimeError):
    """Raised when the derived universe disagrees with the committed manifest."""


@dataclass(frozen=True)
class Origin:
    """One sleeve's claim on a scanned ticker."""

    sleeve: str
    engine_ticker: str
    reason: str | None      # None when the scanned ticker IS the engine ticker


@dataclass(frozen=True)
class ScannerRow:
    """One row of the scanner: what to download and where it came from."""

    scan_ticker: str
    origins: tuple[Origin, ...]
    name: str | None            # engine label where one exists; else from
                                # data/etf_names.json (registry has no name field)
    currency: str
    fx_ticker: str | None
    fx_direction: str | None

    @property
    def sleeves(self) -> tuple[str, ...]:
        seen = [o.sleeve for o in self.origins]
        return tuple(s for s in SLEEVE_ORDER if s in seen)

    @property
    def is_proxied(self) -> bool:
        """True when any sleeve scans something other than what it ranks."""
        return any(o.reason is not None for o in self.origins)

    @property
    def proxy_notes(self) -> tuple[str, ...]:
        return tuple(o.reason for o in self.origins if o.reason)


def _currency_for(scan_ticker: str, engine_meta: dict | None) -> str:
    """Denomination of the scanned series.

    Xetra suffix is authoritative for EUR; an explicit ``currency`` field
    on the engine's universe entry (159801.SZ carries CNY) is honoured
    for everything else. Default USD.
    """
    if scan_ticker.endswith(".DE"):
        return "EUR"
    if engine_meta and engine_meta.get("currency"):
        return str(engine_meta["currency"])
    return "USD"


def _registry_scan_ticker(engine_ticker: str) -> tuple[str, str | None]:
    """Resolve an engine ticker through the registry's trading-proxy field."""
    entry = ETF_REGISTRY.get(engine_ticker)
    if entry is None:
        raise KeyError(
            f"{engine_ticker} is in a registry universe list but has no "
            f"ETF_REGISTRY entry — the registry is internally inconsistent"
        )
    proxy = entry.get("yfinance_trading_proxy")
    if not proxy or proxy == engine_ticker:
        return engine_ticker, None
    return proxy, (
        f"registry yfinance_trading_proxy: {engine_ticker} -> {proxy} "
        f"(UCITS line traded / breadth computed on {engine_ticker})"
    )


def _collect() -> dict[str, list[tuple[Origin, dict | None]]]:
    """Gather every sleeve's claims, keyed by scanned ticker."""
    import run_asset_class_rotation as sleeve_b
    import run_risk_overlay as overlay
    import run_thematic_rotation as sleeve_c

    claims: dict[str, list[tuple[Origin, dict | None]]] = {}

    def add(scan: str, origin: Origin, meta: dict | None) -> None:
        claims.setdefault(scan, []).append((origin, meta))

    for engine_ticker in UNIVERSE_ETFS:
        scan, reason = _registry_scan_ticker(engine_ticker)
        add(scan, Origin(SLEEVE_A, engine_ticker, reason), None)

    for engine_ticker in sleeve_b.TICKERS:
        meta = sleeve_b.UNIVERSE[engine_ticker]
        add(engine_ticker, Origin(SLEEVE_B, engine_ticker, None), meta)

    for engine_ticker in sleeve_c.TICKERS:
        meta = sleeve_c.UNIVERSE[engine_ticker]
        substitution = DECLARED_SUBSTITUTIONS.get(engine_ticker)
        if substitution:
            scan, reason = substitution
        else:
            scan, reason = engine_ticker, None
        add(scan, Origin(SLEEVE_C, engine_ticker, reason), meta)

    for engine_ticker in UNIVERSE_EUROPE_SECTORS:
        scan, reason = _registry_scan_ticker(engine_ticker)
        add(scan, Origin(SLEEVE_D, engine_ticker, reason), None)

    add(
        overlay.EEM_TICKER,
        Origin(SLEEVE_OV, overlay.EEM_TICKER, None),
        {"label": "iShares MSCI Emerging Markets (Phase 22 EM tilt instrument)"},
    )
    return claims


def resolve_universe() -> list[ScannerRow]:
    """Build the scanner universe from the deployed engines.

    Ordered by sleeve then ticker so the output is stable across runs and
    the manifest diff stays readable.
    """
    rows: list[ScannerRow] = []
    for scan_ticker, claims in _collect().items():
        origins = tuple(origin for origin, _ in claims)
        metas = [meta for _, meta in claims if meta]
        # First engine label wins; sleeve order in _collect is A,B,C,D,OV,
        # so a multi-sleeve ticker takes the label of its earliest sleeve.
        name = next(
            (str(m["label"]) for m in metas if m.get("label")),
            None,
        )
        currency = _currency_for(scan_ticker, metas[0] if metas else None)
        fx_ticker, fx_direction, _ = FX_RULES.get(currency, (None, None, None))
        rows.append(
            ScannerRow(
                scan_ticker=scan_ticker,
                origins=origins,
                name=name,
                currency=currency,
                fx_ticker=fx_ticker,
                fx_direction=fx_direction,
            )
        )

    def sort_key(row: ScannerRow) -> tuple[int, str]:
        return (SLEEVE_ORDER.index(row.sleeves[0]), row.scan_ticker)

    return sorted(rows, key=sort_key)


def names_required(rows: list[ScannerRow]) -> list[str]:
    """Scanned tickers with no engine label — these need data/etf_names.json.

    Sleeves A and D have no name anywhere in the registry (there is no
    name field), so their long names come from a committed one-off fetch.
    """
    return [r.scan_ticker for r in rows if not r.name]


def explanation_failures(rows: list[ScannerRow]) -> list[str]:
    """Guard 1 — every proxied row must trace to the registry or a declaration."""
    failures: list[str] = []
    for row in rows:
        for origin in row.origins:
            if origin.engine_ticker == row.scan_ticker:
                continue
            if origin.reason is None:
                failures.append(
                    f"{origin.sleeve}: {origin.engine_ticker} is scanned as "
                    f"{row.scan_ticker} with no recorded reason"
                )
                continue
            from_registry = origin.reason.startswith("registry ")
            declared = DECLARED_SUBSTITUTIONS.get(origin.engine_ticker, (None,))[0]
            if not from_registry and declared != row.scan_ticker:
                failures.append(
                    f"{origin.sleeve}: {origin.engine_ticker} -> "
                    f"{row.scan_ticker} is neither a registry proxy nor a "
                    f"declared substitution"
                )
    return failures


def fingerprint(rows: list[ScannerRow]) -> dict:
    """The comparable shape of a universe — what the manifest stores."""
    by_sleeve: dict[str, list[str]] = {s: [] for s in SLEEVE_ORDER}
    for row in rows:
        for sleeve in row.sleeves:
            by_sleeve[sleeve].append(row.scan_ticker)
    return {
        "row_count": len(rows),
        "sleeves": {s: sorted(by_sleeve[s]) for s in SLEEVE_ORDER},
        "substitutions": {k: v[0] for k, v in sorted(DECLARED_SUBSTITUTIONS.items())},
        "fx": {
            row.scan_ticker: row.fx_ticker
            for row in rows
            if row.fx_ticker
        },
    }


def load_manifest(path: Path = MANIFEST_PATH) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(rows: list[ScannerRow], path: Path = MANIFEST_PATH) -> None:
    """Regenerate the committed manifest — a deliberate, reviewable act."""
    payload = {
        "_comment": (
            "Derived from the deployed sleeve engines by "
            "scripts/scanner_universe.py. Do not hand-edit: regenerate with "
            "`python scripts/scanner_universe.py --write-manifest` in the same "
            "commit as the universe change, so the diff shows what moved."
        ),
        **fingerprint(rows),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def reconcile(rows: list[ScannerRow], manifest: dict | None) -> list[str]:
    """Guard 2 — differences between the derived universe and the manifest."""
    if manifest is None:
        return ["no manifest committed — run --write-manifest to establish one"]

    diffs: list[str] = []
    derived = fingerprint(rows)

    if derived["row_count"] != manifest.get("row_count"):
        diffs.append(
            f"row count {derived['row_count']} != manifest "
            f"{manifest.get('row_count')}"
        )
    for sleeve in SLEEVE_ORDER:
        got = set(derived["sleeves"][sleeve])
        want = set(manifest.get("sleeves", {}).get(sleeve, []))
        for extra in sorted(got - want):
            diffs.append(f"sleeve {sleeve}: {extra} added since the manifest")
        for missing in sorted(want - got):
            diffs.append(f"sleeve {sleeve}: {missing} removed since the manifest")
    if derived["substitutions"] != manifest.get("substitutions"):
        diffs.append(
            f"substitutions {derived['substitutions']} != manifest "
            f"{manifest.get('substitutions')}"
        )
    if derived["fx"] != manifest.get("fx"):
        diffs.append(f"FX mapping {derived['fx']} != manifest {manifest.get('fx')}")
    return diffs


def assert_reconciled(rows: list[ScannerRow] | None = None) -> list[ScannerRow]:
    """Run both guards, raising on any failure. Call this from run_scanner."""
    rows = rows if rows is not None else resolve_universe()
    problems = explanation_failures(rows) + reconcile(rows, load_manifest())
    if problems:
        raise ScannerUniverseDrift(
            "scanner universe failed reconciliation:\n  - "
            + "\n  - ".join(problems)
        )
    return rows


def _format_table(rows: list[ScannerRow]) -> str:
    lines = [
        f"{'SCAN':<11} {'SLEEVES':<9} {'ENGINE':<11} {'CCY':<4} NAME / PROXY NOTE",
        "-" * 96,
    ]
    for row in rows:
        engines = ",".join(
            sorted({o.engine_ticker for o in row.origins if o.engine_ticker != row.scan_ticker})
        ) or "="
        note = row.name or "(name from etf_names.json)"
        lines.append(
            f"{row.scan_ticker:<11} {'/'.join(row.sleeves):<9} {engines:<11} "
            f"{row.currency:<4} {note[:52]}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="run both guards and exit non-zero on drift",
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="regenerate data/scanner_universe_manifest.json",
    )
    args = parser.parse_args(argv)

    rows = resolve_universe()
    print(_format_table(rows))
    print()

    counts = {s: sum(1 for r in rows if s in r.sleeves) for s in SLEEVE_ORDER}
    print(
        "rows: "
        + ", ".join(f"{s}={counts[s]}" for s in SLEEVE_ORDER)
        + f", deduplicated total={len(rows)}"
    )
    pending = names_required(rows)
    if pending:
        print(f"names pending from etf_names.json ({len(pending)}): "
              f"{', '.join(pending)}")

    if args.write_manifest:
        write_manifest(rows)
        print(f"manifest written: {MANIFEST_PATH.relative_to(ROOT)}")
        return 0

    problems = explanation_failures(rows) + reconcile(rows, load_manifest())
    if problems:
        print("\nRECONCILIATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1 if args.check else 0
    print("reconciliation clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
