@echo off
REM ============================================
REM  FluoFigFactory - launcher
REM  Prefers a project-local virtualenv, falls
REM  back to the python on PATH.
REM ============================================
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON=venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo ============================================
echo   FluoFigFactory
echo   http://localhost:5000
echo   Press Ctrl+C to stop.
echo ============================================
echo.

"%PYTHON%" app.py

echo.
echo [FluoFigFactory] service stopped.
pause
