@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo G-Kentei Learning App - Initial Setup
echo ==========================================
echo.

rem The required packages need Python 3.10 or newer.
py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.11"
) else (
    echo ERROR: Python 3.11 was not found.
    echo Install Python 3.11, then run this file again.
    echo.
    pause
    exit /b 1
)

if not exist "requirements.txt" (
    echo ERROR: requirements.txt was not found.
    echo Put setup.bat in the same folder as app.py and requirements.txt.
    echo.
    pause
    exit /b 1
)

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
    if errorlevel 1 (
        echo Existing virtual environment uses an unsupported Python version.
        echo Recreating it with Python 3.11...
        %PYTHON_CMD% -m venv --clear venv
        if errorlevel 1 (
            echo ERROR: Failed to recreate virtual environment.
            pause
            exit /b 1
        )
    ) else (
        echo Existing compatible virtual environment found.
    )
) else (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
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
