@echo off
rem WS6b shadow publisher - weekly runner for the scheduled task
rem "BreadthThrust-WS6bShadow". Registration:
rem C:\dev\KICKOFF_ws6b-unscreened-replication.md (BINDING, items 1-4 signed
rem 2026-07-19); arming plan: reviews\2026-08-05_ws6b_pre-shadow-review.md SS7.
rem Armed 2026-09-09 on ZH's instruction, together with the SS6 rulings.
rem
rem SATURDAY 17:30 SGT, NOT THE 08:30 THE REVIEW PACK STAGED. SS7 was written
rem against a 06:00 SGT weekend cache refresh that no longer exists: since the
rem WS18 cadence change the weekend refresh is BreadthThrust-WeeklyRefresh at
rem 09:00 SGT on Saturday AND Sunday, repeating hourly for six hours, so an
rem 08:30 shadow would run BEFORE its own inputs and capture-integrity would
rem refuse the week. Chaining off that task's completion was considered and
rem rejected on two counts: the Task Scheduler operational log is disabled on
rem this machine, so an event-102 trigger would never fire at all; and adding a
rem second action to the refresh task would fire the shadow on every hourly
rem retry, including the early-exit ones, and would mean editing a task whose
rem settings object has already been clobbered once by exactly that kind of
rem edit. 17:30 clears the whole refresh window instead: the last hourly firing
rem starts at 15:00 and the longest observed run is 92 minutes (2026-09-09,
rem logs\last_green_run.json), so 17:30 sits past even a worst-case last retry.
rem It is also 09:30 UTC, about thirteen and a half hours after Friday's NYSE
rem close, which clears the US settle-hour lag measured on 2026-09-09 - the
rem foreign-domiciled names in a US roster (LIN, CRH, SW, AMCR) were unserved
rem at 01:24 UTC and all carried by 06:42 UTC. Xetra is shut on a Saturday, so
rem the "never refresh after 15:00 SGT" partial-bar rule does not bind here.
rem
rem MAIN TREE, DELIBERATELY. The WS6b member-price caches, the T1 evidence and
rem the shadow log all live under data_local\ws6b\ in this tree, and rebuilding
rem them in a fourth clone would split the register's evidence across two
rem trees. The reason run_norgate_publisher.bat needed its own clone does not
rem apply: this job commits and pushes NOTHING - it writes only to gitignored
rem data_local\ws6b\ - so it cannot collide with a human's staged work.
rem
rem --ff-only, AND NON-FATAL. --rebase refuses while any file in the tree is
rem unstaged, and the holdings-monitor soak leaves data\holdings_monitor_latest
rem .json plus two docs\ files modified every day BY DESIGN; SS7's staged
rem `git pull --rebase && python ...` would therefore have failed the chain
rem before the publisher ever ran, every single week. --ff-only cannot lose
rem work and only refuses when an incoming commit touches a locally-modified
rem file. A failure here is deliberately non-fatal: the publisher still runs,
rem and if the inputs are stale its own capture-integrity guard refuses the
rem week, which is the honest outcome rather than a silent stale publish.
rem
rem BTE_PRICE_SOURCE=norgate matches the deployed default adopted 2026-09-03
rem (WS19c). Left unset, build_panels would rebuild this tree's engine price
rem caches on a yfinance basis behind a run whose deployed siblings are all
rem Norgate-built. This is cache hygiene, not a shadow construction choice -
rem the register already resolves member prices through the WS6 A1/A2 layer.
cd /d C:\dev\breadth-thrust-etf
if not exist data_local\ws6b mkdir data_local\ws6b
echo ---- %date% %time% ---- >> data_local\ws6b\shadow_task.log
git pull --ff-only origin main >> data_local\ws6b\shadow_task.log 2>&1
rem -u because this log is the ONLY record of an unattended run. Redirected
rem stdout is block-buffered, so without it the log sits empty for the length of
rem the throttled weight-fetch phase and a stalled run looks identical to a
rem working one - observed on the 2026-09-09 first-run-clean fire, which showed
rem nothing for twenty minutes while it was in fact progressing normally.
set BTE_PRICE_SOURCE=norgate
C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe -u scripts\run_ws6b_shadow.py >> data_local\ws6b\shadow_task.log 2>&1
