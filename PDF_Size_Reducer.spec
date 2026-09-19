# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import runpy


project_root = Path(SPECPATH)
splash_image = project_root / 'build' / 'startup_splash.png'
generate_splash = runpy.run_path(
    str(project_root / 'tools' / 'generate_startup_splash.py')
)['generate_startup_splash']
generate_splash(splash_image)


a = Analysis(
    ['qt_app.py'],
    pathex=[],
    binaries=[('native_worker/bin/pdf_fast_worker.exe', 'native_worker'), ('native_worker/bin/pdf_fast_worker_backend.exe', 'native_worker'), ('native_worker/bin/mupdfcpp64.dll', 'native_worker')],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
splash = Splash(
    str(splash_image),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(90, 351),
    text_size=10,
    text_font='Segoe UI',
    text_color='#777382',
    text_default='Starting PDF Size Reducer...',
    always_on_top=True,
    center='active',
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    a.binaries,
    a.datas,
    [],
    name='PDF_Size_Reducer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
