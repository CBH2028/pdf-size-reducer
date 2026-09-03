from __future__ import annotations

import io
import threading
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

import native_worker
from compressor import compress_pdf
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


def test_native_ladder_reuses_one_master_render(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "ladder-fixture.pdf"
    rectangle = _make_native_fixture(source)
    requests = [
        NativeRenderRequest(0, 0, rectangle, 144, 80, 7),
        NativeRenderRequest(1, 0, rectangle, 144, 90, 7),
        NativeRenderRequest(2, 0, rectangle, 72, 75, 7),
    ]

    with NativeWorkerSession(source, threads=2) as session:
        rendered = session.render_ladder(requests, tmp_path / "ladder")
        response = session.last_response

    assert response["master_renders"] == 1
    assert response["variants"] == 3
    assert Image.open(io.BytesIO(rendered[0])).size == (520, 300)
    assert Image.open(io.BytesIO(rendered[1])).size == (520, 300)
    assert Image.open(io.BytesIO(rendered[2])).size == (260, 150)
    assert rendered[0] != rendered[1]


def test_compressor_uses_global_native_planner(
    tmp_path: Path,
    native_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "planner-source.pdf"
    output = tmp_path / "planner-output.pdf"
    rectangle = fitz.Rect(20, 20, 340, 200)
    bitmap = Image.new("RGB", (640, 320))
    bitmap.putdata(
        [
            (
                (x * 17 + y * 29) % 256,
                (x * 37 + y * 11) % 256,
                (x * 7 + y * 43) % 256,
            )
            for y in range(bitmap.height)
            for x in range(bitmap.width)
        ]
    )
    buffer = io.BytesIO()
    bitmap.save(buffer, format="PNG")
    document = fitz.open()
    page = document.new_page(width=360, height=240)
    page.insert_image(fitz.Rect(20, 20, 340, 180), stream=buffer.getvalue())
    page.draw_rect(rectangle, color=(0, 0, 1), width=2)
    page.insert_text((35, 55), "SEARCHABLE FIGURE TEXT", fontsize=12)
    document.save(source)
    document.close()

    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_NATIVE", raising=False)
    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_PLANNER", raising=False)
    result = compress_pdf(
        source,
        output,
        180_000,
        selected_image_xrefs=set(),
        selected_vector_pages=set(),
        selected_figure_regions={0: [tuple(rectangle)]},
    )

    assert result.planned_mode is True
    assert result.native_worker_used is True
    assert result.native_master_renders == 1
    assert result.planned_variants == 14
    assert result.candidate_attempts <= 2
    assert output.stat().st_size <= 180_000
    with fitz.open(output) as compressed:
        assert "SEARCHABLE FIGURE TEXT" in compressed[0].get_text()


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
