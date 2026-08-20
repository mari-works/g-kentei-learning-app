@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo G-Kentei Learning App - Initial Setup
echo ==========================================
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON_CMD=py"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (
        set "PYTHON_CMD=python"
    ) else (
        echo ERROR: Python was not found.
        echo Install Python and enable "Add python.exe to PATH".
        echo.
        pause
        exit /b 1
    )
)

if not exist "requirements.txt" (
    echo ERROR: requirements.txt was not found.
    echo Put setup.bat in the same folder as app.py and requirements.txt.
    echo.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Existing virtual environment found.
)

echo.
echo Upgrading pip...
"venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip.
    pause
    exit /b 1
)

echo.
echo Installing required packages...
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install packages.
    echo Check requirements.txt and your internet connection.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo Setup completed successfully.
echo Run start.bat next.
echo ==========================================
echo.
pause
