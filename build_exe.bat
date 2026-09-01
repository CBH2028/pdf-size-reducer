@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto :error

".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed ^
    --name "PDF_Size_Reducer" qt_app.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\PDF_Size_Reducer.exe
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
