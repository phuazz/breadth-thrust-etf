# WS21 measurements — M1 to M4 (2026-09-19)

Seen window 2024-01-11 to 2026-07-02, 620 NYSE sessions. Disclosure only: no performance gate governs adoption, and the construction was fixed at registration.

| Measurement | Result | Stop condition |
|---|---|---|
| **M1** premium/discount, IBIT vs Binance BTCUSDT at the NYSE close | median -0.002%, p5 -0.203%, p95 +0.208%, worst +0.676% (2024-01-12); cumulative ratio 0.9809 to 1.0000 over 619 pairs | **PASS** (band ±1.0% on p5–p95) |
| **M2** restatement, sleeve C | 2 of 129 weekly baskets differ, 0 gate-state flip(s); Sharpe +0.784 → +0.774, CAGR +19.5% → +19.3%, MaxDD -25.1% → -25.3%, turnover 21.68 → 22.00/yr; blend Sharpe +1.7776 → +1.7723 | disclosure, no gate |
| **M3** join behaviour, 2024-01-11 to 2024-10-25 | max absolute signal difference 0.0704 on 2024-02-28 (+0.8283 vs +0.7579), median 0.0077 over 200 sessions | disclosure, no gate |
| **M4** source agreement, Norgate vs Yahoo on IBIT | max relative difference 0.00e+00 over 620 sessions, 620 of them bit-identical — both feeds carry the same cent-level close at float32 precision, so the floor here is quantisation, not a tolerance approached | **PASS** (threshold 1e-4) |

**Reading M1.** Both legs are taken at the same instant, so the timing offset is controlled. The remaining daily difference is not premium/discount alone — it also carries movement in the USDT/USD basis (Binance quotes Tether) and IBIT's daily expense accrual, neither separated here. The stop band bounds the three together. The cumulative ratio (0.9809 to 1.0000) is NOT attributable from this measurement and must not be read as a premium/discount drift: it bundles the same three terms over the whole window. Decomposing them was out of scope and was not done.

Hard cap: nothing after 2026-07-02 is computed under the staged basis before the WS7 verdict is filed.

Sources: thematic_prices_cache.parquet (norgate), IBIT from norgate TOTALRETURN, column basis `ibit-spliced@7ab4a26a7dcf`.
