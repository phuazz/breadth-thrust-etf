# Register BreadthThrust-CombinedBookRisk: the weekly combined-book risk rebuild
# (scripts/scheduled_cbr_refresh.py). Run by hand, once, after this script and
# scripts/scheduled_cbr_refresh.py are on origin/main.
#
# Dedicated clone, as for the holdings monitor (2026-11-03): the job writes
# data/combined_book_risk*.json in place for the length of a run, and the
# weekend and post-fill refreshes refuse a dirty tree in the sched clone, so
# the two must never share a working tree.
#
# Triggers: Saturday, Sunday and Monday at 10:00 SGT, repeating hourly for 23
# hours, i.e. hourly from Saturday 10:00 to Tuesday 09:00. The job reads the
# reconciliation window from the data; a firing outside it is a fast SKIP.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_cbr_refresh_task.ps1           # soak
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_cbr_refresh_task.ps1 -Arm      # armed (--push)
param([switch]$Arm)

$ErrorActionPreference = 'Stop'
$Clone = 'C:\dev\breadth-thrust-etf-cbr'
$Python = 'C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$TaskName = 'BreadthThrust-CombinedBookRisk'

if (-not (Test-Path $Clone)) {
    git clone https://github.com/phuazz/breadth-thrust-etf.git $Clone
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}
if (-not (Test-Path (Join-Path $Clone 'scripts\scheduled_cbr_refresh.py'))) {
    throw "scheduled_cbr_refresh.py is not in $Clone; merge it to main and pull first"
}

$scriptArgs = 'scripts\scheduled_cbr_refresh.py'
if ($Arm) { $scriptArgs += ' --push' }
$action = New-ScheduledTaskAction -Execute $Python -Argument $scriptArgs -WorkingDirectory $Clone

$triggers = @()
foreach ($day in 'Saturday', 'Sunday', 'Monday') {
    $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $day -At 10:00
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At 10:00 -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 23)).Repetition
    $triggers += $t
}

# Priority 4, as the refresh tasks since 2026-11-15 (the default 7 starved pytest).
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew -StartWhenAvailable -Priority 4

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Force `
    -Description 'Weekly combined-book risk snapshot rebuild inside the reconciliation window (scheduled_cbr_refresh.py). Fail closed; heartbeat logs\cbr_refresh_last_run.json.' | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
"registered {0}: {1} {2} (wd {3})" -f $TaskName, $task.Actions[0].Execute, $task.Actions[0].Arguments, $task.Actions[0].WorkingDirectory
$task.Triggers | ForEach-Object { "  {0} days={1} every {2} for {3}" -f $_.StartBoundary, $_.DaysOfWeek, $_.Repetition.Interval, $_.Repetition.Duration }
