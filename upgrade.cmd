@echo off
REM One entry for upgrade.py; logic stays there. ASCII on purpose, see windows_setup.bat.
REM One command on purpose: upgrade.py may git-pull this file while cmd is still reading it.
where py >nul 2>&1 && (py -3 "%~dp0upgrade.py" %* & exit /b) || (python "%~dp0upgrade.py" %* & exit /b)
