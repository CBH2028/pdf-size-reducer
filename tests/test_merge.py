from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

from compressor import (
    CompressionCancelled,
    CompressionError,
    PasswordProtectedPDF,
    compress_pdf,
    merge_pdfs,
)


def _make_text_pdf(
    path: Path,
    labels: list[str],
    bookmark_page: int = 1,
    title: str = "",
) -> None:
    document = fitz.open()
    for label in labels:
        page = document.new_page()
        page.insert_text((72, 72), label)
    document.set_toc([[1, f"Bookmark {labels[0]}", bookmark_page]])
    if title:
        document.set_metadata({"title": title, "author": "PDF Size Reducer"})
    document.save(path)
    document.close()


def _make_image_pdf(path: Path, seed: int) -> None:
    image = Image.new("RGB", (500, 500))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = (
                (x * 17 + y * 13 + seed) % 256,
                (x * 7 + y * 19 + seed * 3) % 256,
                (x * 23 + y * 5 + seed * 7) % 256,
            )
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    document = fitz.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=stream.getvalue())
    page.insert_text((30, 30), f"Image source {seed}")
    document.save(path)
    document.close()


def test_merge_preserves_order_bookmarks_metadata_and_sources(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    output = tmp_path / "merged.pdf"
    _make_text_pdf(first, ["FIRST"], title="Combined report")
    _make_text_pdf(second, ["SECOND-A", "SECOND-B"], bookmark_page=2)
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (first, second)
    }
    progress: list[int] = []

    result = merge_pdfs(
        [first, second],
        output,
        progress_callback=lambda value, _message: progress.append(value),
    )

    assert result.source_count == 2
    assert result.page_count == 3
    assert result.output_path == output.resolve()
    assert progress[-1] == 100
    with fitz.open(output) as merged:
        assert merged.page_count == 3
        assert "FIRST" in merged[0].get_text()
        assert "SECOND-A" in merged[1].get_text()
        assert "SECOND-B" in merged[2].get_text()
        assert merged.get_toc() == [
            [1, "Bookmark FIRST", 1],
            [1, "Bookmark SECOND-A", 3],
        ]
        assert merged.metadata["title"] == "Combined report"
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (first, second)
    } == source_hashes


