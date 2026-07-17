@echo off
rem Stage-1 parallel-run wrapper (approved 2026-07-17) for the Norgate
rem breadth publisher - see reviews/2026-07-17_norgate-feed-migration.md.
rem Writes DERIVED states preview + divergence check via
rem scripts\publish_norgate_breadth.py; all output stays in git-ignored
rem data_local\ (licence guard). Scheduled Tue-Sat 07:15 SGT as
rem "breadth-thrust norgate feed parallel-run".
cd /d C:\dev\breadth-thrust-etf
if not exist data_local mkdir data_local
echo ---- %date% %time% ---- >> data_local\publisher.log
python scripts\publish_norgate_breadth.py >> data_local\publisher.log 2>&1
