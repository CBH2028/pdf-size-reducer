from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pymupdf as fitz
import pytest

from compressor import get_pdf_source_state
from merge_ui import inspect_merge_input
from pdf_composer import PageRef


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def wait(app, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        assert time.monotonic() < deadline, "Visual composer did not finish in time"
        time.sleep(.01)
    app.processEvents()


def pdf(path, count=3):
    with fitz.open() as document:
        for number in range(count):
            page = document.new_page(width=320, height=450)
            page.insert_text((30, 45), f"{path.stem} page {number + 1}")
            page.draw_rect(fitz.Rect(25, 90, 280, 220), fill=(.8, .9, 1), color=(.1, .3, .6))
        document.save(path)
    return path.resolve()


@pytest.fixture
def sources(tmp_path):
    return [pdf(tmp_path / name) for name in ("main.pdf", "material.pdf", "appendix.pdf")]


@pytest.fixture
def factory(app):
    import qt_app
    dialogs = []

    def create(paths=(), inspect=True):
        dialog = qt_app.PDFComposerDialog(None, paths)
        dialogs.append(dialog)
        if inspect:
            for path in paths:
                dialog.info_ready(dialog._generation, inspect_merge_input(path))
        return dialog

    yield create
    for dialog in dialogs:
        dialog.close()
        wait(app, lambda: not dialog.jobs)
        dialog.deleteLater()
    app.processEvents()


def test_initial_three_panes_and_page_insertion(sources, factory):
    dialog = factory(sources)
    assert dialog.main_browser.count() == 3
    assert dialog.material_browser.source == sources[1]
    from composer_ui import ROLE
    assert [item.data(ROLE) for item in dialog.materials.selectedItems()] == [sources[1]]
    assert len(dialog.paths) == 3
    assert dialog.plan.pages() == [PageRef(sources[0], i) for i in range(3)]
    first = dialog.plan.root.children[0].children[0]
    dialog.tree.setCurrentItem(dialog.tree_items[first.uid])
    dialog.material_browser.selected = [2, 0]
    dialog.material_browser.insert_selected()
    assert dialog.plan.pages() == [PageRef(sources[0], 0), PageRef(sources[1], 2), PageRef(sources[1], 0), PageRef(sources[0], 1), PageRef(sources[0], 2)]
    assert dialog.output_browser.output_refs == dialog.plan.pages()
    assert len(dialog.source_states()) == 2
    dialog.undo()
    assert len(dialog.plan.pages()) == 3
    dialog.redo()
    assert len(dialog.plan.pages()) == 5


@pytest.mark.parametrize("mode,position", [(0, 2), (1, 1), (3, 3)])
def test_button_insertion_position(sources, factory, mode, position):
    dialog = factory(sources)
    second = dialog.plan.root.children[0].children[1]
    dialog.tree.setCurrentItem(dialog.tree_items[second.uid])
    dialog.insert_mode.setCurrentIndex(mode)
    dialog.insert_pages([PageRef(sources[1], 1)])
    assert dialog.plan.pages()[position] == PageRef(sources[1], 1)


def test_insert_into_selected_group_and_reject_page_container(sources, factory):
    dialog = factory(sources)
    group = dialog.plan.root.children[0]
    dialog.tree.setCurrentItem(dialog.tree_items[group.uid])
    dialog.insert_mode.setCurrentIndex(2)
    dialog.insert_pages([PageRef(sources[1], 1)])
    assert len(dialog.plan.find(group.uid).children) == 4
    leaf = dialog.plan.find(group.uid).children[0]
    dialog.tree.setCurrentItem(dialog.tree_items[leaf.uid])
    dialog.insert_pages([PageRef(sources[1], 2)])
    assert len(dialog.plan.pages()) == 4
    assert "先选中分组" in dialog.notice.text()


def test_multi_selected_tree_pages_stay_selected_after_move(sources, factory):
    dialog = factory(sources)
    group = dialog.plan.root.children[0]
    selected = [node.uid for node in group.children[1:]]
    dialog.tree.clearSelection()
    for uid in selected:
        dialog.tree_items[uid].setSelected(True)
    dialog.shift_nodes(-1)
    assert set(dialog.selected_ids()) == set(selected)
    assert [ref.index for ref in dialog.plan.pages()] == [1, 2, 0]


def drop_payload(dialog, payload, position):
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent
    from composer_ui import PAGE_MIME
    mime = QMimeData()
    mime.setData(PAGE_MIME, json.dumps(payload).encode())
    event = QDropEvent(QPointF(position), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    dialog.tree.dropEvent(event)
    return event.isAccepted()


def test_drag_page_into_group_and_move_between_branches(app, sources, factory):
    from PySide6.QtCore import QPoint
    dialog = factory(sources)
    dialog.show()
    app.processEvents()
    group = dialog.plan.root.children[0]
    position = dialog.tree.visualItemRect(dialog.tree_items[group.uid]).center()
    assert drop_payload(dialog, {"session": dialog.session, "kind": "pages", "pages": [[str(sources[1]), 2]]}, position)
    assert len(dialog.plan.find(group.uid).children) == 4
    leaf = dialog.plan.find(group.uid).children[-1]
    assert drop_payload(dialog, {"session": dialog.session, "kind": "nodes", "ids": [leaf.uid]}, QPoint(5, dialog.tree.viewport().height() - 5))
    assert dialog.plan.root.children[-1].page == PageRef(sources[1], 2)
    dialog.undo()
    assert len(dialog.plan.find(group.uid).children) == 4


def test_drag_documents_creates_branches_with_one_undo(app, sources, factory):
    from PySide6.QtCore import QPoint
    dialog = factory(sources)
    dialog.show()
    app.processEvents()
    assert drop_payload(dialog, {"session": dialog.session, "kind": "documents", "paths": [str(path) for path in sources[1:]]}, QPoint(5, dialog.tree.viewport().height() - 5))
    assert len(dialog.plan.root.children) == 3
    assert len(dialog.plan.pages()) == 9
    dialog.undo()
    assert len(dialog.plan.pages()) == 3


def test_foreign_or_unknown_page_drop_is_rejected(sources, tmp_path, factory):
    from PySide6.QtCore import QPoint
    dialog = factory(sources)
    assert not drop_payload(dialog, {"session": "another-workspace", "kind": "pages", "pages": [[str(sources[1]), 0]]}, QPoint(1, 1))
    assert not drop_payload(dialog, {"session": dialog.session, "kind": "pages", "pages": [[str(tmp_path / "unknown.pdf"), 0]]}, QPoint(1, 1))
    assert len(dialog.plan.pages()) == 3


def test_material_library_can_exceed_old_100_file_limit(tmp_path, factory):
    paths = [pdf(tmp_path / f"material-{n}.pdf", 1) for n in range(105)]
    dialog = factory()
    dialog.add_materials(paths)
    assert len(dialog.paths) == 105
    assert dialog.materials.count() == 105
    assert not dialog.plan.pages()
    dialog.material_search.setText("material-104")
    assert sum(not item.isHidden() for item in dialog.material_items.values()) == 1


def test_browser_pagination_and_cross_chunk_selection(tmp_path, factory):
    path = pdf(tmp_path / "large.pdf", 80)
    dialog = factory([path])
    browser = dialog.main_browser
    assert browser.grid.count() == 24
    browser.range_edit.setText("2, 30-32, 70")
    browser.select_range()
    assert [ref.index for ref in browser.selected_refs()] == [1, 29, 30, 31, 69]
    browser.show_chunk(2)
    assert browser.grid.count() == 24
    assert len(browser.selected_refs()) == 5
    browser.show_chunk(3)
    assert browser.grid.count() == 8


def test_invalid_pdf_does_not_block_valid_selected_pages(sources, tmp_path, factory):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"invalid PDF")
    dialog = factory([*sources, broken])
    assert dialog.infos[broken].error
    assert dialog.save_button.isEnabled()
    dialog.insert_pages([PageRef(broken, 0)])
    assert len(dialog.plan.pages()) == 3


def test_change_main_keeps_composition(sources, factory):
    dialog = factory(sources)
    before = dialog.plan.pages()
    dialog.set_main(sources[2])
    assert dialog.main_browser.source == sources[2]
    assert dialog.plan.pages() == before


def test_reject_stale_output_and_source_overwrite(sources, factory):
    dialog = factory(sources)
    dialog.output_edit.setText(str(sources[2]))
    dialog.validate_and_accept()
    assert not dialog._closing and "不能覆盖" in dialog.notice.text()
    dialog.output_edit.setText(str(sources[0].parent / "result.pdf"))
    with sources[0].open("ab") as stream:
        stream.write(b"\nchanged\n")
    dialog.validate_and_accept()
    assert not dialog._closing and "已变化" in dialog.notice.text()


def test_declining_overwrite_retains_tree(sources, tmp_path, factory, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    dialog = factory(sources)
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous result")
    dialog.output_edit.setText(str(output))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    dialog.validate_and_accept()
    assert not dialog._closing and len(dialog.plan.pages()) == 3
    assert output.read_bytes() == b"previous result"


def test_actual_metadata_thumbnails_and_zoom_preview(app, sources, factory):
    dialog = factory(sources, inspect=False)
    dialog.show()
    wait(app, lambda: len(dialog.infos) == 3 and len(dialog.cache) >= 6 and not dialog.jobs)
    assert dialog.main_browser.count() == 3 and dialog.material_browser.count() == 3
    assert not dialog.main_browser.grid.item(0).icon().isNull()
    dialog.open_preview(PageRef(sources[1], 1))
    wait(app, lambda: "preview" not in dialog.jobs)
    assert dialog.preview_canvas.scene().items()
    dialog.preview_dialog.close()


def test_inspection_callbacks_stay_on_gui_thread(app, sources, factory, monkeypatch):
    from PySide6.QtCore import QThread
    dialog = factory(sources, inspect=False)
    threads = []
    original = dialog.info_ready

    def record(generation, info):
        threads.append(QThread.currentThread() is app.thread())
        original(generation, info)

    monkeypatch.setattr(dialog, "info_ready", record)
    wait(app, lambda: len(dialog.infos) == 3 and "inspect" not in dialog.jobs)
    assert threads and all(threads)


def test_recheck_source_does_not_reset_tree(app, sources, factory):
    dialog = factory(sources)
    before = dialog.plan.pages()
    with sources[0].open("ab") as stream:
        stream.write(b"\nrevision\n")
    dialog.recheck()
    assert not dialog.save_button.isEnabled()
    wait(app, lambda: len(dialog.infos) == 3 and "inspect" not in dialog.jobs)
    assert dialog.plan.pages() == before
    assert dialog.save_button.isEnabled()
    assert dialog.infos[sources[0]].state == get_pdf_source_state(sources[0])


def test_recheck_shortened_pdf_blocks_missing_pages(app, sources, factory):
    dialog = factory(sources)
    pdf(sources[0], 1)
    dialog.recheck()
    wait(app, lambda: len(dialog.infos) == 3 and "inspect" not in dialog.jobs)
    assert not dialog.save_button.isEnabled()
    assert len(dialog.plan.pages()) == 3
    assert dialog.ref_items[PageRef(sources[0], 0)][0].foreground(0).color().name() == "#18181b"
    assert dialog.ref_items[PageRef(sources[0], 2)][0].foreground(0).color().name() == "#c03535"


def test_minimum_window_keeps_save_controls_visible(app, sources, factory):
    from PySide6.QtCore import QPoint
    dialog = factory(sources)
    dialog.resize(1100, 700)
    dialog.show()
    app.processEvents()
    corner = dialog.save_button.mapTo(dialog, QPoint(dialog.save_button.width(), dialog.save_button.height()))
    assert corner.x() <= dialog.width() and corner.y() <= dialog.height()
    assert dialog.main_browser.grid.height() > 50
    assert dialog.material_browser.grid.height() > 50


def _hung_renderer(requests, max_side, directory, result_queue, cancel_event):
    while True:
        time.sleep(.1)


def test_close_stops_unresponsive_page_renderer(app, sources, factory, monkeypatch):
    import qt_app
    monkeypatch.setattr(qt_app, "_page_render_process", _hung_renderer)
    dialog = factory(sources)
    dialog.start_thumbnails()
    assert "thumb" in dialog.jobs
    dialog.close()
    wait(app, lambda: not dialog.jobs, timeout=8)
    assert dialog._closing


def test_real_composition_worker_and_compression_handoff(app, sources, tmp_path, monkeypatch):
    import qt_app
    window = qt_app.MainWindow()
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    selected = [PageRef(sources[0], 0), PageRef(sources[1], 2), PageRef(sources[0], 1)]
    output = tmp_path / "composed.pdf"
    try:
        window._start_merge(sources[:2], output, {path: get_pdf_source_state(path) for path in sources[:2]}, selected, [[1, "Assembled", 1]])
        wait(app, lambda: errors or (window.input_path == output and not window.assets_loading and window.merge_thread is None))
        assert not errors and window.start_button.isEnabled()
        with fitz.open(output) as document:
            assert document.page_count == 3
            assert "material page 3" in document[1].get_text()
        window.target_edit.setText("100")
        window.unit_combo.setCurrentText("KB")
        window.start_compression()
        wait(app, lambda: errors or (not window.processing_busy and window.compression_thread is None))
        assert not errors and window.last_output != output
        assert window.last_output.stat().st_size <= 100 * 1024
    finally:
        window.close()
        from PySide6.QtCore import QThread
        wait(app, lambda: not any(thread.isRunning() for thread in window.findChildren(QThread)))


def test_composition_worker_final_cancel_keeps_previous(sources, tmp_path):
    import qt_app
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous result")
    worker = qt_app.PageCompositionWorker(sources[:2], output,
                {path: get_pdf_source_state(path) for path in sources[:2]},
                [PageRef(sources[0], 0), PageRef(sources[1], 1)], [[1, "Test", 1]])
    cancelled, errors = [], []
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.failed.connect(errors.append)
    worker.progress.connect(lambda value, _text: worker.cancel() if value == 99 else None)
    worker.run()
    assert cancelled and not errors
    assert output.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".pdf_desktop_job_*"))
