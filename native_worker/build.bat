@echo off
setlocal
cd /d "%~dp0"

set "VS_VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%VS_VCVARS%" (
    echo Visual Studio 2022 C++ Build Tools were not found.
    exit /b 2
)

set "MUPDF_ROOT=%CD%\..\.venv\Lib\site-packages\pymupdf\mupdf-devel"
set "MUPDF_DLL=%CD%\..\.venv\Lib\site-packages\pymupdf\mupdfcpp64.dll"
set "CARGO=%USERPROFILE%\.cargo\bin\cargo.exe"
if not exist "%MUPDF_ROOT%\include\mupdf\classes.h" (
    echo PyMuPDF development headers were not found. Install requirements-dev.txt first.
    exit /b 3
)
if not exist "%CARGO%" (
    echo Rust stable was not found. Install it with rustup before building.
    exit /b 7
)

call "%VS_VCVARS%" >nul
if errorlevel 1 exit /b 4

if not exist "bin" mkdir "bin"
cl.exe /nologo /std:c++17 /EHsc /O2 /GL /GS /sdl /guard:cf /DNDEBUG /MD ^
    /I"%MUPDF_ROOT%\include" pdf_fast_worker.cpp ^
    /link /LTCG /OPT:REF /OPT:ICF /DYNAMICBASE /HIGHENTROPYVA /NXCOMPAT /guard:cf ^
    /LIBPATH:"%MUPDF_ROOT%\lib" mupdfcpp64.lib libmuthreads.lib ^
    /OUT:"bin\pdf_fast_worker_backend.exe"
if errorlevel 1 exit /b 5

copy /Y "%MUPDF_DLL%" "bin\mupdfcpp64.dll" >nul
if errorlevel 1 exit /b 6

for /f %%H in ('powershell.exe -NoProfile -Command "(Get-FileHash -LiteralPath 'bin\pdf_fast_worker_backend.exe' -Algorithm SHA256).Hash"') do set "PDF_WORKER_BACKEND_SHA256=%%H"
if not defined PDF_WORKER_BACKEND_SHA256 exit /b 8
for /f %%H in ('powershell.exe -NoProfile -Command "(Get-FileHash -LiteralPath 'bin\mupdfcpp64.dll' -Algorithm SHA256).Hash"') do set "PDF_WORKER_MUPDF_SHA256=%%H"
if not defined PDF_WORKER_MUPDF_SHA256 exit /b 8

"%CARGO%" build --manifest-path "guard\Cargo.toml" --release --locked --offline
if errorlevel 1 exit /b 9

copy /Y "guard\target\release\pdf-worker-guard.exe" "bin\pdf_fast_worker.exe" >nul
if errorlevel 1 exit /b 10

"bin\pdf_fast_worker.exe" --version
exit /b %errorlevel%
