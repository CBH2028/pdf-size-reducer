from __future__ import annotations

import io
import threading
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

import native_worker
from native_worker import (
    NativeRenderRequest,
    NativeWorkerCancelled,
    NativeWorkerSession,
)


def _make_native_fixture(path: Path) -> tuple[float, float, float, float]:
    document = fitz.open()
    page = document.new_page(width=320, height=240)
    figure = fitz.Rect(30, 30, 290, 180)
    page.draw_rect(figure, color=(0, 0.5, 0), width=2)
    page.insert_text(
        (50, 70),
        "NATIVE TEXT SHOULD STAY PDF",
        fontsize=18,
        fontname="helv",
    )
    page.draw_line(fitz.Point(45, 155), fitz.Point(275, 105), color=(0, 0, 1))
    document.save(path)
    document.close()
    return tuple(figure)


@pytest.fixture
def native_binary(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_NATIVE", raising=False)
    native_worker.find_native_worker.cache_clear()
    binary = native_worker.find_native_worker()
    if binary is None:
        pytest.skip("native worker has not been built")
    return binary


def test_native_worker_protocol(native_binary: Path) -> None:
    assert native_binary.name == "pdf_fast_worker.exe"
    assert native_binary.with_name("mupdfcpp64.dll").is_file()


def test_persistent_native_session_culls_text(
    tmp_path: Path, native_binary: Path
) -> None:
    unicode_directory = tmp_path / "中文 路径"
    unicode_directory.mkdir()
    source = unicode_directory / "原始 文件.pdf"
    rectangle = _make_native_fixture(source)
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_request = NativeRenderRequest(0, 0, rectangle, 144, 85)
    second_request = NativeRenderRequest(0, 0, rectangle, 150, 85)

    with NativeWorkerSession(source, threads=2) as session:
        process_id = session.process.pid
        first = session.render([first_request], first_directory)[0]
        second = session.render([second_request], second_directory)[0]
        assert session.process.pid == process_id

    first_image = Image.open(io.BytesIO(first)).convert("RGB")
    second_image = Image.open(io.BytesIO(second)).convert("RGB")
    assert first_image.size == (520, 300)
    assert second_image.width > first_image.width

    # The expensive JPEG layer excludes native text. The compressor keeps the
    # real PDF text above it, so glyphs stay searchable and are not duplicated.
    text_area = first_image.crop((35, 35, 505, 100)).convert("L")
    assert sum(text_area.histogram()[:80]) < 10


def test_native_session_can_be_cancelled(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "cancel-fixture.pdf"
    rectangle = _make_native_fixture(source)
    request = NativeRenderRequest(0, 0, rectangle, 600, 90)
    cancellation = threading.Event()
    cancellation.set()

    with NativeWorkerSession(source, threads=2) as session:
        with pytest.raises(NativeWorkerCancelled):
            session.render(
                [request],
                tmp_path / "cancelled",
                cancel_event=cancellation,
            )
        assert session.process.poll() is not None
