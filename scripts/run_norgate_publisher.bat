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
rem --push does an explicit single-file `git add` (never -A; interactive
rem sessions share this working tree), then commit / pull --rebase / push,
rem failing soft at every step. On any git failure the states file is still
rem written locally and CI keeps consuming the scrape path until a later run
rem pushes successfully - the designed degradation ladder (review section 5).
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
cd /d C:\dev\breadth-thrust-etf
if not exist data_local mkdir data_local
echo ---- %date% %time% ---- >> data_local\publisher.log
python scripts\publish_norgate_breadth.py --commit-path --push >> data_local\publisher.log 2>&1
