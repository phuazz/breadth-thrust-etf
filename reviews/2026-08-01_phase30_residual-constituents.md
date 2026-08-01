# Phase 30 residual constituents — classification and recovery plan (2026-08-01)

After the Phase 30 resolver fix, 141 of the 475 historical constituent
identifiers across the five European sector universes still have no yfinance
price history. They decompose as follows (classification rules are the
regex / explicit lists in the session script; counts measured against the
rebuilt `prices_cache_ex*.parquet` panels):

| ETF | Universe | With data | No data | Bloomberg placeholders | Rights / temp lines | Alias candidates | Genuine delistings |
|---|---:|---:|---:|---:|---:|---:|---:|
| EXH1 | 46 | 32 | 14 | 3 | 0 | 5 | 6 |
| EXH3 | 210 | 148 | 62 | 9 | 13 | 12 | 28 |
| EXH9 | 58 | 33 | 25 | 1 | 14 | 3 | 7 |
| EXV1 | 83 | 67 | 16 | 0 | 2 | 5 | 9 |
| EXV3 | 78 | 54 | 24 | 0 | 3 | 5 | 16 |
| **Total** | **475** | **334** | **141** | **13** | **32** | **30** | **66** |

## Buckets

1. **Bloomberg placeholders (13)** — numeric codes ending in `D`
   (e.g. `1581372D.L`). These are Bloomberg's own identifiers for dead
   companies, printed by older iShares CSV vintages in place of the
   historical exchange ticker. Only the company NAME column in the raw
   CSVs identifies them. They also resolve directly on a Bloomberg
   terminal, which makes a one-off terminal export the cheapest recovery
   route for exactly these rows.

2. **Rights / temporary entitlement lines (32)** — subscription rights,
   paid/nil-paid lines and temporary shares (`-BTA-`/`-TR-` Stockholm,
   `...DS.PA`, `DIED*.LS`, `ELI20/24.BR`, LSE `F`/`N` paid lines, etc.).
   Non-tradable or transient by construction; correctly absent from
   yfinance. A future refinement could exclude them from rosters the way
   `.RI` rights already are; they appear for a week or two each and only
   marginally inflate `n_constituents`.

3. **Alias candidates (30)** — old or variant roots whose company (or
   successor line) likely still trades with full back-history under a
   different symbol. Wiring these as registry `ticker_overrides` recovers
   history at zero cost, but **each mapping must be verified against two
   sources before use** (vault data-integrity rule). Candidate map as
   hypothesised, NOT yet verified:
   FP.PA→TTE.PA, STL.OL→EQNR.OL, AKERBP.OL→AKRBP.OL, RDSA.AS→SHELL.AS,
   SSO.OL→SCATC.OL, DPW.DE→DHL.DE, MOCORP.HE→METSO.HE, GAS.MC→NTGY.MC,
   REE.MC→RED.MC, AND.VI→ANDR.VI, STM.MI→STMMI.MI, BESIT.AS→BESI.AS,
   NDA.ST→NDA-SE.ST, SHBA.ST→SHB-A.ST, VOLVB.ST→VOLV-B.ST,
   LOOM-B.ST→LOOM.ST, DNBH.OL→DNB.OL, UCGIM.MI→UCG.MI,
   BAAKOMB.PR→KOMB.PR, ROLLS.L→RR.L, FI.N.SW→FI-N.SW, ECM.L→RS1.L,
   RMG.L→IDS.L (successor itself delisted 2024), CGCBV.HE→HIAB.HE,
   G24B.DE/G24B.HM→G24.DE, KGXA.DE→KGX.DE, IGYB.DE→IGY.DE, CNHI.MI→CNHI,
   SCHA.OL→VEND.OL.

4. **Genuine delistings (66)** — 2018-2026 M&A, take-privates,
   nationalisations and collapses (Wirecard, Credit Suisse, Bankia, UBI,
   Aggreko, Meggitt, Ultra, G4S, Homeserve, Signature Aviation, DS Smith,
   Just Eat Takeaway, Darktrace, Deliveroo, Avast, Aveva, Micro Focus,
   Iliad, Ingenico, Software AG, Osram, innogy, Uniper, EDF, Suez, Neoen,
   Atlantia, Abertis, Siemens Gamesa, PGNiG, Lundin, Natixis, Virgin
   Money, ...). Yahoo withdraws the entire history at delisting, so these
   are unrecoverable from yfinance regardless of identifier. This is the
   licensed-source target set. Note several 2025-2026 events (Wood Group,
   SIG, Banca Popolare di Sondrio, Santander Bank Polska, Fortnox,
   Svitzer, Varta, Ashtead's US listing move) — the set grows by roughly
   5-15 names per year, so a durable source beats a one-off patch.

## Recovery plan (from the 2026-08-01 licensed-source assessment)

1. **EODHD All World (US$19.99/month, self-serve)** — the only verified
   self-serve API exposing delisted European tickers per exchange
   (`exchange-symbol-list/{EXCHANGE}?delisted=1`, Name + ISIN fields).
   Risk: vendor states non-US delisted coverage is concentrated in the
   most recent 6-7 years, so 2018-2019 delistings need an empirical hit-
   rate check. Protocol: one paid month; pull delisted lists for all ~16
   venue codes; match the 66-name target set (plus failed aliases) by
   name; count hits per delisting year BEFORE building anything on it.
2. **Bloomberg terminal one-off export** — the 13 placeholder codes are
   native Bloomberg tickers; a few hundred names of daily `PX_LAST` /
   `TOT_RETURN_INDEX_GROSS_DVDS` sit comfortably inside the Excel add-in
   caps (~500k hits/day, ~5-7k unique IDs/month per university-library
   documentation). Use as gap-filler and as the independent cross-check
   on ~20 random EODHD series (adjustment correctness) — this doubles as
   the guard layer required for any unattended integration.
3. **EDI (Exchange Data International)** — institutional fallback if the
   EODHD hit rate disappoints; explicitly offers one-off historical
   purchases and free coverage checks against an ISIN/name list.
4. **Not viable**: Norgate (US/AU/CA only — confirmed), Tiingo (US/CN),
   Finnhub, Twelve Data, Marketstack, Alpha Vantage (delisted endpoint is
   US-only), Stooq (undocumented); LSEG Datastream/Workspace covers
   everything but at ~US$12-30k/yr is out of proportion for this project.

STOXX historical composition files are licensed-only (current lists are
free), so the weekly iShares point-in-time snapshots remain the membership
source; the licensed need is prices only.
