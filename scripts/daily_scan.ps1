# GovAward Alpha — daily scan wrapper for Windows Task Scheduler.
#
# Runs scan-sam (lookback=1) and scan-usaspending (lookback=2) once a day so
# the dashboard always has fresh contract data. Designed to be idempotent:
# safe to run multiple times per day, never re-alerts on awards already seen.
#
# Logs are appended to logs/scan-YYYY-MM-DD.log. Exit code is 0 if both
# scans completed (even if they returned 0 rows); non-zero only if Python
# itself failed to launch.

$ErrorActionPreference = "Continue"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$today  = Get-Date -Format "yyyy-MM-dd"
$logFile = Join-Path $logDir "scan-$today.log"

# Resolve the venv Python; fall back to PATH if venv missing (e.g., user moved repo)
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# Critical: clear any empty/invalid ANTHROPIC_API_KEY inherited from the shell
# so pydantic-settings reads the value from .env. (We also fixed this in
# config.py via _NonEmptyEnvSettingsSource — this is belt-and-suspenders.)
if ($env:ANTHROPIC_API_KEY -eq "" -or $null -eq $env:ANTHROPIC_API_KEY) {
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
}

function Run-Scan($name, $argList) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value ""
    Add-Content -Path $logFile -Value "==================== $stamp  $name ===================="
    & $python -m govaward_alpha.cli @argList 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Null
    return $LASTEXITCODE
}

$exit1 = Run-Scan "scan-sam"         @("scan-sam",         "--lookback", "1")
$exit2 = Run-Scan "scan-usaspending" @("scan-usaspending", "--lookback", "2")

# Backfill Claude analysis on any new alerts that fired (also picks up
# any prior alerts that were never enriched). Cap at 25 per run so a single
# day can't blow up the Anthropic bill if something goes weird.
$exit3 = Run-Scan "enrich-alerts"    @("enrich-alerts", "--limit", "25", "--sleep", "0.3")

# Daily report — written to logs/reports/YYYY-MM-DD.md and emailed if
# EMAIL_ENABLED=true. --no-show suppresses the stdout dump (the log file
# already captures everything from the scan steps above).
$exit4 = Run-Scan "daily-report"     @("daily-report", "--hours", "24", "--fresh-days", "7", "--no-show")

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value ""
Add-Content -Path $logFile -Value "==================== $stamp  done (sam=$exit1 usaspending=$exit2 enrich=$exit3 report=$exit4) ===================="

# Surface only a hard launch failure to the scheduler. Scans/enrich/report
# each log their own errors and exit 0 on transient API failures.
if ($exit1 -ne 0 -and $exit2 -ne 0 -and $exit4 -ne 0) { exit 1 }
exit 0
