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
if not exist "%MUPDF_ROOT%\include\mupdf\classes.h" (
    echo PyMuPDF development headers were not found. Install requirements-dev.txt first.
    exit /b 3
)

call "%VS_VCVARS%" >nul
if errorlevel 1 exit /b 4

if not exist "bin" mkdir "bin"
cl.exe /nologo /std:c++17 /EHsc /O2 /GL /DNDEBUG /MD ^
    /I"%MUPDF_ROOT%\include" pdf_fast_worker.cpp ^
    /link /LTCG /OPT:REF /OPT:ICF ^
    /LIBPATH:"%MUPDF_ROOT%\lib" mupdfcpp64.lib libmuthreads.lib ^
    /OUT:"bin\pdf_fast_worker.exe"
if errorlevel 1 exit /b 5

copy /Y "%MUPDF_DLL%" "bin\mupdfcpp64.dll" >nul
if errorlevel 1 exit /b 6

"bin\pdf_fast_worker.exe" --version
exit /b %errorlevel%
