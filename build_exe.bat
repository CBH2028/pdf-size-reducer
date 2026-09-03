@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto :error

call "native_worker\build.bat"
if errorlevel 1 goto :error

".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed ^
    --add-binary "native_worker\bin\pdf_fast_worker.exe;native_worker" ^
    --name "PDF_Size_Reducer" qt_app.py
if errorlevel 1 goto :error

if not exist "dist\PDF_Fast_Worker" mkdir "dist\PDF_Fast_Worker"
copy /Y "native_worker\bin\pdf_fast_worker.exe" "dist\PDF_Fast_Worker\" >nul
copy /Y "native_worker\bin\mupdfcpp64.dll" "dist\PDF_Fast_Worker\" >nul
copy /Y "native_worker\README.md" "dist\PDF_Fast_Worker\" >nul
powershell.exe -NoProfile -Command "Compress-Archive -Path 'dist\PDF_Fast_Worker\*' -DestinationPath 'dist\PDF_Fast_Worker_Windows_x64.zip' -Force"
if errorlevel 1 goto :error

echo.
echo Build complete: dist\PDF_Size_Reducer.exe and dist\PDF_Fast_Worker_Windows_x64.zip
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
