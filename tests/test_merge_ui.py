from __future__ import annotations

import os
import time
from pathlib import Path

import pymupdf as fitz
import pytest

from compressor import CompressionError, get_pdf_source_state, merge_pdfs


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        assert time.monotonic() < deadline, "Merge queue did not finish in time"
        time.sleep(0.01)
    app.processEvents()


def make_pdf(path, pages=1):
    with fitz.open() as document:
        for number in range(pages):
            document.new_page().insert_text((40, 40), f"{path.stem} / {number + 1}")
        document.save(path)
    return path.resolve()


@pytest.fixture
def sources(tmp_path):
    return [make_pdf(tmp_path / f"part{n}.pdf", n) for n in (1, 2, 3, 4, 10)]


@pytest.fixture
def dialog_factory(app):
    import qt_app
    dialogs = []

    def create(paths=(), protected=(), inspected=True):
        dialog = qt_app.PDFMergeDialog(None, list(paths), protected)
        dialogs.append(dialog)
        if inspected:
            # Most queue tests don't need a subprocess. Real inspection has
            # its own end-to-end cases below.
            from merge_ui import inspect_merge_input
            for path in paths:
                dialog._info_ready(dialog._generation, inspect_merge_input(path))
        return dialog

    yield create
    for dialog in dialogs:
        dialog.reject()
        wait_for(app, lambda: dialog.thread is None)
        dialog.deleteLater()
    app.processEvents()


def select_rows(dialog, rows):
    dialog.file_list.clearSelection()
    for row in rows:
        dialog.file_list.item(row).setSelected(True)


@pytest.mark.parametrize("rows,offset,expected", [
    ([1, 2], -1, [1, 2, 0, 3, 4]),
    ([1, 2], 1, [0, 3, 1, 2, 4]),
    ([1, 3], -1, [1, 0, 3, 2, 4]),
    ([1, 3], 1, [0, 2, 1, 4, 3]),
    ([0, 1], -1, [0, 1, 2, 3, 4]),
    ([3, 4], 1, [0, 1, 2, 3, 4]),
    ([0, 1, 2, 3, 4], -1, [0, 1, 2, 3, 4]),
])
def test_multi_selection_moves_keep_order_and_selection(sources, dialog_factory, rows, offset, expected):
    from PySide6.QtCore import Qt
    dialog = dialog_factory(sources)
    select_rows(dialog, rows)
    dialog._move_selected(offset)
    assert dialog.source_paths() == [sources[i] for i in expected]
    assert {Path(item.data(Qt.ItemDataRole.UserRole)) for item in dialog.file_list.selectedItems()} == {sources[i] for i in rows}
    assert bool(dialog._history) == (expected != list(range(5)))
    if dialog._history:
        dialog._undo()
        assert dialog.source_paths() == sources


def test_natural_sort_and_undo(sources, dialog_factory):
    original = [sources[4], sources[1], sources[0]]
    dialog = dialog_factory(original)
    dialog._sort_files()
    assert dialog.source_paths() == [sources[0], sources[1], sources[4]]
    dialog._sort_files()
    assert len(dialog._history) == 1
    dialog._undo()
    assert dialog.source_paths() == original


def test_remove_clear_undo_never_delete_files(sources, dialog_factory):
    before = {path: path.read_bytes() for path in sources}
    dialog = dialog_factory(sources)
    select_rows(dialog, [1, 3])
    dialog._remove_selected()
    assert dialog.source_paths() == [sources[i] for i in (0, 2, 4)]
    dialog._clear()
    assert dialog.source_paths() == []
    dialog._undo()
    assert dialog.source_paths() == [sources[i] for i in (0, 2, 4)]
    dialog._undo()
    assert dialog.source_paths() == sources
    assert {path: path.read_bytes() for path in sources} == before


def test_drop_files_reports_duplicates_and_invalid(sources, tmp_path, dialog_factory):
    from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    dialog = dialog_factory(sources[:1])
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in [*sources[:2], tmp_path / "missing.pdf", tmp_path]])
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog.file_list.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog.file_list.dropEvent(drop)
    assert drop.isAccepted()
    assert dialog.source_paths() == sources[:2]
    assert "1 个重复" in dialog.notice.text()
    assert "2 个非 PDF" in dialog.notice.text()


def test_queue_limit_is_reported(tmp_path, dialog_factory):
    paths = [make_pdf(tmp_path / f"{i}.pdf") for i in range(101)]
    dialog = dialog_factory()
    dialog.add_paths(paths)
    assert len(dialog.source_paths()) == 100
    assert "1 个超出上限" in dialog.notice.text()


def test_default_output_is_safe_and_custom_name_survives_sort(sources, dialog_factory):
    from merge_ui import suggest_merge_output
    first = sources[4]
    default = suggest_merge_output(first)
    default.write_bytes(b"existing result")
    dialog = dialog_factory([first, sources[0]])
    assert dialog.output_path().name == "part10_合并 (2).pdf"
    assert dialog.output_path() not in sources
    dialog.output_edit.setText("my report")
    dialog._output_edited("my report")
    dialog._sort_files()
    assert dialog.output_path() == first.parent / "my report.pdf"
    assert default.read_bytes() == b"existing result"


