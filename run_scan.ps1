# Hourly local run of the Upbit scanner.
#
# Why this exists: GitHub's scheduled workflows are best-effort and actually fired
# only 5-7 times a day (measured 2026-09-13..25), so signals arrived hours after
# their 4h bar closed. This runs the SAME scan.py from the SAME clone, so both
# paths share sent.json and neither re-alerts what the other already sent.
# GitHub's cron stays on as the fallback for whenever this PC is off.
#
# Secrets: read from .env next to this script. That file is gitignored and is
# written by the user -- the token never passes through a chat or a commit.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- load .env (KEY=VALUE per line, # comments allowed) ---
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
            $k, $v = $t.Split('=', 2)
            Set-Item -Path ("env:" + $k.Trim()) -Value $v.Trim()
        }
    }
} else {
    Write-Output "WARNING: .env not found - scan will run but cannot send Telegram."
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Output "=== local run $stamp KST ==="

# Pull first: the ledgers may have been advanced by a GitHub run since last time.
# Without this the push below conflicts and the dedup ledger forks.
git pull --rebase --quiet
if ($LASTEXITCODE -ne 0) { Write-Output "git pull failed - aborting so ledgers cannot fork"; exit 1 }

python scan.py
$scanExit = $LASTEXITCODE
Write-Output "scan.py exit=$scanExit"

# Persist the ledgers exactly like the workflow does, so a signal alerted here is
# not alerted again by the next GitHub run.
$dirty = git status --porcelain sent.json heartbeat.json ranking.json
if ($dirty) {
    git add sent.json heartbeat.json ranking.json
    git commit -m "chore: update alert ledger [skip ci]" --quiet
    git push --quiet
    if ($LASTEXITCODE -eq 0) { Write-Output "ledgers pushed" } else { Write-Output "ledger push FAILED" }
} else {
    Write-Output "ledgers unchanged"
}

exit $scanExit
