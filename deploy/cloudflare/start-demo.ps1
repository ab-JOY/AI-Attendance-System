<#
Start the whole system on a temporary public HTTPS link - no domain needed.

    powershell -ExecutionPolicy Bypass -File deploy\cloudflare\start-demo.ps1

Starts serve.py, Caddy and a Cloudflare quick tunnel as hidden background
processes, so they keep running when this window or VS Code closes and leave
no window to close by accident, then prints the https://....trycloudflare.com
link and copies it. What they print goes to logs\ (see below).

The link is different every time this runs. A quick tunnel is Cloudflare's
offer for testing and demos: no account, no domain, no uptime guarantee. For
classes that run for months, use a named tunnel on your own domain
(docs/deployment.md, step 4).

Stop everything with: deploy\cloudflare\stop-demo.ps1
#>

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$caddy = 'C:\caddy\caddy.exe'
$caddyfile = 'C:\caddy\Caddyfile'
$cloudflared = 'C:\caddy\cloudflared.exe'
$logs = Join-Path $root 'logs'
$tunnelLog = Join-Path $logs 'tunnel.log'
# Hidden processes have no window to show their output, so it goes here.
$appLog = Join-Path $logs 'serve-console.log'
$caddyLog = Join-Path $logs 'caddy.log'

function Fail($message) {
    Write-Host ""
    Write-Host "CANNOT START: $message" -ForegroundColor Red
    exit 1
}

function Listening($port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

foreach ($file in $python, $caddy, $caddyfile, $cloudflared) {
    if (-not (Test-Path $file)) { Fail "$file is missing. See docs/deployment.md." }
}

if (-not (Listening 3306)) {
    Fail "MySQL is not running. Start it in the XAMPP Control Panel, then run this again."
}

# Anything left from an earlier run is stopped first, so this always starts
# one fresh copy - two would fight over the camera.
& (Join-Path $PSScriptRoot 'stop-demo.ps1') -Quiet

New-Item -ItemType Directory -Force -Path $logs | Out-Null

Write-Host "Starting the attendance system..."
Start-Process -FilePath $python -ArgumentList 'serve.py' -WorkingDirectory $root `
    -RedirectStandardError $appLog -RedirectStandardOutput "$appLog.out" -WindowStyle Hidden

for ($i = 0; $i -lt 60 -and -not (Listening 8000); $i++) { Start-Sleep -Seconds 1 }
if (-not (Listening 8000)) {
    Fail "the app did not start. See logs\app.log (and $appLog), or run: .venv\Scripts\python serve.py"
}

Write-Host "Starting Caddy..."
Start-Process -FilePath $caddy -ArgumentList 'run', '--config', $caddyfile, '--adapter', 'caddyfile' `
    -RedirectStandardError $caddyLog -RedirectStandardOutput "$caddyLog.out" -WindowStyle Hidden

for ($i = 0; $i -lt 20 -and -not (Listening 8080); $i++) { Start-Sleep -Seconds 1 }
if (-not (Listening 8080)) { Fail "Caddy did not start. See $caddyLog" }

Write-Host "Opening the Cloudflare tunnel..."
if (Test-Path $tunnelLog) { Remove-Item $tunnelLog -Force }

# cloudflared prints the link on stderr; it is read back from this log.
Start-Process -FilePath $cloudflared `
    -ArgumentList 'tunnel', '--no-autoupdate', '--url', 'http://127.0.0.1:8080' `
    -RedirectStandardError $tunnelLog -WindowStyle Hidden

$url = $null
for ($i = 0; $i -lt 60 -and -not $url; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $tunnelLog) {
        $text = Get-Content $tunnelLog -Raw -ErrorAction SilentlyContinue
        if ($text -match 'Registered tunnel connection' -and $text -match '(https://[a-z0-9-]+\.trycloudflare\.com)') {
            $url = $Matches[1]
        }
    }
}

if (-not $url) { Fail "the tunnel did not open. Check the internet connection, and $tunnelLog" }

try { Set-Clipboard -Value $url } catch { }

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " The system is online at:" -ForegroundColor Green
Write-Host ""
Write-Host "   $url" -ForegroundColor Yellow
Write-Host ""
Write-Host " (copied to the clipboard - it can take ~30 s to answer at first)" -ForegroundColor Green
Write-Host " Mobile app: Settings -> server URL -> paste this link." -ForegroundColor Green
Write-Host " Runs hidden in the background. Logs: $logs" -ForegroundColor Green
Write-Host " Stop everything: deploy\cloudflare\stop-demo.ps1" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
