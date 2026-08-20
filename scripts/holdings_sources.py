"""Pluggable current-holdings sources for the theme-constituent monitor.

WHAT THIS IS NOT. This is not the breadth-panel roster path. That path
(``fetch_constituents.py`` + ``etf_registry.py``) exists to serve a
BACKTEST, so it needs point-in-time rosters on arbitrary historical dates,
and it is bound to one issuer's API because that issuer is the only one
serving them. This module serves a MONITOR, which needs only the current
roster, so it can take each issuer's own daily publication and is not
bound to anybody. Keep the two separate: nothing here may be used to build
a breadth panel or feed a strategy, because a today-only roster carries no
history and would stamp present membership onto the past.

ADDING A FUND is a ``MONITOR_FUNDS`` entry plus, if its issuer is new, one
adapter function. An adapter takes the registry config and returns a
``RosterSnapshot``. That is the whole contract.

THE AS-OF DATE IS THE ISSUER'S, NEVER ``today()``. Every adapter must read
the publication date out of the file itself. A snapshot stamped with the
fetch date would silently relabel a stale file as current, which is the
same defect this repository has already been bitten by twice (the tilt feed
freezing behind an unchanged ``signal_as_of``, and the EDGAR path reading
the wrong period tag). If an issuer's file carries no date, the adapter
must fail rather than substitute one.

DROPPED ROWS ARE COUNTED, NEVER SILENT. Every row not admitted to the
roster is returned in ``dropped`` with a reason. A holdings file contains
cash lines, unsettled-trade placeholders and Bloomberg composites; dropping
them is correct, dropping them invisibly is not, because a roster that
quietly lost a tenth of its names still looks entirely healthy.

Run standalone to check a source is alive:
    python scripts/holdings_sources.py --etf ARKG
    python scripts/holdings_sources.py --all
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Issuer sites serve these files to browsers. A bare python-requests UA is
# refused by some CDNs, so identify as a browser but keep a contact string
# so an operator on the other end can reach us. Request volume is one file
# per fund per day, which is well inside any fair-use expectation.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
    "navigo-research-monitor (contact: research@navigo.sg)"
)
HTTP_HEADERS = {"User-Agent": UA}
HTTP_TIMEOUT = 30

# A published roster older than this is a capture fault, not a slow week.
# US ETFs publish every business day; three sessions of slack absorbs a
# long weekend plus a public holiday without crying wolf.
MAX_ROSTER_AGE_DAYS = 5


class HoldingsSourceError(RuntimeError):
    """Raised when a roster cannot be fetched or parsed safely."""


@dataclass(frozen=True)
class Holding:
    """One admitted equity line of a published roster."""
    ticker: str
    name: str
    weight_pct: float
    shares: float | None = None
    cusip: str | None = None
    sector: str | None = None


@dataclass
class RosterSnapshot:
    """One fund's published roster, as of the issuer's own stated date."""
    etf: str
    as_of: date
    source: str
    url: str
    holdings: list[Holding] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    fetched_at_utc: str = ""

    @property
    def weight_sum(self) -> float:
        return round(sum(h.weight_pct for h in self.holdings), 4)

    @property
    def tickers(self) -> list[str]:
        return [h.ticker for h in self.holdings]

    def to_dict(self) -> dict:
        return {
            "etf": self.etf,
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "url": self.url,
            "fetched_at_utc": self.fetched_at_utc,
            "n_holdings": len(self.holdings),
            "weight_sum_pct": self.weight_sum,
            "holdings": [
                {
                    "ticker": h.ticker, "name": h.name, "weight_pct": h.weight_pct,
                    "shares": h.shares, "cusip": h.cusip, "sector": h.sector,
                }
                for h in self.holdings
            ],
            "dropped": self.dropped,
        }


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------

# A US equity symbol is one to five letters, optionally with a class suffix.
# Anything starting with a digit is an issuer-internal placeholder: SSGA
# uses rows like "2200963D" for unsettled or pending positions, which are
# real exposures but not tradable symbols and resolve at no price vendor.
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,6}(-[A-Z])?$")

# Cash, futures margin and total rows carry these in the ticker column.
_NON_EQUITY_MARKERS = {"-", "--", "", "CASH", "USD", "N/A", "NA", "TOTAL"}


def normalise_ticker(
    raw: str | None, overrides: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(symbol, drop_reason)``. Exactly one of the two is None.

    Bloomberg composites ("ARCT UQ", where UQ is Bloomberg's Nasdaq code)
    are REJECTED rather than salvaged by stripping the venue token. That
    matches ``fetch_constituents._us_symbol`` and its reasoning, which is
    worth restating because the temptation to strip is real: a composite
    tells us the upstream field is not the field we believe it to be, and
    recovering "ARCT" from it is a guess that happens to be right today.
    The cost of rejecting is a named, counted, visible drop; the cost of
    guessing wrong is a security that never existed sitting in a roster.

    The escape hatch is an explicit per-fund ``ticker_overrides`` entry —
    the same mechanism ``etf_registry.py`` uses. An override is not a
    guess: it is an operator recording a mapping verified against two
    sources, with the verification noted beside it. That distinction is
    the whole point, so overrides are matched on the RAW upstream string
    and never inferred by pattern.

    Class shares use a dot upstream ("BRK.B") and a dash at the price
    vendor ("BRK-B"), which is a documented convention rather than a guess,
    so that one IS translated.
    """
    if raw is None:
        return None, "empty"
    s = str(raw).strip().upper()
    if overrides:
        mapped = overrides.get(s)
        if mapped is not None:
            return mapped.strip().upper(), None
    if s in _NON_EQUITY_MARKERS or s == "NAN":
        return None, "non-equity row"
    if any(c.isspace() for c in s):
        return None, "bloomberg composite (venue-coded symbol, not a ticker)"
    s = s.rstrip(".").replace(".", "-")
    if not _US_TICKER_RE.match(s):
        return None, "not a US equity symbol"
    return s, None


