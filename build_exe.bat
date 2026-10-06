@echo off
cd /d "%~dp0"
if not exist venv\.ok (
  echo Run run_windows.bat once first, then run this.
  pause
  exit /b
)
call venv\Scripts\activate
pip install pyinstaller
pyinstaller --onefile --name AirControlAI --collect-all mediapipe --add-data "aircontrol-ai.html;." aircontrol_agent.py
echo.
echo Done. Your program is dist\AirControlAI.exe - double-click it any time, no Python needed.
pause