def test_protected_default_name_is_skipped(sources, dialog_factory):
    from merge_ui import suggest_merge_output
    protected = suggest_merge_output(sources[0])
    dialog = dialog_factory(sources[:2], [protected])
    assert dialog.output_path() != protected


@pytest.mark.parametrize("mode", [0, 1])
def test_save_modes_accept_without_writing(sources, dialog_factory, mode):
    from PySide6.QtWidgets import QDialog
    dialog = dialog_factory(sources[:2])
    dialog.after_combo.setCurrentIndex(mode)
    output = dialog.output_path()
    dialog._validate_and_accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.load_after_merge() == bool(mode)
    assert len(dialog.source_states()) == 2
    assert not output.exists()


def test_declining_overwrite_keeps_queue_open(sources, tmp_path, dialog_factory, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"previous result")
    dialog = dialog_factory(sources[:2])
    dialog.output_edit.setText(str(output))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    dialog._validate_and_accept()
    assert not dialog._closing
    assert dialog.source_paths() == sources[:2]
    assert output.read_bytes() == b"previous result"
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog._validate_and_accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert output.read_bytes() == b"previous result"


@pytest.mark.parametrize("target", ["source", "workspace", "directory", "wrong_suffix", "empty"])
def test_invalid_destinations_do_not_close_queue(sources, tmp_path, dialog_factory, target):
    workspace = make_pdf(tmp_path / "active.pdf")
    folder = tmp_path / "folder.pdf"
    folder.mkdir()
    destination = {"source": sources[0], "workspace": workspace, "directory": folder,
                   "wrong_suffix": tmp_path / "out.txt", "empty": ""}[target]
    dialog = dialog_factory(sources[:2], [workspace])
    dialog.output_edit.setText(str(destination))
    dialog._validate_and_accept()
    assert not dialog._closing
    assert dialog.source_paths() == sources[:2]


def test_stale_sources_require_recheck_in_ui_and_engine(sources, tmp_path, dialog_factory):
    dialog = dialog_factory(sources[:2])
    expected = dialog.source_states()
    with sources[0].open("ab") as stream:
        stream.write(b"\nmodified\n")
    dialog._validate_and_accept()
    assert not dialog._closing
    assert "已变化" in dialog.notice.text()
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"previous result")
    with pytest.raises(CompressionError, match="预检后发生变化"):
        merge_pdfs(sources[:2], output, expected_source_states=expected)
    assert output.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".pdf_size_reducer_merge_*"))


def test_preflight_requires_all_expected_sources(sources, tmp_path):
    with pytest.raises(CompressionError, match="预检后发生变化"):
        merge_pdfs(sources[:2], tmp_path / "out.pdf", expected_source_states={sources[0]: get_pdf_source_state(sources[0])})
    assert not (tmp_path / "out.pdf").exists()


def test_checked_worker_rejects_late_changes(sources, tmp_path):
    import qt_app
    expected = {path: get_pdf_source_state(path) for path in sources[:2]}
    with sources[1].open("ab") as stream:
        stream.write(b"\nchanged after confirmation\n")
    output = tmp_path / "out.pdf"
    output.write_bytes(b"existing output")
    errors, results = [], []
    worker = qt_app.MergeWorker(sources[:2], output, expected)
    worker.failed.connect(errors.append)
    worker.completed.connect(results.append)
    worker.run()
    assert not results and len(errors) == 1
    assert "预检后发生变化" in errors[0]
    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".pdf_size_reducer_*"))


def test_background_page_counts_and_recheck(app, sources, dialog_factory):
    dialog = dialog_factory(sources[:2], inspected=False)
    assert not dialog.merge_button.isEnabled()
    wait_for(app, lambda: len(dialog._infos) == 2 and dialog.thread is None)
    assert "3 页" in dialog.count_label.text()
    assert dialog.merge_button.isEnabled()
    old = dialog._infos[sources[1]]
    with sources[1].open("ab") as stream:
        stream.write(b"\nsource updated\n")
    dialog._recheck()
    dialog._info_ready(dialog._generation - 1, old)
    assert not dialog._infos
    wait_for(app, lambda: len(dialog._infos) == 2 and dialog.thread is None)
    assert dialog._infos[sources[1]].state != old.state
    assert dialog.merge_button.isEnabled()


