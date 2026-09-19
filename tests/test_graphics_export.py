from __future__ import annotations

import json
import os
import threading
import time
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

from compressor import CompressionCancelled, CompressionError, PDFAsset, get_pdf_source_state, list_pdf_assets
from graphics_export import (
    MANIFEST_NAME,
    export_pdf_graphics,
    install_graphics_export,
    suggest_graphics_export_directory,
)


@pytest.fixture(scope="module")
def qt_app_instance():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _wait_for(app, predicate, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        assert time.monotonic() < deadline, "graphics export did not finish in time"
        time.sleep(0.01)
    app.processEvents()


def _png(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    stream = BytesIO()
    Image.new("RGBA", size, color).save(stream, "PNG")
    return stream.getvalue()


def _paper_pdf(path: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=420, height=520)
        page.draw_rect(fitz.Rect(35, 35, 225, 180), color=(0.1, 0.2, 0.9), width=2)
        page.draw_line(fitz.Point(45, 160), fitz.Point(215, 55), color=(0.8, 0.1, 0.2), width=2)
        page.insert_text((55, 75), "Searchable vector label", fontsize=12)
        page.insert_image(fitz.Rect(70, 100, 170, 145), stream=_png((160, 72), (220, 60, 30, 255)))
        page.insert_text((40, 205), "Figure 1. Vector and bitmap experiment", fontsize=10)
        page.insert_image(fitz.Rect(275, 55, 375, 135), stream=_png((320, 256), (30, 160, 220, 180)))
        document.save(path)


def test_export_preserves_figure_vectors_and_original_bitmap_pixels(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    output = tmp_path / "export"
    _paper_pdf(source)
    before = source.read_bytes()
    assets, _page_count = list_pdf_assets(source)
    assert {asset.kind for asset in assets} == {"figure", "image"}

    result = export_pdf_graphics(
        source,
        output,
        assets,
        formats={"svg", "png", "pdf"},
        expected_source_state=get_pdf_source_state(source),
    )

    assert source.read_bytes() == before
    assert result.item_count == 2
    assert result.figure_count == 1
    assert result.image_count == 1
    assert result.svg_count == 2
    assert result.png_count == 2
    assert result.pdf_count == 2
    assert result.formats == ("pdf", "png", "svg")
    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["source_file"] == source.name
    assert str(source.resolve()) not in json.dumps(manifest)
    figure = next(item for item in manifest["items"] if item["type"] == "figure")
    image = next(item for item in manifest["items"] if item["type"] == "image")
    assert figure["preservation_mode"] == "mixed-vector-raster-preserved"
    assert figure["vector_content_preserved"] is True
    assert image["preservation_mode"] == "original-raster-preserved"
    assert image["vector_content_preserved"] is False

    figure_svg = (output / figure["files"]["svg"]).read_bytes()
    assert b"<path" in figure_svg
    assert b"<image" in figure_svg
    with fitz.open(stream=figure_svg, filetype="svg") as rendered:
        assert rendered.page_count == 1
        assert rendered[0].rect.width > 100
    with fitz.open(output / figure["files"]["pdf"]) as exported_pdf:
        assert "Searchable vector label" in exported_pdf[0].get_text()
        assert exported_pdf[0].get_drawings()
    with Image.open(output / figure["files"]["png"]) as exported_figure:
        assert exported_figure.width > 1000

    png_path = output / image["files"]["png"]
    with Image.open(png_path) as exported:
        assert exported.size == (320, 256)
    image_svg = (output / image["files"]["svg"]).read_bytes()
    assert b"data:image/png;base64," in image_svg
    with fitz.open(stream=image_svg, filetype="svg") as rendered:
        assert rendered[0].get_pixmap().width == 320
    with fitz.open(output / image["files"]["pdf"]) as exported_pdf:
        embedded = exported_pdf[0].get_images(full=True)
        assert len(embedded) == 1
        assert embedded[0][2:4] == (320, 256)


def test_export_rejects_stale_source_and_removes_partial_output(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    stale = get_pdf_source_state(source)
    source.write_bytes(source.read_bytes() + b"\n")
    output = tmp_path / "export"
    with pytest.raises(CompressionError, match="已更改"):
        export_pdf_graphics(source, output, assets, expected_source_state=stale)
    assert not output.exists()

    bad = PDFAsset("bad", "figure", (99,), rect=(0, 0, 10, 10))
    with pytest.raises(CompressionError, match="页码"):
        export_pdf_graphics(source, output, [assets[0], bad])
    assert not output.exists()


def test_source_change_during_export_discards_completed_payload(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    output = tmp_path / "export"
    changed = False

    def change_after_first_item(value: int, _message: str) -> None:
        nonlocal changed
        if value > 3 and not changed:
            with source.open("ab") as stream:
                stream.write(b"\n% changed during graphics export\n")
            changed = True

    with pytest.raises(CompressionError, match="导出期间发生变化"):
        export_pdf_graphics(source, output, assets, progress_callback=change_after_first_item)
    assert changed
    assert not output.exists()


def test_export_cancellation_leaves_no_directory(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    cancellation = threading.Event()
    cancellation.set()
    output = tmp_path / "cancelled"
    with pytest.raises(CompressionCancelled):
        export_pdf_graphics(source, output, assets, cancel_event=cancellation)
    assert not output.exists()

    cancellation.clear()
    with pytest.raises(CompressionCancelled):
        export_pdf_graphics(
            source,
            output,
            assets,
            cancel_event=cancellation,
            progress_callback=lambda value, _text: cancellation.set() if value > 3 else None,
        )
    assert not output.exists()


@pytest.mark.parametrize("selected", [{"svg"}, {"png"}, {"pdf"}])
def test_only_user_selected_format_is_written(
    tmp_path: Path, selected: set[str]
) -> None:
    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    output = tmp_path / next(iter(selected))
    result = export_pdf_graphics(source, output, assets, formats=selected)
    payload_extensions = {
        path.suffix.lower().lstrip(".")
        for path in output.iterdir()
        if path.name != MANIFEST_NAME
    }
    assert payload_extensions == selected
    assert result.formats == tuple(sorted(selected))
    assert result.svg_count == (len(assets) if "svg" in selected else 0)
    assert result.png_count == (len(assets) if "png" in selected else 0)
    assert result.pdf_count == (len(assets) if "pdf" in selected else 0)
    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["requested_formats"] == sorted(selected)


@pytest.mark.parametrize("formats", [set(), {"webp"}, {"pdf", "exe"}])
def test_invalid_format_selection_is_rejected_before_output(
    tmp_path: Path, formats: set[str]
) -> None:
    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    output = tmp_path / "invalid"
    with pytest.raises(CompressionError, match="至少选择|不支持"):
        export_pdf_graphics(source, output, assets, formats=formats)
    assert not output.exists()


def test_suggested_directory_is_unique_and_windows_safe(tmp_path: Path) -> None:
    source = tmp_path / "A strange name.pdf"
    first = suggest_graphics_export_directory(source, tmp_path)
    assert first.name == "A_strange_name_高清图"
    first.mkdir()
    assert suggest_graphics_export_directory(source, tmp_path).name == "A_strange_name_高清图 (2)"


def test_install_graphics_export_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    candidate = tmp_path / "candidate"
    result = export_pdf_graphics(source, candidate, assets)
    destination = tmp_path / "final"
    install_graphics_export(
        result.output_directory,
        destination,
        source,
        get_pdf_source_state(source),
    )
    assert (destination / MANIFEST_NAME).is_file()
    assert not candidate.exists()

    second = tmp_path / "candidate2"
    export_pdf_graphics(source, second, assets)
    with pytest.raises(CompressionError, match="不会覆盖|未覆盖"):
        install_graphics_export(
            second,
            destination,
            source,
            get_pdf_source_state(source),
        )
    assert second.exists()


def test_desktop_worker_stages_then_atomically_installs(tmp_path: Path) -> None:
    import qt_app

    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    destination = tmp_path / "worker-export"
    worker = qt_app.GraphicsExportWorker(
        source, destination, assets, get_pdf_source_state(source)
    )
    completed, failed, cancelled = [], [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.run()
    assert not failed and not cancelled
    assert len(completed) == 1
    assert completed[0].output_directory == destination.resolve()
    assert completed[0].manifest_path == destination.resolve() / MANIFEST_NAME
    assert not list(tmp_path.glob(".pdf_graphics_job_*"))


def test_desktop_worker_cancel_at_final_progress_never_installs(tmp_path: Path) -> None:
    import qt_app

    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    destination = tmp_path / "cancelled-export"
    worker = qt_app.GraphicsExportWorker(
        source, destination, assets, get_pdf_source_state(source)
    )
    terminals = []
    worker.completed.connect(lambda _result: terminals.append("completed"))
    worker.failed.connect(lambda message: terminals.append(message))
    worker.cancelled.connect(lambda: terminals.append("cancelled"))
    worker.progress.connect(
        lambda value, _text: worker.cancel() if value >= 97 else None
    )
    worker.run()
    assert terminals == ["cancelled"]
    assert not destination.exists()
    assert not list(tmp_path.glob(".pdf_graphics_job_*"))


def test_main_window_exports_only_selected_assets(
    tmp_path: Path, qt_app_instance, monkeypatch
) -> None:
    import qt_app

    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    window = qt_app.MainWindow()
    window.input_path = source
    window.input_source_state = get_pdf_source_state(source)
    window.assets = assets
    chosen = assets[0]
    window.selected_asset_keys = {chosen.key}
    window.assets_loading = False
    window._set_busy(False)
    assert window.export_graphics_button.isEnabled()
    monkeypatch.setattr(
        qt_app.QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(tmp_path),
    )

    window.export_all_graphics({"svg", "png", "pdf"})
    assert window.processing_busy
    assert not window.export_graphics_button.isEnabled()
    _wait_for(qt_app_instance, lambda: window.graphics_export_thread is None)

    assert not window.processing_busy
    assert window.last_output is not None and window.last_output.is_dir()
    assert (window.last_output / MANIFEST_NAME).is_file()
    manifest = json.loads(
        (window.last_output / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["item_count"] == 1
    assert [item["source_asset_key"] for item in manifest["items"]] == [chosen.key]
    assert manifest["svg_count"] == 1
    assert manifest["png_count"] == 1
    assert manifest["pdf_count"] == 1
    assert window.open_button.text() == "打开高清图文件夹"
    window.close()
    qt_app_instance.processEvents()


def test_export_action_tracks_selection_and_rejects_empty_selection(
    tmp_path: Path, qt_app_instance, monkeypatch
) -> None:
    import qt_app

    source = tmp_path / "paper.pdf"
    _paper_pdf(source)
    assets, _page_count = list_pdf_assets(source)
    window = qt_app.MainWindow()
    window.input_path = source
    window.input_source_state = get_pdf_source_state(source)
    window.assets = assets
    window.selected_asset_keys = {asset.key for asset in assets}
    window.assets_loading = False
    window._set_busy(False)
    assert window.export_graphics_button.isEnabled()

    window._set_selection(None, False)
    assert not window.export_graphics_button.isEnabled()
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    window.export_all_graphics({"pdf"})
    assert errors == ["请先在右侧勾选至少一个需要导出的图形。"]
    assert window.graphics_export_thread is None

    window._asset_selection_changed(assets[-1].key, True)
    assert window.export_graphics_button.isEnabled()
    window.close()
    qt_app_instance.processEvents()


def test_format_dialog_uses_checkboxes_and_requires_one_selection(
    qt_app_instance,
) -> None:
    import qt_app
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = qt_app.GraphicsExportOptionsDialog()
    assert dialog.selected_formats() == frozenset({"svg"})
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()
    dialog.svg_check.setChecked(False)
    assert not ok.isEnabled()
    dialog.pdf_check.setChecked(True)
    assert ok.isEnabled()
    assert dialog.selected_formats() == frozenset({"pdf"})
    dialog.close()
    dialog.deleteLater()
    qt_app_instance.processEvents()