def test_merge_rejects_password_protected_source(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    locked = tmp_path / "locked.pdf"
    output = tmp_path / "merged.pdf"
    _make_text_pdf(first, ["FIRST"])
    document = fitz.open()
    document.new_page()
    document.save(
        locked,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    document.close()

    with pytest.raises(PasswordProtectedPDF):
        merge_pdfs([first, locked], output)

    assert not output.exists()


@pytest.mark.parametrize("cancel_at", [1, 41, 82, 94])
def test_cancelled_merge_does_not_replace_existing_output(
    tmp_path: Path, cancel_at: int,
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    output = tmp_path / "merged.pdf"
    _make_text_pdf(first, ["FIRST"])
    _make_text_pdf(second, ["SECOND"])
    output.write_bytes(b"existing output")
    cancel_event = threading.Event()

    def cancel_during_merge(value: int, _message: str) -> None:
        if value >= cancel_at:
            cancel_event.set()

    with pytest.raises(CompressionCancelled):
        merge_pdfs(
            [first, second], output,
            progress_callback=cancel_during_merge, cancel_event=cancel_event,
        )

    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".pdf_size_reducer_merge_*"))


def test_merged_result_can_be_compressed_to_target(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    merged = tmp_path / "merged.pdf"
    compressed = tmp_path / "merged-compressed.pdf"
    _make_image_pdf(first, 1)
    _make_image_pdf(second, 2)
    merge_pdfs([first, second], merged)
    target = 120 * 1024

    result = compress_pdf(
        merged,
        compressed,
        target,
        compress_vectors=False,
    )

    assert result.output_bytes <= target
    assert result.method in {"images", "lossless"}
    with fitz.open(compressed) as document:
        assert document.page_count == 2
        assert "Image source 1" in document[0].get_text()
        assert "Image source 2" in document[1].get_text()


def test_invalid_input_does_not_replace_output(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    broken = tmp_path / "broken.pdf"
    output = tmp_path / "merged.pdf"
    _make_text_pdf(first, ["FIRST"])
    broken.write_bytes(b"not a PDF")
    output.write_bytes(b"previous result")
    with pytest.raises(CompressionError):
        merge_pdfs([first, broken], output)
    assert output.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".pdf_size_reducer_merge_*"))


@pytest.mark.parametrize("count", [0, 1, 101])
def test_merge_validates_file_count(tmp_path: Path, count: int) -> None:
    source = tmp_path / "first.pdf"
    _make_text_pdf(source, ["FIRST"])
    with pytest.raises(CompressionError):
        merge_pdfs([source] * count, tmp_path / "out.pdf")


def test_merge_cannot_overwrite_source(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _make_text_pdf(first, ["FIRST"])
    _make_text_pdf(second, ["SECOND"])
    before = first.read_bytes()
    with pytest.raises(CompressionError):
        merge_pdfs([first, second], first)
    assert first.read_bytes() == before


def test_merge_preserves_navigation_and_page_geometry(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _make_text_pdf(first, ["FIRST"])
    with fitz.open() as source:
        source.new_page(width=300, height=400)
        source.new_page(width=420, height=600)
        source[1].set_rotation(90)
        source[0].insert_link({
            "kind": fitz.LINK_GOTO, "from": fitz.Rect(10, 10, 80, 30),
            "page": 1, "to": fitz.Point(25, 35),
        })
        source[0].insert_link({
            "kind": fitz.LINK_URI, "from": fitz.Rect(10, 40, 80, 60),
            "uri": "https://example.com/",
        })
        source[0].add_text_annot((100, 100), "Keep this note")
        source.set_toc([
            [1, "Section", 1, {
                "kind": fitz.LINK_GOTO, "page": 0,
                "to": fitz.Point(40, 70), "zoom": 1.5, "bold": True,
            }],
            [2, "Website", -1, {"kind": fitz.LINK_URI, "uri": "https://example.com/"}],
        ])
        source.save(second)
    output = tmp_path / "out.pdf"
    merge_pdfs([first, second], output)
    with fitz.open(output) as merged:
        assert merged[1].rect == fitz.Rect(0, 0, 300, 400)
        assert merged[2].rotation == 90
        links = merged[1].get_links()
        assert next(link for link in links if link["kind"] == fitz.LINK_GOTO)["page"] == 2
        assert next(link for link in links if link["kind"] == fitz.LINK_URI)["uri"] == "https://example.com/"
        assert next(merged[1].annots()).info["content"] == "Keep this note"
        toc = merged.get_toc(False)
        assert toc[1][2] == 2
        assert toc[1][3]["to"] == fitz.Point(40, 70)
        assert toc[1][3]["zoom"] == 1.5
        assert toc[1][3]["bold"]
        assert toc[2][3]["uri"] == "https://example.com/"


@pytest.fixture(scope="module")
def qt_application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wait_for(app, predicate, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        assert time.monotonic() < deadline, "Qt workflow did not finish in time"
        time.sleep(0.01)
    app.processEvents()


def test_merge_dialog_order_and_deduplication(tmp_path: Path, qt_application) -> None:
    import qt_app
    from PySide6.QtWidgets import QWidget

    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    _make_text_pdf(first, ["FIRST"])
    _make_text_pdf(second, ["SECOND"])
    parent = QWidget()
    dialog = qt_app.PDFMergeDialog(parent, [first, second, first])
    assert dialog.source_paths() == [first, second]
    assert dialog.load_after_merge()
    dialog.file_list.setCurrentRow(1)
    dialog._move_selected(-1)
    assert dialog.source_paths() == [second, first]
    dialog._remove_selected()
    assert dialog.source_paths() == [first]
    dialog.close()
    parent.close()


def test_qt_merge_then_compress_workflow(tmp_path: Path, qt_application, monkeypatch) -> None:
    import qt_app
    from PySide6.QtCore import QThread

    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    merged = tmp_path / "merged.pdf"
    _make_image_pdf(first, 1)
    _make_image_pdf(second, 2)
    window = qt_app.MainWindow()
    errors: list[str] = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    try:
        window._start_merge([second, first], merged)
        assert not window.merge_button.isEnabled()
        _wait_for(qt_application, lambda: bool(errors) or (
            window.input_path == merged and not window.assets_loading
            and window.merge_thread is None
        ))
        assert not errors
        assert window.start_button.isEnabled()
        assert window.assets
        with fitz.open(merged) as document:
            assert "Image source 2" in document[0].get_text()
        window.unit_combo.setCurrentText("KB")
        window.target_edit.setText("120")
        destination = window.output_path
        assert destination != merged
        window.start_compression()
        _wait_for(qt_application, lambda: bool(errors) or (
            not window.processing_busy and window.compression_thread is None
        ))
        assert not errors
        assert destination.is_file()
        assert destination.stat().st_size <= 120 * 1024
        assert window.last_output == destination
        assert window.merge_button.isEnabled()
    finally:
        window.close()
        _wait_for(qt_application, lambda: not any(
            thread.isRunning() for thread in window.findChildren(QThread)
        ))
