# ============================================================
#  Launch all three NL-to-SQL backends, each in its own window
#    Unified Data : http://localhost:8000   (serves the UI)
#    CM Elevate   : http://localhost:8100
#    Focus        : http://localhost:8200
#  Open http://localhost:8000 in your browser after they start.
#  Close a window (or Ctrl+C in it) to stop that backend.
#
#  Run from anywhere:  powershell -ExecutionPolicy Bypass -File run_all.ps1
# ============================================================

$Root = $PSScriptRoot
$Py   = Join-Path $Root "Unified-Data\.venv\Scripts\python.exe"

$apps = @(
    @{ Name = "Unified Data (8000)"; Dir = "Unified-Data"; Port = 8000 },
    @{ Name = "CM Elevate (8100)";   Dir = "CM-Elevate";   Port = 8100 },
    @{ Name = "Focus (8200)";        Dir = "Focus";        Port = 8200 }
)

# Free the ports first — kill any stale backend still holding them, so re-running
# this script never fails with "[Errno 10048] only one usage of each socket address".
foreach ($a in $apps) {
    Get-NetTCPConnection -LocalPort $a.Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 600

foreach ($a in $apps) {
    $dir = Join-Path $Root $a.Dir
    # Each backend runs in its own PowerShell window so logs stay separate.
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location '$dir'; & '$Py' -m uvicorn backend.main:app --port $($a.Port)"
    ) | Out-Null
    Write-Host "Started $($a.Name)"
}

Write-Host ""
Write-Host "All three backends are starting in separate windows."
Write-Host "Open http://localhost:8000 in your browser."
