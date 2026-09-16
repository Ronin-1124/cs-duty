@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m cs_duty %*
) else (
    python -m cs_duty %*
)
exit /b %errorlevel%