def test_background_bad_and_encrypted_files_block_merge(app, sources, tmp_path, dialog_factory):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a PDF")
    locked = tmp_path / "locked.pdf"
    with fitz.open() as document:
        document.new_page()
        document.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    dialog = dialog_factory([sources[0], broken, locked], inspected=False)
    wait_for(app, lambda: len(dialog._infos) == 3 and dialog.thread is None)
    assert "2 个不可用" in dialog.count_label.text()
    assert "密码保护" in dialog._infos[locked].error
    assert not dialog.merge_button.isEnabled()
    select_rows(dialog, [1, 2])
    dialog._remove_selected()
    dialog.add_paths([sources[1]])
    wait_for(app, lambda: sources[1] in dialog._infos and dialog.thread is None)
    assert dialog.merge_button.isEnabled()


def _hung_inspection(paths, result_queue, cancel_event):
    while True:
        time.sleep(0.1)


def test_close_cancels_hung_inspector(app, sources, dialog_factory, monkeypatch):
    import qt_app
    monkeypatch.setattr(qt_app, "_merge_inspection_process", _hung_inspection)
    dialog = dialog_factory(sources[:2], inspected=False)
    dialog._start_inspection()
    assert dialog.thread is not None
    start = time.monotonic()
    dialog.close()
    wait_for(app, lambda: dialog.thread is None, timeout=8)
    assert time.monotonic() - start < 8
    assert dialog._closing


def test_form_detection_and_missing_file(sources, tmp_path):
    from merge_ui import inspect_merge_input
    form = tmp_path / "form.pdf"
    with fitz.open() as document:
        page = document.new_page()
        widget = fitz.Widget()
        widget.field_name = "example"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.rect = fitz.Rect(10, 10, 100, 30)
        page.add_widget(widget)
        document.save(form)
    assert inspect_merge_input(form).has_forms
    assert inspect_merge_input(tmp_path / "missing.pdf").error


def test_default_reader_open_is_explicit(sources, dialog_factory, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    dialog = dialog_factory(sources[:2])
    assert not opened
    dialog._open_item(dialog.file_list.item(1))
    assert [Path(path) for path in opened] == [sources[1]]


def test_keyboard_shortcuts_move_remove_and_confirm(app, sources, dialog_factory):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog
    dialog = dialog_factory(sources[:3])
    dialog.show()
    app.processEvents()
    select_rows(dialog, [1, 2])
    QTest.keyClick(dialog.file_list, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert dialog.source_paths() == [sources[1], sources[2], sources[0]]
    select_rows(dialog, [2])
    QTest.keyClick(dialog.file_list, Qt.Key.Key_Delete)
    assert dialog.source_paths() == sources[1:3]
    assert all(path.is_file() for path in sources)
    dialog.output_edit.setFocus()
    QTest.keyClick(dialog.output_edit, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_inspection_failure_is_visible_and_does_not_loop(app, sources, dialog_factory, monkeypatch):
    import qt_app

    def fail_start(worker, *args, **kwargs):
        worker.failed.emit("Simulated process start failure")

    monkeypatch.setattr(qt_app, "_run_pdf_process", fail_start)
    dialog = dialog_factory(sources[:2], inspected=False)
    wait_for(app, lambda: len(dialog._infos) == 2 and dialog.thread is None)
    assert not dialog.merge_button.isEnabled()
    assert all("Simulated process start failure" in info.error for info in dialog._infos.values())
    app.processEvents()
    assert dialog.thread is None


def test_editor_entry_points_removed(app):
    import qt_app
    root = Path(qt_app.__file__).parent
    assert not hasattr(qt_app, "PDFEditorDialog")
    assert not hasattr(qt_app.MainWindow, "open_pdf_editor")
    assert not (root / "pdf_editor.py").exists()
    assert not (root / "qt_editor.py").exists()
    window = qt_app.MainWindow()
    try:
        assert not hasattr(window, "edit_button")
        assert window.merge_button.isEnabled()
    finally:
        window.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_main_window_prefills_current_pdf_and_passes_checked_states(app, sources, monkeypatch, explicit):
    import qt_app
    from PySide6.QtWidgets import QDialog
    captured = {}

    class FakeDialog:
        def __init__(self, parent, initial_paths, protected):
            captured["initial"] = initial_paths
            captured["protected"] = protected

        def exec(self):
            return QDialog.DialogCode.Accepted

        def source_paths(self):
            return sources[:2]

        def source_states(self):
            return {path: get_pdf_source_state(path) for path in sources[:2]}

        def output_path(self):
            return sources[0].parent / "combined.pdf"

        def load_after_merge(self):
            return True

        def deleteLater(self):
            captured["deleted"] = True

    window = qt_app.MainWindow()
    window.input_path = sources[4]
    monkeypatch.setattr(qt_app, "PDFMergeDialog", FakeDialog)
    monkeypatch.setattr(window, "_start_merge", lambda *args: captured.update(start=args))
    try:
        window.open_quick_merge_dialog(sources[:2] if explicit else None)
        assert captured["initial"] == (sources[:2] if explicit else [sources[4]])
        assert captured["protected"] == [sources[4]]
        assert captured["deleted"]
        assert captured["start"][0] == sources[:2]
        assert captured["start"][2] == {path: get_pdf_source_state(path) for path in sources[:2]}
    finally:
        window.close()
