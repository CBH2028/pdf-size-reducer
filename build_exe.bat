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

rem The tracked spec owns the early bootloader splash and bundled native files.
rem Building qt_app.py directly would silently drop the pre-Python splash.
".venv\Scripts\pyinstaller.exe" --noconfirm --clean "PDF_Size_Reducer.spec"
if errorlevel 1 goto :error

if not exist "dist\PDF_Fast_Worker" mkdir "dist\PDF_Fast_Worker"
copy /Y "native_worker\bin\pdf_fast_worker.exe" "dist\PDF_Fast_Worker\" >nul
copy /Y "native_worker\bin\pdf_fast_worker_backend.exe" "dist\PDF_Fast_Worker\" >nul
copy /Y "native_worker\bin\mupdfcpp64.dll" "dist\PDF_Fast_Worker\" >nul
copy /Y "native_worker\README.md" "dist\PDF_Fast_Worker\" >nul
powershell.exe -NoProfile -Command "Compress-Archive -Path 'dist\PDF_Fast_Worker\*' -DestinationPath 'dist\PDF_Fast_Worker_Windows_x64.zip' -Force"
if errorlevel 1 goto :error
powershell.exe -NoProfile -Command "$files = Get-Item -LiteralPath 'dist\PDF_Size_Reducer.exe', 'dist\PDF_Fast_Worker_Windows_x64.zip'; $lines = $files | ForEach-Object { '{0}  {1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash, $_.Name }; Set-Content -LiteralPath 'dist\SHA256SUMS.txt' -Value $lines -Encoding ascii"
if errorlevel 1 goto :error

echo.
echo Build complete: dist\PDF_Size_Reducer.exe, dist\PDF_Fast_Worker_Windows_x64.zip, and dist\SHA256SUMS.txt
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