# ---------------------------------------------------------------------------
# Adapters — one per issuer publication format
# ---------------------------------------------------------------------------


def _http_get(url: str) -> requests.Response:
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise HoldingsSourceError(f"transport failed for {url}: {exc}") from exc
    if r.status_code != 200:
        raise HoldingsSourceError(f"HTTP {r.status_code} for {url}")
    if not r.content:
        raise HoldingsSourceError(f"empty body for {url}")
    return r


def adapter_ark_csv(cfg: dict) -> RosterSnapshot:
    """ARK Invest daily holdings CSV.

    Columns: date, fund, company, ticker, cusip, shares, market value ($),
    weight (%). The date column carries the publication date per row in
    US m/d/Y, which is the fund's own as-of.

    ARK renames funds and the CSV path follows the name — the pre-2025 path
    carried "MULTISECTOR" for ARKG and now 404s. When this adapter starts
    failing, re-resolve the link from ARK's own document table rather than
    guessing the new slug: GET /api/fund/document-table/<fundId> on
    www.ark-funds.com returns the current CSV href.
    """
    r = _http_get(cfg["url"])
    try:
        df = pd.read_csv(io.StringIO(r.text))
    except Exception as exc:
        raise HoldingsSourceError(f"ARK CSV unparseable: {exc}") from exc
    need = {"date", "ticker", "company", "weight (%)", "shares"}
    missing = need - set(df.columns)
    if missing:
        raise HoldingsSourceError(f"ARK CSV missing columns {sorted(missing)}")

    df = df[df["date"].notna()]
    if df.empty:
        raise HoldingsSourceError("ARK CSV has no dated rows")
    # Python months are 1-indexed; ARK publishes US m/d/Y.
    raw_date = str(df["date"].iloc[0]).strip()
    try:
        as_of = datetime.strptime(raw_date, "%m/%d/%Y").date()
    except ValueError as exc:
        raise HoldingsSourceError(
            f"ARK CSV date {raw_date!r} not in expected m/d/Y form"
        ) from exc

    overrides = cfg.get("ticker_overrides") or {}
    holds, dropped = [], []
    for _, row in df.iterrows():
        sym, reason = normalise_ticker(row.get("ticker"), overrides)
        name = str(row.get("company") or "").strip()
        if sym is None:
            dropped.append({"raw": str(row.get("ticker")), "name": name,
                            "reason": reason})
            continue
        holds.append(Holding(
            ticker=sym, name=name,
            weight_pct=_pct(row.get("weight (%)")),
            shares=_num(row.get("shares")),
            cusip=(str(row.get("cusip")).strip() or None),
            sector=None,   # ARK does not publish a sector column.
        ))
    return RosterSnapshot(
        etf=cfg["etf"], as_of=as_of, source="ark_csv", url=cfg["url"],
        holdings=holds, dropped=dropped,
        fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def adapter_ssga_xlsx(cfg: dict) -> RosterSnapshot:
    """State Street (SPDR) daily holdings workbook.

    Preamble rows carry "Holdings: As of 18-Aug-2026"; the table header sits
    at row index 4 with Name / Ticker / Identifier / SEDOL / Weight /
    Sector / Shares Held / Local Currency.

    SSGA serves a Sector column but leaves it "-" for single-sector funds
    such as XBI, so ``sector`` comes back None rather than a dash. Do not
    read that as a parse failure; for a biotech fund the column carries no
    information even when populated.
    """
    r = _http_get(cfg["url"])
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    as_of = _ssga_as_of(raw)

    header_row = cfg.get("header_row", 4)
    df = pd.read_excel(io.BytesIO(r.content), header=header_row)
    need = {"Name", "Ticker", "Weight"}
    missing = need - set(df.columns)
    if missing:
        raise HoldingsSourceError(f"SSGA workbook missing columns {sorted(missing)}")
    df = df[df["Ticker"].notna()]

    overrides = cfg.get("ticker_overrides") or {}
    holds, dropped = [], []
    for _, row in df.iterrows():
        sym, reason = normalise_ticker(row.get("Ticker"), overrides)
        name = str(row.get("Name") or "").strip()
        if sym is None:
            dropped.append({"raw": str(row.get("Ticker")), "name": name,
                            "reason": reason})
            continue
        sec = str(row.get("Sector") or "").strip()
        holds.append(Holding(
            ticker=sym, name=name,
            weight_pct=_pct(row.get("Weight")),
            shares=_num(row.get("Shares Held")),
            cusip=(str(row.get("Identifier")).strip() or None),
            sector=(sec if sec and sec != "-" else None),
        ))
    return RosterSnapshot(
        etf=cfg["etf"], as_of=as_of, source="ssga_xlsx", url=cfg["url"],
        holdings=holds, dropped=dropped,
        fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _ssga_as_of(raw: pd.DataFrame) -> date:
    """Pull "As of DD-Mon-YYYY" out of the workbook preamble.

    Scans the first column-pair of the first ten rows rather than pinning a
    cell, because SSGA has moved the preamble before. Fails rather than
    defaulting: an undated roster must not be stamped with today.
    """
    for r_i in range(min(10, len(raw))):
        for c_i in range(min(3, raw.shape[1])):
            cell = str(raw.iat[r_i, c_i])
            m = re.search(r"As of\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", cell)
            if m:
                # Python months are 1-indexed; %b is the abbreviated month.
                return datetime.strptime(m.group(1), "%d-%b-%Y").date()
    raise HoldingsSourceError(
        "SSGA workbook carries no 'As of' date in its preamble; refusing to "
        "substitute the fetch date"
    )


def _num(v) -> float | None:
    """Parse a possibly comma-grouped, possibly $-prefixed number."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", "nan", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v) -> float:
    """Weights arrive as "9.63%" from ARK and as 1.6497 from SSGA."""
    n = _num(v)
    return 0.0 if n is None else float(n)


ADAPTERS = {
    "ark_csv": adapter_ark_csv,
    "ssga_xlsx": adapter_ssga_xlsx,
}


# ---------------------------------------------------------------------------
# The registry — adding a fund happens here
# ---------------------------------------------------------------------------

MONITOR_FUNDS: dict[str, dict] = {
    "ARKG": {
        "etf": "ARKG",
        "label": "ARK Genomic Revolution",
        "issuer": "ARK Invest",
        "adapter": "ark_csv",
        "url": ("https://assets.ark-funds.com/fund-documents/funds-etf-csv/"
                "ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv"),
        # ACTIVELY managed, so a change in share count is the manager's
        # decision. This is what makes the flow view worth reading, and it
        # is why the page shows flow for this fund and not for an index one.
        "active": True,
        # Expected roster size, used by the guard as a sanity band rather
        # than a hard contract. Measured 2026-08-19: 33 admitted lines.
        "expected_holdings": (25, 60),
        # VERIFIED MAPPINGS, not guesses. ARK serves two lines as Bloomberg
        # composites, which the normaliser rejects by default. Each was
        # confirmed 2026-08-19 against two independent sources before being
        # admitted here — ARK's own company field and the price vendor's
        # security name — and both are Nasdaq Global Market equities, which
        # is consistent with Bloomberg's UQ venue code:
        #   "ARCT UQ"  ARK: "ARCTURUS THERAPEUTICS HOLDIN"
        #              vendor: "Arcturus Therapeutics Holdings Inc." (NGM)
        #              CUSIP 03969T109, 1.29% of fund
        #   "ATAI UQ"  ARK: "ATAIBECKLEY INC"
        #              vendor: "AtaiBeckley Inc." (NGM)
        #              CUSIP 04650F101, 0.00% of fund (residual line)
        # If a future composite appears, it is DROPPED until someone repeats
        # this verification. Do not pattern-strip the venue token.
        "ticker_overrides": {"ARCT UQ": "ARCT", "ATAI UQ": "ATAI"},
    },
    "XBI": {
        "etf": "XBI",
        "label": "SPDR S&P Biotech (equal-weight)",
        "issuer": "State Street (SPDR)",
        "adapter": "ssga_xlsx",
        "url": ("https://www.ssga.com/us/en/intermediary/library-content/"
                "products/fund-data/etfs/us/holdings-daily-us-en-xbi.xlsx"),
        "header_row": 4,
        # Equal-weight INDEX fund: share-count changes are mechanical
        # rebalancing, not conviction, so flow is suppressed on the page.
        # Reading it as a signal would be reading noise as information.
        "active": False,
        # Measured 2026-08-19: 150 admitted lines out of 157 ticker rows.
        "expected_holdings": (100, 220),
    },
}


def fetch_roster(etf: str) -> RosterSnapshot:
    """Fetch and normalise one registered fund's current roster."""
    cfg = MONITOR_FUNDS.get(etf.upper())
    if cfg is None:
        raise HoldingsSourceError(
            f"{etf} is not registered in MONITOR_FUNDS "
            f"(have: {', '.join(sorted(MONITOR_FUNDS))})"
        )
    adapter = ADAPTERS.get(cfg["adapter"])
    if adapter is None:
        raise HoldingsSourceError(f"unknown adapter {cfg['adapter']!r} for {etf}")
    snap = adapter(cfg)
    if not snap.holdings:
        raise HoldingsSourceError(f"{etf} roster parsed to zero holdings")
    return snap


def roster_age_days(snap: RosterSnapshot, today: date) -> int:
    return (today - snap.as_of).days


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf", help="single fund to probe")
    p.add_argument("--all", action="store_true", help="probe every registered fund")
    a = p.parse_args(argv)
    targets = ([a.etf.upper()] if a.etf
               else sorted(MONITOR_FUNDS) if a.all else sorted(MONITOR_FUNDS))
    rc = 0
    today = datetime.now(timezone.utc).date()
    for etf in targets:
        try:
            s = fetch_roster(etf)
        except HoldingsSourceError as exc:
            print(f"FAIL {etf}: {exc}")
            rc = 1
            continue
        age = roster_age_days(s, today)
        flag = "OK " if age <= MAX_ROSTER_AGE_DAYS else "OLD"
        print(f"{flag} {etf:6} as_of={s.as_of} age={age}d "
              f"n={len(s.holdings):3d} weight_sum={s.weight_sum:.2f}% "
              f"dropped={len(s.dropped)} src={s.source}")
        for d in s.dropped:
            print(f"      dropped {d['raw']!r} — {d['reason']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
