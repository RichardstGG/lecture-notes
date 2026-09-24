@echo off
REM Entry point only. All logic lives in setup.py (one copy for all platforms).
REM Text is ASCII here on purpose: cmd.exe renders a .bat with the console code
REM page, so Chinese in this file would be garbled. setup.py prints UTF-8 itself.
cd /d "%~dp0"
python setup.py %*
set RC=%errorlevel%
if not "%RC%"=="0" (
  echo.
  echo setup.py failed ^(exit %RC%^). See the messages above.
)
echo.
pause
exit /b %RC%
