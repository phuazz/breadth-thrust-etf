# Capture integrity repair

Working directory: `C:\dev\breadth-thrust-etf`.

## Scope and status

Implemented capture-chain repairs from the newest weekly constituent snapshot
through prices, breadth, live instruction and pre-trade checks. Historical
membership, registered coverage floors, whole-column price-source selection
and the adopted Friday-rank/Monday-fill cadence are unchanged.

The system-design and testing-strategy skills informed the dependency checks
and failure-case regression tests. This is an engineering repair, not a new
strategy or a restatement of research conclusions.

## Changes

- Revalidate the newest weekly roster; retry recent negative or wrong-date caches.
- Retry wholly missing active-member history and missing completed-session rows.
- Reject cache reuse when required columns or the exact required close are absent.
- Record roster fingerprint, capture session and named price gaps in breadth JSON.
- Preserve the complete ranking universe and stop downstream work after failed capture.
- Warn on known capture shortfalls even inside ordinary weekly age tolerances.
- Check the four-sleeve instruction and A/D panels before the next fill.
- Keep capture dates and repair commands visible in the phone Data Health layout.

## Verification

- Full Python suite: **2,220 passed, 26 skipped**. The 127 warnings are datetime deprecations.
- Isolated live European energy panel: 26/26 active members priced through the
  required completed session, 2026-09-10; panel current.
- Isolated live US broad-market panel: 503/504 active members priced through the
  same required session; panel current under the unchanged breadth coverage rule.
  One lagging member remains named in the evidence. Its cause is not established.
- Evidence: ignored `logs/capture-smoke/exh1/` and `logs/capture-smoke/csp1/`.
  Production data files were not replaced by these tests.
- Static dashboard check: 0 failures; existing light-only theme warning.
- Rendered check: all 14 tabs at four viewports; **56 checks, no browser errors,
  no body overflow, minimum visible font 11 px**. Charts and fonts loaded over
  the network; measurements waited for the normal chart reflow to settle.
- Data Health measured client/scroll widths: 390/390, 844/844, 768/768, 1280/1280.
  Prose measure: 40 characters on the phone, 68 at the other widths.
- Reproduce browser checks with `tools/verify_health_viewports.cjs` against a
  local server on port 8765. Full measurements and screenshots are in `logs/`.

## Deployment boundary and next step

The full production refresh has not run on this version. The dedicated scheduler
checkout pulls origin before running and will pick up the repair after it is
pushed. Its ordinary weekend runs begin at 09:00 SGT. Review positions after a
successful full run; the independent Sunday 14:00 SGT check remains the guard,
not the earliest possible review time. Vendor publication can delay finality.

Do not claim the whole book is current from these two smoke tests. Do not
substitute prices, delete lagging members or lower the registered coverage floor
to make a warning disappear. No orders or manual emails were sent.

Preserve the unrelated untracked Word document under `reviews/`. Temporary
pytest directories are test fixtures, not source or live data, and must not be
included in the commit.
