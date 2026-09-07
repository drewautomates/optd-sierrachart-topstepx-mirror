@echo off
REM ---------------------------------------------------------------------
REM  OPTD Sierra Chart -> TopstepX manual mirror.
REM
REM  Double-click this file to start the bridge. It installs the two
REM  Python packages on first run, checks the whole setup, and refuses
REM  to start if anything is wrong.
REM
REM  Sierra Chart side is separate: build the study and set its inputs
REM  once, per the README.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0bridge"

set "PY=python"
python --version >nul 2>nul || set "PY=py -3"
%PY% --version >nul 2>nul || goto :nopython

REM First run: install pyyaml + requests so nobody has to find pip.
%PY% -c "import yaml, requests" >nul 2>nul
if errorlevel 1 (
    echo First run - installing the two required Python packages...
    echo.
    %PY% -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto :badpip
    echo.
)

echo Checking setup...
%PY% manual_bridge.py --doctor
if errorlevel 1 goto :badsetup

echo Starting the bridge. Leave this window open while you trade.
echo Press Ctrl+C, or close the window, to stop it.
echo.
%PY% manual_bridge.py
goto :done

:nopython
echo.
echo ERROR: Python was not found.
echo   Install Python 3.9 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during the install.
goto :done

:badpip
echo.
echo ERROR: could not install the required packages.
echo   Try running this by hand, from this folder:
echo     %PY% -m pip install -r requirements.txt
goto :done

:badsetup
echo.
echo Setup check FAILED - see the FAIL lines above.
echo The bridge was NOT started, so nothing was sent to TopstepX.

:done
echo.
pause
