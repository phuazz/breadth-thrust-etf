"""Resolve a point-in-time roster ticker to a Norgate symbol.

WHY THIS EXISTS. Constituent prices come from yfinance, which serves history
only under a security's CURRENT symbol. Every name that was later acquired,
taken private or renamed therefore has no history under the ticker it carried
while it was in the index, and is dropped from breadth entirely. CNDX coverage
is 81.6% in 2018 and only clears 99% in 2025 for that reason alone, so the
early backtest is computed on survivors. Norgate Platinum carries ~21k
delisted securities back to 1990 and can fill them in.

THE TRAP THIS MODULE EXISTS TO AVOID. Symbols are REUSED, so resolving by
ticker alone silently attaches the wrong company's prices:

    FB            today = ProShares S&P 500 Dynamic Buffer ETF (from 2025-06-26)
                  in 2018 = Facebook, whose history now lives under META
    CA-201811     = CA Inc, acquired by Broadcom 2018-11
    CA-202605     = Xtrackers California Municipal Bond ETF, a different thing
    SPLK-200004   = Spanlink Communications
    SPLK-202403   = Splunk Inc

That is not hypothetical: the committed yfinance cache ALREADY holds 281 bars
under "FB" starting 2025-06-26, which are the ProShares ETF's, not Facebook's.
They are harmless today only because FB left the index years before that date.

So resolution is by (ticker, as-of date) against each candidate's quotation
window, never by ticker alone.

RENAMES are a separate case. Norgate keeps a renamed security's full history
under its CURRENT symbol, so there is no dated candidate to match and an
explicit map is required. Each entry below was verified against Norgate's own
``security_name`` rather than assumed from the ticker.

Requires the norgatedata package and a local Norgate installation, so this is
LOCAL-ONLY: CI has neither, which is already true of the constituent refresh.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

# Roster ticker -> the CURRENT Norgate symbol carrying that security's history.
# Renames only: the security survived, so Norgate has no dated delisted entry.
# Verified 2026-08-10 against norgatedata.security_name.
RENAMED: dict[str, str] = {
    "CTRP":  "TCOM",    # Trip.com Group ADR (renamed 2019)
    "MYL":   "VTRS",    # Mylan -> Viatris (2020)
    "SYMC":  "GEN",     # Symantec -> NortonLifeLock -> Gen Digital
    "NLOK":  "GEN",     # NortonLifeLock -> Gen Digital (2022)
    "WLTW":  "WTW",     # Willis Towers Watson (2022)
    # Liberty Interactive QVC Group A -> Qurate Retail -> QVC Group. Norgate
    # carries the whole lineage under one delisted symbol from 2006-05-04.
    "QVCA":  "QVCAQ-202608",
    "QRTEA": "QVCAQ-202608",
    # Renamed AND their old ticker later reused by an unrelated fund, which is
    # why resolution checks dated candidates before this table: FB must mean
    # Facebook in 2018 and the ProShares ETF that took the ticker in 2025.
    "FB":    "META",    # Facebook -> Meta Platforms (2022), history from 2012
    "PCLN":  "BKNG",    # Priceline -> Booking Holdings (2018)

    # --- 2026-08-10 sweep of the remaining unpriced constituents. ---------
    # EVERY entry below was checked against Norgate's security_name before
    # being written here, and that check is not ceremony: proposing the
    # obvious successor ticker produced FOURTEEN wrong answers, because the
    # old ticker had been reused. CBS/VIAC -> PARA gives "Banzai
    # International", DISCA -> WBD gives "Wimm-Bill-Dann Foods", ABC -> COR
    # gives "Crystal Oil". Each of those would have attached an unrelated
    # company's prices to an index constituent. The verified symbol is often
    # NOT the plain successor ticker.
    "ABC":   "COR",             # AmerisourceBergen -> Cencora (2023)
    "ADS":   "BFH",             # Alliance Data -> Bread Financial (2022)
    "ANTM":  "ELV",             # Anthem -> Elevance Health (2022)
    "BHGE":  "BKR",             # Baker Hughes GE -> Baker Hughes (2019)
    "BK":    "BNY",             # BNY Mellon changed ticker BK -> BNY
    "BLL":   "BALL",            # Ball Corp (2023)
    "BRKS":  "AZTA",            # Brooks Automation -> Azenta (2022)
    "CBG":   "CBRE",            # CBRE Group (2018)
    "CBS":   "PARAA-202508",    # CBS -> ViacomCBS -> Paramount Global A
    "VIAC":  "PARAA-202508",    # same lineage
    "CDAY":  "DAY-202602",      # Ceridian -> Dayforce (2024)
    "CHK":   "EXE",             # Chesapeake -> Expand Energy (2024)
    "CLI":   "VRE-202605",      # Mack-Cali -> Veris Residential (2022)
    "CLNY":  "DBRG",            # Colony Capital -> DigitalBridge (2021)
    "CREE":  "WOLF",            # Cree -> Wolfspeed (2021)
    "CTL":   "LUMN",            # CenturyLink -> Lumen (2020)
    "DDR":   "SITC",            # DDR Corp -> SITE Centers (2018)
    "DISCA": "WBD",             # Discovery -> Warner Bros. Discovery (2022)
    "DPS":   "KDP",             # Dr Pepper Snapple -> Keurig Dr Pepper (2018)
    "FBHS":  "FBIN",            # Fortune Brands Innovations (2022)
    "FCEA":  "FCE.A-201812",    # Forest City Realty Class A
    "FI":    "FISV",            # Fiserv took the FI ticker in 2024
    "FLT":   "CPAY",            # FLEETCOR -> Corpay (2024)
    "FVE":   "ALR-202303",      # Five Star Senior Living -> AlerisLife
    "GOV":   "OPITQ-202606",    # Government Properties -> Office Properties
    "GPS":   "GAP",             # Gap Inc ticker change (2024)
    "HCN":   "WELL",            # Health Care REIT -> Welltower
    "HCP":   "DOC",             # HCP -> Healthpeak -> DOC
    "PEAK":  "DOC",             # same lineage
    "HFC":   "DINO",            # HollyFrontier -> HF Sinclair (2022)
    "HPT":   "SVC",             # Hospitality Properties -> Service Properties
    "HRS":   "LHX",             # Harris -> L3Harris (2019)
    "IIVI":  "COHR",            # II-VI -> Coherent (2022)
    "JEC":   "J",               # Jacobs Engineering -> Jacobs Solutions
    "KORS":  "CPRI",            # Michael Kors -> Capri Holdings (2018)
    "LUK":   "JEF",             # Leucadia -> Jefferies Financial (2018)
    "MMC":   "MRSH",            # Marsh & McLennan ticker change (2025)
    "OFC":   "CDP",             # Corporate Office -> COPT Defense
    "PKI":   "RVTY",            # PerkinElmer -> Revvity (2023)
    "RE":    "EG",              # Everest Re -> Everest Group (2023)
    "SGH":   "PENG",            # SMART Global -> Penguin Solutions (2025)
    "SNH":   "DHC",             # Senior Housing -> Diversified Healthcare
    "TMK":   "GL",              # Torchmark -> Globe Life (2019)
    "UTX":   "RTX",             # United Technologies -> RTX (2020)
    "WRE":   "ELME",            # Washington REIT -> Elme Communities (2022)
    "WYN":   "TNL",             # Wyndham Worldwide -> Travel + Leisure
    "BPR":   "BPYU-202107",     # Brookfield Property REIT Class A

    # --- 2026-08-13 WS16 sweep of the held-window-aware residuals. -------
    # Same discipline as above: every entry verified against Norgate's
    # security_name AND quotation window before being written. The window
    # matters as much as the name — HR's live line starts 2012-06-06, which
    # is HTA's IPO date, proving it carries the HTA side of the 2022 merger
    # rather than the absorbed old Healthcare Realty (HR-202207).
    "SIVB":  "SIVBQ-202411",    # SVB Financial (failed 2023-03; OTC line
                                #   carries the lineage to 2024-11)
    "FRC":   "FRCB",            # First Republic Bank (failed 2023-05; the
                                #   FRCB line carries the lineage)
    "LB":    "BBWI",            # L Brands -> Bath & Body Works (2021);
                                #   LB reused by LandBridge from 2024-06
    "COG":   "CTRA-202605",     # Cabot -> Coterra (2021), which itself
                                #   stopped quoting 2026-05-06
    "DWDP":  "DD",              # DowDuPont era lives under DuPont de
                                #   Nemours; DD's history starts 2017-09-01,
                                #   the merger date
    "APY":   "CHX-202507",      # Apergy -> ChampionX (2020), acquired 2025
    "SATS":  "ECHO",            # EchoStar ticker change SATS -> ECHO (2026)
    "VSCO":  "VSXY",            # Victoria's Secret ticker change (2026)
    "MPW":   "MPT",             # Medical Properties Trust ticker change
                                #   (2026); MPT starts 2005-07-08, its IPO
    "AHH":   "AHRT",            # Armada Hoffler -> AH Realty Trust (2026);
                                #   AHRT starts 2013-05-08, the AHH IPO
    "PEI":   "PRETQ-202404",    # Pennsylvania REIT (delisted via OTC 2024)
    "AFIN":  "RTL-202309",      # American Finance Trust -> Necessity
                                #   Retail REIT, acquired 2023-09
    "HTA":   "HR",              # Healthcare Trust of America -> Healthcare
                                #   Realty Class A (2022 merger, HTA side)
    "IRET":  "CSR",             # Investors Real Estate Trust -> Centerspace
    "OPI":   "OPITQ-202606",    # Office Properties Income Trust, old line
                                #   to 2026-06-17 (a NEW OPI line quotes
                                #   from 2026-06-22 — era barrier applies)
    "BFB":   "BF.B",            # Brown-Forman Class B: iShares prints
                                #   "BFB", Norgate "BF.B", yfinance "BF-B"
    "WPG":   "WPGGQ-202110",    # Washington Prime Group (bankruptcy OTC
                                #   line to 2021-10)
    "RVI":   "RVIC-202304",     # Retail Value Inc (SITE Centers spin,
                                #   wound down 2023; RVI reused by a 2026
                                #   CEF, which the dated check keeps out)
}

# Roster entries that are NOT ordinary equity and cannot be priced as one.
# Both appeared in exactly ONE weekly snapshot, so neither is a real sustained
# constituent; excluding them is correct rather than a gap to be filled.
NOT_EQUITY: dict[str, str] = {
    # T-Mobile rights line, 2020-06-26 only (the SoftBank secondary).
    "TMUSR": "rights line, not an ordinary listing",
    # Bloomberg composite ("UW" = Nasdaq) that leaked through the iShares
    # ticker field, 2026-01-09 only. Now rejected upstream by
    # fetch_constituents._us_symbol; kept here so historical rosters resolve.
    "VSNTV UW": "Bloomberg composite identifier, not a ticker",
    # Warrants, when-issued lines, rights and Bloomberg composites that the
    # iShares ticker field has served over the years. None is an ordinary
    # listing, so none has a price history to attach; naming them here keeps
    # them out of the "unresolved" list, where they would read as coverage
    # still to be recovered rather than rows that should never be priced.
    "DISHR":     "rights line, not an ordinary listing",
    "MRP-W":     "warrant, not an ordinary listing",
    "SYF-W":     "warrant, not an ordinary listing",
    "OXY WS":    "warrant, not an ordinary listing",
    "OXY WS WI": "warrant, when-issued, not an ordinary listing",
    "HOLX US":   "Bloomberg composite identifier, not a ticker",
    "RTX US":    "Bloomberg composite identifier, not a ticker",
    "RTL US":    "Bloomberg composite identifier, not a ticker",
    "AFIN US":   "Bloomberg composite identifier, not a ticker",
    "1812473D":  "Bloomberg internal identifier, not a ticker",
}


class NorgateUnavailable(RuntimeError):
    """norgatedata is absent or the local Norgate service is not running."""


def _nd():
    try:
        import norgatedata as nd
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise NorgateUnavailable("norgatedata is not installed") from e
    if not nd.status():
        raise NorgateUnavailable("the local Norgate service is not running")
    return nd


@lru_cache(maxsize=1)
def _candidates() -> dict[str, list[str]]:
    """Ticker root -> every Norgate symbol sharing it, live and delisted."""
    nd = _nd()
    out: dict[str, list[str]] = {}
    for s in nd.database_symbols("US Equities"):
        out.setdefault(s, []).append(s)
    for s in nd.database_symbols("US Equities Delisted"):
        out.setdefault(s.split("-")[0], []).append(s)
    return out


def _as_date(v) -> date | None:
    """Norgate returns quotation dates as ISO strings, datetimes or None
    depending on the call; normalise before any comparison. Comparing a str
    to a date raises rather than silently mis-ordering, which is how this was
    caught, but normalising here keeps the resolver readable."""
    if v is None:
        return None
    if isinstance(v, date):
        return v if not hasattr(v, "date") else v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@lru_cache(maxsize=8192)
def _window(symbol: str) -> tuple[date | None, date | None]:
    nd = _nd()
    try:
        return (_as_date(nd.first_quoted_date(symbol)),
                _as_date(nd.last_quoted_date(symbol)))
    except Exception:
        return None, None


def resolve(ticker: str, as_of: date) -> str | None:
    """The Norgate symbol quoting ``ticker`` on ``as_of``, or None.

    None means "deliberately unresolvable" as well as "not found": a rights
    line and a Bloomberg composite both return None, because inventing a
    security for them would be worse than leaving the name unpriced.
    """
    if ticker in NOT_EQUITY:
        return None

    # DATED CANDIDATES FIRST. A symbol that was actually quoting on as_of is
    # authoritative, and checking renames first would break the reused ones:
    # FB must resolve to Facebook in 2018 and to the ProShares ETF that took
    # the ticker in 2025, which one blanket rename entry cannot express.
    best: tuple[int, str] | None = None
    for sym in _candidates().get(ticker, []):
        first, last = _window(sym)
        if not first or as_of < first:
            continue
        if last is not None and as_of > last:
            continue
        # Prefer the candidate whose window ENDS soonest after as_of: a
        # delisted line that was quoting then beats a still-live reuse of the
        # same ticker that only started later.
        rank = (last - as_of).days if last is not None else 10 ** 6
        if best is None or rank < best[0]:
            best = (rank, sym)
    if best:
        return best[1]

    # No symbol was quoting under this ticker then, which is what a RENAME
    # looks like: the security lives on under a different symbol carrying the
    # whole history, so there is nothing dated to match.
    sym = RENAMED.get(ticker)
    if sym:
        first, last = _window(sym)
        if first and first <= as_of and (last is None or as_of <= last):
            return sym
    return None


def audit(tickers: list[str], as_of: date) -> dict[str, str | None]:
    """Resolve many at once — the shape the backfill and its tests both want."""
    return {t: resolve(t, as_of) for t in tickers}
