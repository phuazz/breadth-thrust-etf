# Claude result: section 02 rebuilt around the portfolio, and one labelled revision

## Working directory, seat and objective

Working directory: `C:\dev\breadth-thrust-etf\.component-mail\factsheet-presentation`
(worktree of `C:\dev\breadth-thrust-etf`, branch `codex/factsheet-presentation`).
Model and effort: Claude Opus, high.

Objective, from `handoffs/claude-factsheet-redesign-and-send.md`: redesign the
information architecture of "02 / Proposed changes and their signals" around the
portfolio rather than the fund list, preserve every refresh repair and data
safeguard already in production, verify, deploy, and send one clearly labelled
revised-presentation email to all existing recipients without disturbing the
already-delivered instruction.

Status: **COMPLETE — see "Actual send evidence" below.**

Codex commit `f3e666c` was used as a starting point, not a constraint. Its
performance panels, return drivers, holding-price moves, deterministic PDF,
complete HTML book, substantive plain text, sender display name, source-hash
verification and deliberate `risk_overlay.current_breadth` omission are all
retained unchanged. Section 02, its supporting view model, and the revision
path are new.

## The problem and the chosen information design

The old section 02 rendered a large heading and a paragraph for each of the 18
changes, over two PDF pages. It answered "what happened to each line" eighteen
times and never answered "what happened to the portfolio". The fix is an order
change, not a wording change: the portfolio answer comes first, and the fund
list becomes the evidence behind it rather than the argument itself.

Archetype: **decision aid** in the email, **reference dossier** in the PDF. The
email is a short read that a reader can act on; the PDF carries the per-row
evidence and the complete book.

Section 02 now reads, in this order, on both surfaces:

1. **A computed portfolio lead.** Number of changes, one-way turnover, entries
   and exits, then what the strategy budgets did. For 2026-09-11: 18 changes,
   7.78% one-way turnover, 1 new, 1 closed, and the budgets do not move — every
   change is a rotation *inside* a strategy. That sentence is derived from the
   book, not asserted: `budgets_held()` compares each sleeve's net NAV shift
   against the existing model rounding bound (1e-4 NAV), and a de-risk week
   prints "Strategy budgets change this week" with the moved budgets instead.
   The PDF adds a held / target / net-shift / change-count table per strategy.
2. **The largest moves at a glance.** Labelled rows in the house grammar: the
   three largest increases, the three largest reductions, every entry, every
   exit, and what is unchanged (thematic, the EM tilt, the gate state, and
   whether there is a defensive allocation). Entries and exits are named as
   such rather than left to be inferred from a sign, and are described as
   ranking-tail only when *every* one of them is below the existing 0.5% NAV
   house threshold (`MATERIAL_NAV`, matching `build_email_body`).
3. **One compact held-to-target table, grouped by strategy.** Columns:
   position (traded ticker, an ENTER/EXIT tag where it applies, and the fund
   name), held, target, change. Groups are ordered by strategy identity, not by
   move size, so a small Europe resize cannot be pushed off the page by a large
   core move — this replaces the old top-six-plus-separate-D-block mechanism
   while keeping the D confirmation block as well. Each group header states
   that strategy's own driver **once**: the signal it re-ranked on, its largest
   addition and its largest reduction, each with the stored before/after level
   and rank. The PDF keeps a per-row signal column so every line has its own
   evidence without a paragraph of its own.
4. **Units, per the house rule.** Relative breadth in percentage points;
   constituent breadth unsigned in percentages; price distance from the 200-day
   average signed in percentages. The unit is written once, on the figure the
   reader ends on ("+1.88 to +11.02pp · rank 5 to 4 of 14").

The email now lists **every** change rather than the largest six. A disclosed
cap (`EMAIL_CHANGE_LIMIT = 20`) keeps a pathological week from turning the email
into a book, and prints "N of M changes shown" when it bites; otherwise it
prints "All N proposed changes are listed above; none is omitted."

## What is deliberately NOT claimed

