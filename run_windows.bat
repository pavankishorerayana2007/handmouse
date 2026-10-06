@echo off
cd /d "%~dp0"
set PYCMD=
py -3.12 --version >nul 2>&1 && set PYCMD=py -3.12
if "%PYCMD%"=="" python --version >nul 2>&1 && set PYCMD=python
if "%PYCMD%"=="" goto installpy
if not exist venv %PYCMD% -m venv venv
call venv\Scripts\activate
if not exist venv\.ok (
  echo Installing AirControl AI - first time only, please wait...
  pip install -r requirements.txt
  if errorlevel 1 goto failed
  echo ok> venv\.ok
)
python aircontrol_agent.py
pause
exit /b

:installpy
echo Python is not installed. Installing Python 3.12 now...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Done. Close this window and double-click run_windows.bat again.
pause
exit /b

:failed
echo.
echo Install failed. If the error says "No matching distribution", install Python 3.12
echo from python.org, delete the venv folder, and run this again.
pause
exit /b
