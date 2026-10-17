<#
Stop what start-demo.ps1 started: the app (port 8000), Caddy (port 8080)
and the Cloudflare quick tunnel. MySQL is left running.

    powershell -ExecutionPolicy Bypass -File deploy\cloudflare\stop-demo.ps1
#>

param([switch]$Quiet)

$stopped = @()

foreach ($port in 8000, 8080) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
            $stopped += "port $port"
        }
}

# Only the quick tunnel to Caddy - a named tunnel installed as a service is
# not this script's to stop.
Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
    Where-Object { $_.CommandLine -like '*127.0.0.1:8080*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped += 'tunnel'
    }

if (-not $Quiet) {
    if ($stopped) { Write-Host "Stopped: $($stopped -join ', ')" } else { Write-Host "Nothing was running." }
}