- **No weighting mechanism is invented.** `sizing_scheme()` names the weighting
  only when this book's own weights reconstruct from its own signal rows within
  the precision those rows are stored at (four decimals), or when the members
  are exactly equal-weighted. Anything else returns `None` and the surfaces say
  nothing. `sizing_note()` prints it once, and only when every ranked strategy
  shown agrees; a mixed week is silent. For 2026-09-11 the check passes for A,
  B and D, so the note appears once and explains why a weight can fall without
  a rank changing.
- **No market commentary.** Every "why" is a stored signal level, a stored rank,
  or an overlay state. Nothing attributes a move to a market event.
- **Only a D follow-up claims an earlier email.** `email_decision` authorises a
  `d_update` only while the core identity matches what was distributed, so the
  A–C and overlay groups can be labelled "already sent in the initial email"
  and the D block can say "do not submit them a second time". A single all-ready
  factsheet no longer prints the old conditional "If you received the initial
  email…", because in that stage no earlier email exists.
- **`risk_overlay.current_breadth` stays omitted.** The initial and consolidated
  archives carry 0.4771 and 0.3912 for the same `panel_end_date`. Its producer
  cause is still untraced; a date label is not proof the scalar is current. The
  verified gate state and the recorded rule thresholds remain.
- **Provisional / held / executed distinctions survive.** The initial stage still
  marks whole-book performance provisional and prints "Unavailable" for the D
  sleeve's return rather than filling it.

## The presentation revision path

