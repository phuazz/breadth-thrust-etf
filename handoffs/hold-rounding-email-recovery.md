# Rounded D HOLD and email recovery

## Change

The failed core release rescaled the three stored D holdings from 20.002% to
20.000% of NAV. Their resulting non-zero deltas demanded current traded-fund
quotes even though D was on HOLD and its registered risk budget had not changed.
The vendor had supplied no Friday close for those funds. The quote check was
correct for the generated instructions; generating the rounding trades was not.

The builder now preserves `target == held` and `delta == 0` exactly for a D HOLD
at an unchanged registered risk budget. Recognition is limited to the two D
budgets derived from the registered base allocation and de-risk fraction, using
the existing model-held-basis rounding bound of 0.0001 NAV. This is not a new
minimum trade size and is not an exemption for moving between risk budgets.

The signed `rounding_residual_nav` is derived from the held D lines and nominal
budget. Builder and release validator reconcile target NAV to one plus this
explicit residual. It is not cash, a price, a normalisation of the baseline or
an order. Missing, null or forged non-zero residual declarations fail closed.
The failed baseline is covered with all three exact D weights and total held
NAV of 1.000055; the resulting target residual is 0.00002 NAV.

Actual risk reductions/restorations still resize D proportionally and require
current prices. A READY D rank retains the existing price and date requirements.
No Thursday substitution, forward fill, strategy parameter or recipient changes.

Book and traded-price validation now also runs directly after live target
generation, before commentary, page builds and the full regression suite. This
is an early failure check only. Every existing final seal, source hash, capture
guard and regression test remains mandatory for publication.

## Email

The email explains that D holdings are unchanged and small rounding differences
are not trades. The attachment retains the exact residual for audit. The
existing two-stage policy remains: initial verified core, then consolidated
verified D or the scheduled D-HOLD deadline. Both stages use all existing
recipients. The sender still reserves delivery before SMTP and blocks uncertain
or duplicate attempts.

The local scheduled process does not need Gmail credentials to send the
factsheet: its verified release push triggers the hosted sender, where the
three existing secret names were checked without reading their values.
Local failure-alert credentials remain a separate, unchanged issue.

## Verification and operation

Tests cover unchanged rounded HOLD, positive/negative residual recognition,
month/year boundaries, real risk transitions, missing D quotes, forged metadata,
NAV corruption, absence of artificial trades from rendered email, and the
no-send reservation/deduplication path. The synthetic rehearsal renders the
rounded HOLD both as a changes email and a complete book, with no D quotes.

Verified with the production Python interpreter: 2,377 tests passed, three
skipped, with 127 existing datetime deprecation warnings. The focused rounding
suite passed all 14 cases. The six-preview rehearsal used no SMTP or production
delivery ledger. Static checks found no failures; their dark-theme detector
warning was checked against actual rendering in both themes.

All 48 browser checks passed (six surfaces, two themes, four widths). At 390,
844, 768 and 1280 px respectively, measured horizontal overflow was zero and
minimum text size was 16 px throughout. Measured characters per line were 49.5,
73.8, 73.8 and 73.8. The rounded HOLD email and complete book were visually
inspected; the three D lines retained zero deltas and the caveats were visible.

Deployment is isolated from the development branch and unrelated holdings work.
Use the existing scheduled task with normal arguments, after checking that it
is idle and the automation clone is clean. Do not start a second refresh.
The normal release push is the email trigger; never dispatch a replacement
sender while an existing run or an unconfirmed reservation is outstanding.

Verify the core release commit, hosted sender outcome and confirmed delivery
receipt separately. A passing test suite, successful push or completed task
alone is not evidence that an email was accepted.

Rollback: after the active refresh finishes, revert this isolated code commit.
Preserve any confirmed delivery ledger and never replay an uncertain send.
The untracked Word review, research worktrees and unpublished holdings commit
are outside scope.
