@echo off

call venv\Scripts\activate.bat

echo ==========================================
echo Starting G-Kentei Learning App...
echo ==========================================
echo.

start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"

flask --app app.py run

pause