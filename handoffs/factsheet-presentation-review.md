# Factsheet presentation review

## Status and scope

Working directory: `C:\dev\breadth-thrust-etf\.component-mail\factsheet-presentation`.
Branch: `codex/factsheet-presentation`, based on production `c961120`.
This is a no-send presentation implementation for owner review, not a deployed
change. No delivery ledger, recipient configuration, schedule, source dataset,
portfolio construction or published instruction has been changed.

The development checkout has an unfinished merge and unrelated holdings work.
It was not rebased, resolved or edited. The Word review is untouched.

## What the reader receives

The short email restores headline performance, indicative return drivers,
holding-price moves, the largest six proposed changes and their signal context,
allocation, risk-rule thresholds and venue-specific fill dates. It states how
many changes are outside the summary. Every D change also appears in its own
confirmation section, so small D resizes cannot disappear behind larger core
moves. The complete HTML and JSON books remain attached.

A deterministic PDF attachment adds every proposed change and signal explanation,
all held-to-target lines, all model-held price returns with proxy labels, and
readiness/provenance. The two real-snapshot design PDFs are six pages each.
The sender has a portfolio display name and a substantive plain-text alternative.

Archetype: short dossier in the email, complete reference dossier in the PDF.
The design is light by default, with an explicit dark-mode override. Browser
checks do not establish Gmail/Outlook inbox rendering; an authorised owner-only
mail-client test remains useful before broader rollout.

## Data boundaries

- Presentation sources are read from the release's archived commit and checked
  against its existing source hashes, never from an unverified newer dataset.
- Core/Europe identities and delivery eligibility are unchanged. PDF and plain
  text are included in the existing candidate digest and durable reservation.
- Whole-book performance is explicitly provisional while D is HOLD. A later D
  valuation can change performance without changing the verified core orders.
- Sleeve attribution is labelled an approximation using decision-date allocation
  times exact-window sleeve returns. The difference from the blend is shown only
  with complete endpoint coverage. Missing endpoints are never forward-filled.
- Individual price returns are not presented as exact portfolio contributions.
  Quote proxies are disclosed; no European FX leg is invented.
- The legacy `risk_overlay.current_breadth` scalar is deliberately omitted: the
  initial and consolidated archives carry different values despite the same
  `panel_end_date`/`gate_feed_last_bar`. A date label alone does not prove this
  scalar represents the gate's current observation. Verified book state and the
  recorded rule thresholds remain; tracing the scalar producer is separate work.
- No assertion that a past model rebalance was actually executed is restored.
- Old PDF benchmark blanks, approximate YTD holding contributions and historical
  charts are not copied without compatible sealed inputs. This is not a claim of
  pixel-for-pixel or section-for-section parity with the legacy PDF.

## Reproduce the previews

Use the production Python interpreter and the isolated branch:

```powershell
python tests/preview_factsheet_presentation.py c734490 initial-review
python tests/preview_factsheet_presentation.py a5e46de consolidated-review
python -m pytest tests/test_factsheet_presentation.py tests/test_component_sender.py tests/test_hold_rounding.py -q
node tests/check_component_email_mobile.cjs
```

The preview script reconstructs the archived inputs in a temporary directory,
revalidates them at their sealed clock, and writes only local design previews
under `.component-mail`. It does not clear or modify the real delivery ledger,
invoke SMTP, rerank a strategy or reseal production.

Read the initial/consolidated `.html` and `.pdf` files under `.component-mail`.
The pages are explicitly marked no-send previews based on already-sent snapshots.

## Verification

Static checks passed for all four HTML surfaces. The static dark-mode detector
warned, but real rendering verified both explicit theme states. Across 32 browser
checks, viewport and scroll width matched at 390/844/768/1280 px, overflow was zero,
minimum font was 13 px, and measured line lengths were 49.5/73.8/73.8/73.8 characters.
Email summaries contain six position cards; complete books contain all 24 lines.
PDF page content and breaks were checked visually and with extracted text.
The email HTML previews are approximately 10 KB each; this size observation is
not an inbox compatibility or clipping test.

The focused sender/presentation/rounding run passed 51 tests. After the final
provisional-performance wording and scalar omission, all 11 presentation tests
were rerun and passed. The full repository suite was not rerun for this draft.

## Next action and rollback

Owner review of the presentation comes before publication or another email.
Confirm the existing decision-date snapshot and proposed fill dates in the preview;
these are not a request to change those dates or submit orders.

After approval, rerun relevant checks, publish only this isolated change and
fast-forward the automation clone only while it is idle and clean. Preserve
all confirmed and pending delivery records. The already-delivered anchor must
not be resent simply because the formatting changed. A special test email or
redistribution needs an explicit audience and authorisation.

Rollback is a revert of this presentation change, not a reset of data or receipts.
No additional PDF library is needed: ReportLab is already in requirements.txt.
