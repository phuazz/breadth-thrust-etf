@echo off
rem Stage-2 publisher (activated 2026-08-09 on ZH's explicit approval) for
rem the Norgate breadth feed - see reviews/2026-07-17_norgate-feed-migration.md.
rem
rem Stage 1 (2026-07-17 to 2026-08-09) ran WITHOUT --commit-path: derived
rem states went to git-ignored data_local\ as a preview and the run only
rem logged a divergence check against the deployed scrape feed. The soak
rem closed CLEAN on 2026-08-07 (review section 10).
rem
rem Stage 2 writes DERIVED states to tracked data\gate_states_norgate.json
rem and pushes it, so run_risk_overlay.py consumes them instead of computing
rem states from the scrape series. Licence guard holds either way: the file
rem carries dates plus binary 0/1 states only, never the breadth values.
rem
rem --push does an explicit single-file `git add` (never -A), then commit /
rem pull --rebase / push, failing soft at every step. On any git failure the
rem states file is still written locally and CI keeps consuming the scrape
rem path until a later run pushes successfully - the designed degradation
rem ladder (review section 5).
rem
rem DEDICATED WORKING TREE (2026-08-25). This job runs in
rem C:\dev\breadth-thrust-etf-norgate, a third clone of the same origin, and
rem NOT in the main tree. Reason: `git pull --rebase` refuses while ANY file
rem in the tree is unstaged, and the "holdings monitor daily" task registered
rem 2026-08-19 leaves data\holdings_monitor_latest.json plus two docs\ files
rem modified every day BY DESIGN (soak mode builds the page but commits
rem nothing). That defeated the ladder's self-heal clause: the publish step
rem failed on 2026-08-21, 08-22 and 08-25, and the accumulated commits only
rem reached origin when an unrelated interactive push swept them out. That
rem rescue is incidental, and it disappears on an unattended machine.
rem
rem Do NOT repoint this at C:\dev\breadth-thrust-etf-sched either - that tree
rem belongs to the BreadthThrust-WeeklyRefresh task, and sharing it would
rem rebuild the same collision between two scheduled jobs instead of one
rem scheduled job and a human.
rem
rem The scheduled task still invokes THIS copy of the batch, in the main
rem tree, so the runner stays version-controlled in one canonical place.
rem
rem Rollback: drop the two flags below to return to Stage 1, and delete
rem data\gate_states_norgate.json. run_risk_overlay falls through to the
rem scrape path verbatim when the file is absent, stale beyond the 10-day
rem cap, or malformed.
rem
rem Scheduled Tue-Sat 07:15 SGT as "breadth-thrust norgate feed parallel-run".
rem Tue-Sat is CORRECT and deliberate: SGT is UTC+8, so the job runs 23:15
rem UTC the previous day and each fire captures the PRIOR session's US close
rem - Tue-Sat therefore covers exactly Mon-Fri US sessions. Do not "fix" it
rem to Mon-Fri; that drops Saturday and loses every Friday close.
cd /d C:\dev\breadth-thrust-etf-norgate
if not exist data_local mkdir data_local
echo ---- %date% %time% ---- >> data_local\publisher.log
rem Sync to origin tip BEFORE computing. The divergence check reads the
rem deployed state from data\risk_overlay.json, which CI rewrites daily, so
rem an unsynced tree would compare today's Norgate state against a stale
rem deployed one and report a meaningless "ok". --ff-only cannot lose work
rem in a tree nothing else writes to, and a failure here is deliberately
rem non-fatal: the publisher still runs and its own ladder governs.
git pull --ff-only origin main >> data_local\publisher.log 2>&1
python scripts\publish_norgate_breadth.py --commit-path --push >> data_local\publisher.log 2>&1
