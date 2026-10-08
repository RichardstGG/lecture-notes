@echo off
REM One entry for a fresh Windows checkout. Logic stays in setup.py.
REM This file is ASCII on purpose: cmd.exe prints it with the console code page.
setlocal EnableExtensions
cd /d "%~dp0"

call :find_python
if defined PY goto have_python

echo Python 3.11+ was not found.
where winget >nul 2>&1
if errorlevel 1 (
  echo winget is not available. Install Python 3.11 or newer from
  echo https://www.python.org/downloads/ and include python.exe on PATH.
  echo Then run windows_setup.bat again.
  echo.
  pause
  exit /b 1
)
echo Installing Python 3.13 with winget. This can take a few minutes.
winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
call :find_python
if defined PY goto have_python
echo.
echo Python still is not visible in this window.
echo Close this window and run windows_setup.bat again.
echo.
pause
exit /b 1

:have_python
echo.
echo Installing anything still missing: ffmpeg, Git, Node.js, the VC++ runtime,
echo speech engines, the summary model, and the Web UI.
echo If an NVIDIA GPU is present, setup.py asks whether to use CUDA.
echo.
"%PY%" setup.py --provision %*
set RC=%errorlevel%
if not "%RC%"=="0" (
  echo.
  echo setup.py failed ^(exit %RC%^). See the messages above.
)
echo.
pause
exit /b %RC%

:find_python
set "PY="
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable if sys.version_info>=(3,11) else '')" 2^>nul') do (
    call :accept_python "%%I"
  )
)
if defined PY exit /b 0
where python >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('python -c "import sys; print(sys.executable if sys.version_info>=(3,11) else '')" 2^>nul') do (
    call :accept_python "%%I"
  )
)
if defined PY exit /b 0
for %%V in (313 312 311) do (
  if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" (
    set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
    exit /b 0
  )
  if exist "%ProgramFiles%\Python%%V\python.exe" (
    set "PY=%ProgramFiles%\Python%%V\python.exe"
    exit /b 0
  )
)
exit /b 1

:accept_python
set "CANDIDATE=%~1"
if not defined CANDIDATE exit /b 0
echo %CANDIDATE% | find /i "WindowsApps" >nul
if not errorlevel 1 exit /b 0
if exist "%CANDIDATE%" set "PY=%CANDIDATE%"
exit /b 0