The ordinary sender correctly refuses the completed anchor ("already distributed
or update window closed"), and nothing here weakens that. A narrowly scoped,
separately reserved and separately logged path was added instead.

`plan_revision()` refuses by default and requires all of: a valid identifier
(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); no operator hold; no outstanding pending
attempt anywhere in the ledger except this revision's own; a state for the
current anchor whose ordinary weekly delivery **completed**; no outstanding D
follow-up; no prior revision for that anchor; a sealed release that verifies and
belongs to the current anchor; `d_ready`; core **and** Europe identities equal to
what was delivered; the weekend review checkpoint still ahead; and no proposed
fill date in the past.

`prepare_revision()` writes the candidate and takes the **same durable
reservation** the ordinary stages use, so an interrupted revision blocks every
later send until an operator reconciles it. `send_revision()` re-runs the entire
eligibility check at the actual send clock, re-verifies the release against the
candidate, requires the reservation to be present **on the remote tracking
branch** before SMTP, and only then calls the existing `smtp_send` (which raises
if any recipient is refused). On success it pops the reservation and records the
revision **beside** the original receipt; `core`, `europe`, `preview`, `regular`,
`last_confirmed_at` and `docs/factsheet_published.json` are untouched.

`REVISION_ACTION` is not in `SEND_ACTIONS`, `email_decision` can never return it,
and `send()` now refuses a payload whose decision is not an ordinary stage.
`.github/workflows/factsheet_revision.yml` is `workflow_dispatch` only, defaults
to a dry run, shares the `weekly-factsheet` concurrency group so it can never
overlap the scheduled sender, and reserves-and-pushes before contacting Gmail.

Retry safety is tested both ways: after a confirmed revision the send refuses
("revision eligibility changed after reservation: a presentation revision was
already delivered"), a second identifier is refused, and a changed core or
Europe identity is refused.

## Commits and changed files

Branch `codex/factsheet-presentation`, rebased onto production and fast-forwarded
to `main`.

- `PLACEHOLDER_COMMITS`

Changed:

- `scripts/component_factsheet_view.py` — section 02 rebuilt; signal/units
  helpers; `sleeve_shifts`, `budgets_held`, `budget_sentence`, `action_of`,
  `highlight_rows`, `sleeve_story`, `sizing_scheme`, `sizing_note`,
  `group_heading`, `unchanged_sentence`, `_change_table`; grouped complete-book
  table; revision banner; plain-text extractor now keeps a table row on one line.
- `scripts/component_publication.py` — a `revision` branch in `email_wording`.
  `email_decision` is untouched.
- `scripts/send_component_factsheet.py` — `plan_revision`, `prepare_revision`,
  `send_revision`, the `revise` / `revise-send` operations, and a guard on
  `send()` rejecting a non-ordinary payload.
- `tests/test_factsheet_presentation.py` — reworked to the new layout contract.
- `tests/test_factsheet_revision.py` — new, the revision guard set.
- `tests/preview_factsheet_presentation.py` — optional `--action` so the
  `d_update` and `revision` stages can be rehearsed no-send.
- `tests/check_component_email_mobile.cjs` — now measures **contrast** as well
  as overflow and font size (see "A defect found and fixed").
- `.github/workflows/factsheet_revision.yml` — new, manual only.
- `handoffs/claude-factsheet-redesign-result.md` — this file.

## A defect found and fixed during verification

The ENTER/EXIT tag colours are inline so they survive a mail client that strips
the stylesheet. That is correct — and it meant the same inline colours ignored
the dark-theme rule: `#1a6b34` and `#a3201a` fall to about 2.7:1 and 2.3:1 on
the dark ground, below WCAG AA. Overflow and font-size checks cannot see this.

Fixed with a dark-theme override (`#86efac` / `#fca5a5`, measured 12.6:1 and
9.3:1), and the render check now computes the contrast ratio of every element
carrying its own text against its nearest opaque ancestor background and fails
below 4.5:1 (3:1 for large text). Measured minimum after the fix: **6.57:1 in
light, 9.35:1 in dark, zero failures across 64 checks.** The word ENTER/EXIT
also remains, so colour is never the only signal.

## Tests, guards and measured results

- **Full repository suite, production interpreter** (`.venv`, Python 3.12.14),
  run against the final code: `PLACEHOLDER_FULLSUITE`.
- **Focused suite** (`test_factsheet_presentation.py`, `test_factsheet_revision.py`,
  `test_component_sender.py`, `test_hold_rounding.py`): 85 tests. 23 presentation
  tests lock the new layout contract (portfolio answer before the fund list, the
  budget sentence computed not asserted, entry/exit classification, every change
  listed once with its direction, the disclosed cap, units per signal kind, the
  weighting note only when the book confirms it, D keeping its own group and
  confirmation, the small-entry threshold, the plain-text row, the rounding
  residual never reading as a reallocation, and only a d_update claiming an
  earlier email). 22 revision tests cover the guard set above.
- **`MOBILE_CHECK.md`, half one** — `python C:/dev/scripts/check_page.py` on all
  eight surfaces: 0 fail, 8 ok each. The one WARN is the static dark-theme
  detector, disproved by the rendered check below (light background
  `rgb(255,255,255)`, dark `rgb(17,24,39)`).
- **`MOBILE_CHECK.md`, half two** — real emulated viewports, 8 surfaces × 4
  widths × 2 themes = **64 checks, all passing**. `document.documentElement.clientWidth`
  read back and equal to the requested width in every case; `scrollWidth` never
  exceeding it; **horizontal overflow 0**; **minimum font 13 px**; **characters
  per line 49.5 / 73.8 / 73.8 / 73.8 at 390 / 844 / 768 / 1280 px**; minimum
  contrast 6.57:1. Measurements in `.component-mail/mobile-measurements.json`.
- **Gmail worst case** — every surface re-rendered with the `<style>` block
  removed entirely and re-measured at the same four widths: 32 checks, zero
  overflow, 13 px minimum, all 8 surfaces keeping their full row counts. A table
  without its stylesheet is still a table; the information survives.
- **PDF** — 4 stages × 6 pages = 24 pages rendered to PNG and inspected (14
  distinct by hash; the rest are byte-identical across stages). Page breaks are
  clean, no cell text is clipped, and the Action column no longer wraps.
- **Figure reconciliation** — an independent script rebuilds each archived
  release from its sealed sources and checks the rendered HTML, plain text and
  PDF **against the book**, not against the renderer's view model: all 24 lines
  and their exact held / target / change figures in the complete book and PDF,
  the change count, turnover, entry and exit counts and sizes, all five
  performance values, the weekly window, gate and tilt state, the exact rounding
  residual, the rounding caveat appearing exactly when there is a residual, both
  venue fill dates with the weekday verified by a date library, the provisional
  wording matching D HOLD, and the omitted breadth scalar. **0 failures across
  all three stages.**
- **Read-only eligibility probe** against the real repository state before any
  send: `plan_revision` returned `revision` for anchor 2026-09-11 (identity
  `d821d62b6d295189`, `d_ready True`) with `committed` both False and True, while
  the ordinary `plan` returned `wait — already distributed or update window
  closed`. Duplicate protection intact and untouched.

## Dates, verified with a date library

All weekdays below were checked with `datetime`/`zoneinfo`, not from memory.

- Decision close / anchor: **Friday 11 September 2026**.
- Proposed fill: **Monday 14 September 2026**, NYSE and XETR closing auctions.
- Weekend review checkpoint: **Monday 14 September 2026, 06:00 SGT**.
- Revision sent: `PLACEHOLDER_SENDDATE`.

The venue dates were still ahead at send time; nothing stale was re-sent. This
is stated because the brief requires actual venue dates to be checked and
flagged rather than rewritten as current.

## Deployment

`PLACEHOLDER_DEPLOY`

## Actual send evidence

`PLACEHOLDER_SEND`

## Open decisions for the owner

1. **The email now lists every change, not six.** For a typical week that is one
   screen of compact table and strictly more useful than the old six cards. If
   you would rather it stayed shorter, the cap is one constant
   (`EMAIL_CHANGE_LIMIT`) and the disclosure path is already tested.
2. **The weighting note.** "Members are sized in proportion to the recorded
   signal levels, so a weight can fall without a rank changing" is emitted only
   when the book's own numbers confirm it. It is the one sentence in section 02
   that describes a mechanism rather than a figure. Say the word and it goes.
3. **`risk_overlay.current_breadth` remains untraced.** Still omitted. Tracing
   the producer is separate work and is not a presentation task.
4. **The stale-fill guard in `plan_revision` is unreachable in practice.**
   `verify()`'s own calendar check rejects a stale book before that branch runs,
   so the branch is defence in depth and is not covered by a test. I am flagging
   it rather than claiming coverage I do not have.
5. **SMTP acceptance is not inbox placement.** Browser and stripped-CSS checks
   do not establish Gmail or Outlook rendering. An owner-only mail-client look at
   the delivered message is still the only way to confirm that.

## Do not touch

- The unfinished merge in the development checkout `C:\dev\breadth-thrust-etf`
  (conflicted `docs/index.html` and `handoffs/execution-timing-dashboard-followup.md`)
  and the unrelated holdings-monitor work. Not resolved, reset, rebased or
  published. The untracked `reviews/2026-09-10_ws6-session-record.docx` is intact.
- The confirmed delivery receipts for anchor 2026-09-11. The revision is recorded
  beside them; none was cleared, edited or replayed.
- Recipients, schedules, calendars, strategy parameters and the two-stage weekly
  policy. All unchanged.
- `output/` and `tmp/` in this worktree — untracked review artefacts, not added.
- No blanket force-send switch exists and none was added.

## Next prompt and notes for Codex

Suggested next prompt:

> Independent guard review of the factsheet presentation revision path in
> `scripts/send_component_factsheet.py` and the section 02 view model in
> `scripts/component_factsheet_view.py`. Try to find a sequence that delivers a
> second email for a settled anchor, or that makes a surface state a figure or a
> mechanism the sealed book does not carry. Do not run a send.

Notes:

- `playwright` is not installed in this repository; the render check needs it.
  It was installed into the session scratchpad and run with `NODE_PATH` pointing
  there, so `node_modules/` never entered the worktree. `node_modules` is **not**
  in `.gitignore` — if you install it locally, do not commit it.
- `pypdfium2` was likewise installed to a scratchpad `--target` directory for PDF
  page rendering; the production `.venv` is unchanged and requirements.txt gains
  nothing. ReportLab was already there.
- The automation clone `C:\dev\breadth-thrust-etf-sched` was verified clean and
  idle (task `BreadthThrust-WeeklyRefresh` Ready, last run 2026-09-13 16:00 SGT
  result 0, next 2026-09-19) before being touched. See the deployment section.
- The contrast measurement added to `check_component_email_mobile.cjs` is worth
  keeping and worth copying: it is what caught a defect that eight surfaces of
  overflow and font-size checking could not see.
- No recipient address or credential appears in this handoff.
