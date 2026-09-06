from dataclasses import replace
import os
from pathlib import Path
import time

import pymupdf as fitz
import pytest
from PySide6.QtCore import QRectF, QThread
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from compressor import get_pdf_source_state
from pdf_editor import TextEdit, TextStyle, detect_page_regions
from qt_editor import EditHistory, PDFEditorDialog
import qt_app


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QApplication.instance() or QApplication([])


def pump(app, predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        app.processEvents()
        assert time.monotonic() < deadline, "editor job did not finish"
        time.sleep(0.01)
    app.processEvents()


def make_source(path, rotation=0):
    with fitz.open() as doc:
        page = doc.new_page(width=500, height=600)
        page.insert_text((40, 70), "Original words", fontsize=14)
        page.set_rotation(rotation)
        doc.new_page().insert_text((40, 80), "Second page text")
        doc.save(path)


def test_history_upsert_undo_redo_and_branching():
    history = EditHistory()
    a = TextEdit("a", 0, None, (10, 10, 100, 100), "a", TextStyle())
    b = replace(a, key="b", text="b")
    history.put(a)
    history.put(b)
    assert not history.put(a)
    history.put(replace(a, text="changed"))
    history.undo()
    assert history.edits == (a, b)
    history.redo()
    assert history.edits[0].text == "changed"
    history.undo()
    history.put(replace(b, text="branch"))
    assert not history.redo_stack


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_real_editor_modify_preview_add_undo_save_and_close(tmp_path, app, monkeypatch, rotation):
    source, output = tmp_path / "source.pdf", tmp_path / "edited.pdf"
    make_source(source, rotation)
    dialog = PDFEditorDialog(source, get_pdf_source_state(source), qt_app.PDFEditorWorker)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(output), "PDF"))
    try:
        dialog.show()
        pump(app, lambda: dialog.layout_data is not None and dialog.thread is None)
        region = dialog.layout_data.regions[0]
        shape = dialog._region_items[region.key]
        assert dialog.scene.sceneRect().contains(shape.rect())
        dialog._select(region.key)
        assert dialog.text_edit.toPlainText() == "Original words"
        dialog.text_edit.setPlainText("Edited words")
        dialog._apply_current()
        pump(app, lambda: dialog.thread is None)
        assert dialog._preview_valid, dialog.status.text()
        dialog._history_action(False)
        pump(app, lambda: dialog.thread is None)
        assert not dialog.history.edits
        dialog._history_action(True)
        pump(app, lambda: dialog.thread is None)
        assert dialog.history.edits[0].text == "Edited words"
        # Use a transformed rectangle; it must map back into unrotated PDF space.
        from PySide6.QtGui import QTransform
        box = QTransform(*dialog.layout_data.rotation_matrix).mapRect(QRectF(40, 160, 250, 60))
        dialog._new_box(box)
        assert dialog._rect() == pytest.approx((40, 160, 290, 220), abs=0.02)
        assert dialog._form_outline is not None
        assert dialog.size_spin.value() == 14
        dialog.text_edit.setPlainText("Added matching text")
        dialog._apply_current()
        pump(app, lambda: dialog.thread is None)
        assert dialog._preview_valid, dialog.status.text()
        dialog._save(True)
        pump(app, lambda: dialog.thread is None and not dialog.isVisible())
        assert dialog.load_after and dialog.last_output == output
        with fitz.open(output) as doc:
            assert "Edited words" in doc[0].get_text()
            assert "Added matching text" in doc[0].get_text()
            assert "Original words" not in doc[0].get_text()
    finally:
        dialog._close_confirmed = True
        dialog.close()
        pump(app, lambda: dialog.thread is None)
        dialog.deleteLater()
        app.processEvents()


def _slow_editor(source, page_index, edits, destination, source_state, deleted_pages, result_queue, cancel_event):
    result_queue.put(("progress", 1, "started"))
    time.sleep(60)


def test_editor_close_cancels_hung_preview(tmp_path, app, monkeypatch):
    source = tmp_path / "source.pdf"
    make_source(source)
    monkeypatch.setattr(qt_app, "_editor_preview_process", _slow_editor)
    dialog = PDFEditorDialog(source, get_pdf_source_state(source), qt_app.PDFEditorWorker)
    try:
        dialog.show()
        pump(app, lambda: dialog.status.text() == "started")
        started = time.monotonic()
        dialog.close()
        pump(app, lambda: dialog.thread is None and not dialog.isVisible())
        assert time.monotonic() - started < 5
        assert not any(thread.isRunning() for thread in dialog.findChildren(QThread))
    finally:
        dialog.close()
        pump(app, lambda: dialog.thread is None)
        dialog.deleteLater()


def test_editor_worker_late_cancel_preserves_destination(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "edited.pdf"
    make_source(source)
    output.write_bytes(b"existing output")
    with fitz.open(source) as doc:
        region = detect_page_regions(doc, 0).regions[0]
    edit = TextEdit(region.key, 0, region.key, region.rect, "New words", region.style)
    worker = qt_app.PDFEditorWorker("save", source, 0, [edit], get_pdf_source_state(source), output)
    errors, cancelled, complete = [], [], []
    worker.failed.connect(errors.append)
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.completed.connect(complete.append)
    worker.progress.connect(lambda value, _text: worker.cancel() if value == 99 else None)
    worker.run()
    assert not errors and not complete and cancelled
    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".pdf_desktop_job_*"))


def test_page_delete_ui_undo_restore_save(tmp_path, app, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "edited.pdf"
    make_source(source)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(output), "PDF"))
    dialog = PDFEditorDialog(source, get_pdf_source_state(source), qt_app.PDFEditorWorker)
    try:
        dialog.show()
        pump(app, lambda: dialog.layout_data is not None and dialog.thread is None)
        dialog._delete_current_page()
        pump(app, lambda: dialog.thread is None)
        assert dialog.history.deleted_pages == {0}
        assert "恢复" in dialog.delete_page_button.text()
        assert not dialog.add_button.isEnabled()
        assert dialog.save_button.isEnabled()
        dialog._history_action(False)
        pump(app, lambda: dialog.thread is None)
        assert not dialog.history.deleted_pages
        dialog._history_action(True)
        pump(app, lambda: dialog.thread is None)
        assert dialog.history.deleted_pages == {0}
        dialog._save(True)
        pump(app, lambda: dialog.thread is None and not dialog.isVisible())
        with fitz.open(output) as doc:
            assert len(doc) == 1 and "Second page text" in doc[0].get_text()
    finally:
        dialog._close_confirmed = True
        dialog.close()
        pump(app, lambda: dialog.thread is None)
        dialog.deleteLater()
