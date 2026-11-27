@echo off
rem RCI collector launcher (use this file in Windows Task Scheduler)
cd /d "%~dp0"
set PYTHONUTF8=1
rem 1) portable Python unzipped into the "python" folder (no install needed)
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" collect.py %*
  goto :done
)
rem 2) installed Python
where python >nul 2>nul
if %errorlevel%==0 (
  python collect.py %*
  goto :done
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 collect.py %*
  goto :done
)
echo [ERROR] Python not found. Unzip the Python embeddable package into the "python" folder. See README.
exit /b 9
:done
set RC=%errorlevel%
if %RC%==0 goto :eof
if %RC%==1 echo [WARN] Some sources failed. See logs folder and the source status table in the inbox tab.
if %RC%==2 echo [ERROR] Could not read the page. See logs folder.
exit /b %RC%
