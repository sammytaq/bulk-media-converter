@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "app.py" %*
    exit /b
)
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 app.py %*
) else (
    python app.py %*
)
if errorlevel 1 (
    echo.
    echo Install Python 3.11 or newer and run: python -m pip install -r requirements.txt
    echo Then launch this file again.
    pause
)
