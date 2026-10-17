<#
Start the attendance system (serve.py) automatically when you log on to the
classroom machine. Run once, from the project folder, in a normal (not
administrator) PowerShell:

    powershell -ExecutionPolicy Bypass -File deploy\cloudflare\install-autostart.ps1

Remove it again with:

    Unregister-ScheduledTask -TaskName AttendanceSystem -Confirm:$false

Why a logon task and not a Windows service: the camera is opened by this
process, and a service runs in session 0 under a system account, outside the
camera privacy settings of the user who logs on. Whether a webcam opens from
there has not been tested on this machine. A task in the logged-on user's
session opens it exactly as `python app.py` does today.

Caddy and cloudflared have no camera, so they run as real services (see
docs/deployment.md, steps 3 and 4).
#>

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Run: bash scripts/setup.sh"
}

if (-not (Test-Path (Join-Path $root '.env'))) {
    throw "No .env in $root. See docs/deployment.md step 2."
}

$action = New-ScheduledTaskAction -Execute $python -Argument 'serve.py' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# serve.py exits 1 if MariaDB is not running. Do not count on the restart
# settings below to cover that - whether Task Scheduler treats a non-zero exit
# as a failure to restart is not verified here. Install XAMPP's MySQL as a
# Windows service instead (docs/deployment.md step 1), so it is up before logon.
# ExecutionTimeLimit zero = no limit; the default would stop it after 3 days.
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName 'AttendanceSystem' `
    -Description 'AI Attendance System - serve.py behind Caddy and Cloudflare Tunnel' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force | Out-Null

Write-Host "Registered. serve.py will start at your next logon."
Write-Host "Start it now with: Start-ScheduledTask -TaskName AttendanceSystem"
