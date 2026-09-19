@echo off
REM Vault launcher for Windows.  Uses the py launcher so it works regardless
REM of which Python is first on PATH.
setlocal
set "HERE=%~dp0"
py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" 2>nul
if errorlevel 1 (
  echo vault: needs Python 3.9 or newer. 1>&2
  echo   Install it from https://python.org/downloads or the Microsoft Store. 1>&2
  exit /b 127
)
set "PYTHONPATH=%HERE%src;%PYTHONPATH%"
py -3 -m vault %*
