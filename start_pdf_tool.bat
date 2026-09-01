@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: preparing the PDF tool...
    py -3 -m venv .venv
    if errorlevel 1 goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -c "import pymupdf, PySide6" >nul 2>nul
if errorlevel 1 (
    echo Installing the PDF compression engine...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
)

start "" ".venv\Scripts\pythonw.exe" qt_app.py
exit /b 0

:error
echo.
echo Setup failed. Please check Python and the network, then try again.
pause
exit /b 1
