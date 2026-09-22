@echo off
REM nanoDoc - double-click this file to start.
REM It needs Python 3.9 or newer, and nothing else.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found on this computer.
  echo.
  echo   Install it from https://www.python.org/downloads/ and tick
  echo   "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

python start.py %*
if errorlevel 1 pause
endlocal
