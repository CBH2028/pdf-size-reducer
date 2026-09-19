from __future__ import annotations

import os
import time
from pathlib import Path

from PIL import Image
import pytest


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _wait(app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def test_qt_startup_splash_is_visibly_animated(app) -> None:
    import qt_app

    splash = qt_app.AnimatedStartupSplash()
    splash.show()
    app.processEvents()
    first = splash.grab().toImage()
    initial_phase = splash._phase
    splash.set_status("正在准备测试工作区")
    _wait(app, 0.18)
    second = splash.grab().toImage()
    assert splash.status_text == "正在准备测试工作区"
    assert splash._phase > initial_phase
    assert first != second
    splash.close()
    splash.deleteLater()
    app.processEvents()


def test_splash_handoff_shows_main_window_and_stops_timer(app) -> None:
    import qt_app
    from PySide6.QtWidgets import QWidget

    splash = qt_app.AnimatedStartupSplash()
    target = QWidget()
    splash.show()
    app.processEvents()
    assert splash._timer.isActive()
    splash.finish(target)
    app.processEvents()
    assert target.isVisible()
    assert not splash.isVisible()
    assert not splash._timer.isActive()
    target.close()
    target.deleteLater()
    app.processEvents()


def test_early_splash_asset_is_reproducible(tmp_path: Path) -> None:
    from tools.generate_startup_splash import generate_startup_splash

    first = generate_startup_splash(tmp_path / "first.png")
    second = generate_startup_splash(tmp_path / "second.png")
    assert first.read_bytes() == second.read_bytes()
    with Image.open(first) as image:
        assert image.size == (720, 420)
        assert image.mode == "RGB"
        assert image.getpixel((360, 126))[2] > image.getpixel((360, 126))[0]


def test_spec_enables_onefile_bootloader_splash() -> None:
    project = Path(__file__).resolve().parents[1]
    spec = (project / "PDF_Size_Reducer.spec").read_text(encoding="utf-8")
    build_script = (project / "build_exe.bat").read_text(encoding="utf-8")
    assert "splash = Splash(" in spec
    assert "splash.binaries" in spec
    assert "text_default='Starting PDF Size Reducer...'" in spec
    assert "center='active'" in spec
    assert '"PDF_Size_Reducer.spec"' in build_script
    assert '--name "PDF_Size_Reducer" qt_app.py' not in build_script
