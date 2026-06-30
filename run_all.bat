@echo off
REM ============================================================
REM  Launch all three NL-to-SQL backends, each in its own window
REM    Unified Data : http://localhost:8000   (serves the UI)
REM    CM Elevate   : http://localhost:8100
REM    Focus        : http://localhost:8200
REM  Open http://localhost:8000 in your browser after they start.
REM  Close a window (or Ctrl+C in it) to stop that backend.
REM ============================================================

set ROOT=%~dp0
set PY=%ROOT%Unified-Data\.venv\Scripts\python.exe

REM --- Free ports 8000/8100/8200 first so a leftover instance can't cause ---
REM --- "Errno 10048: only one usage of each socket address" on startup.   ---
echo Clearing any old backends on ports 8000, 8100, 8200 ...
for %%P in (8000 8100 8200) do (
  for /f "tokens=5" %%K in ('netstat -ano ^| findstr /R /C:":%%P .*LISTENING"') do (
    echo   freeing port %%P (PID %%K)
    taskkill /F /PID %%K >nul 2>&1
  )
)
echo.

start "Unified Data (8000)" cmd /k "cd /d "%ROOT%Unified-Data" && "%PY%" -m uvicorn backend.main:app --port 8000"
start "CM Elevate (8100)"   cmd /k "cd /d "%ROOT%CM-Elevate"   && "%PY%" -m uvicorn backend.main:app --port 8100"
start "Focus (8200)"        cmd /k "cd /d "%ROOT%Focus"        && "%PY%" -m uvicorn backend.main:app --port 8200"

echo.
echo All three backends are starting in separate windows.
echo Open http://localhost:8000 in your browser.
echo.
