from __future__ import annotations

from copy import deepcopy
import json

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QPushButton

from compressor import CompressionError
from merge_ui import inspect_merge_input
from pdf_composer import PageRef
from test_composer_ui import app, sources, pdf, wait


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
        dialog.show()
        app.processEvents()
        return dialog

    yield create
    for dialog in dialogs:
        dialog.close()
        wait(app, lambda: not dialog.jobs)
        dialog.deleteLater()
    app.processEvents()


def page_payload(dialog, source, indices):
    return {"session": dialog.session, "kind": "pages", "pages": [[str(source), i] for i in indices]}


def mime_payload(payload):
    from simple_composer_ui import PAGE_MIME
    mime = QMimeData()
    mime.setData(PAGE_MIME, json.dumps(payload).encode())
    return mime


def drop(view, mime, point, actions=Qt.DropAction.CopyAction | Qt.DropAction.MoveAction):
    # Dispatch through Qt and its real viewport; calling dropEvent directly masks
    # a disabled drop receiver, which caused the v3.13.0 regression.
    for event_type in (QDragEnterEvent, QDragMoveEvent, QDropEvent):
        position = QPointF(point) if event_type is QDropEvent else point
        event = event_type(position, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), event)
        if not event.isAccepted():
            return False
    return True


def test_default_has_only_simple_actions(sources, factory):
    dialog = factory(sources)
    assert not dialog.is_advanced()
    assert not dialog.tree.isVisible()
    assert not dialog.output_edit.isVisible()
    assert not dialog.quick_button.isVisible()
    buttons = [b.text() for b in dialog.findChildren(QPushButton) if b.isVisible()]
    assert set(buttons) == {"添加 PDF", "保存 PDF", "高级组合"}
    assert dialog.simple.output.pages.count == 3
    assert dialog.simple.source.pages.source == sources[1]
    assert dialog.simple.documents.count() == 3


@pytest.mark.parametrize("position", [0, 1, 3])
def test_insert_page_at_flat_position(sources, factory, position):
    dialog = factory(sources)
    expected = dialog.plan.pages()
    expected[position:position] = [PageRef(sources[1], 2), PageRef(sources[1], 0)]
    dialog.simple.apply_drop(page_payload(dialog, sources[1], [2, 0]), position)
    assert dialog.plan.pages() == expected
    dialog.undo()
    assert len(dialog.plan.pages()) == 3
    dialog.redo()
    assert dialog.plan.pages() == expected


def test_drag_event_inserts_and_shows_gap_marker(app, sources, factory):
    dialog = factory(sources)
    view = dialog.simple.output
    rect = view.visualRect(view.pages.index(1))
    point = QPoint(rect.left() + 10, rect.center().y())
    mime = mime_payload(page_payload(dialog, sources[1], [2]))
    enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(view.viewport(), enter)
    assert enter.isAccepted()
    event = QDragMoveEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(view.viewport(), event)
    assert event.isAccepted() and view.drop_at == 1
    assert drop(view, mime, point)
    assert dialog.plan.pages()[1] == PageRef(sources[1], 2)
    assert view.drop_at is None and not view.scroll_timer.isActive()


def test_viewports_accept_drops_after_mode_switch_and_resize(app, sources, factory):
    dialog = factory(sources)
    for advanced in (False, True, False):
        dialog.set_advanced(advanced)
        dialog.resize(1100 if advanced else 920, 740)
        app.processEvents()
        assert dialog.simple.output.viewport().acceptDrops()
        assert dialog.simple.source.viewport().acceptDrops()


@pytest.mark.parametrize("output,rows", [(False, [0]), (False, [0, 2]), (True, [0]), (True, [0, 2])])
def test_mouse_press_to_drop_dispatch_updates_plan(app, sources, factory, monkeypatch, output, rows):
    """Only the OS drag loop is replaced; mouse initiation and Qt dispatch are real."""
    import simple_composer_ui
    from PySide6.QtCore import QItemSelectionModel
    dialog = factory(sources)
    source = dialog.simple.output if output else dialog.simple.source
    target = dialog.simple.output
    before = dialog.plan.pages()
    before_ids = [uid for uid, _ref in target.pages.entries]
    for row in rows:
        source.selectionModel().select(source.pages.index(row), QItemSelectionModel.SelectionFlag.Select)
    point = source.visualRect(source.pages.index(rows[0])).center()
    destination = QPoint(10, target.viewport().height() - 10)
    captured = []

    class DragLoop:
        def __init__(self, parent): assert parent is source
        def setMimeData(self, mime): self.mime = mime
        def setPixmap(self, pixmap): pass
        def exec(self, action):
            assert action == (Qt.DropAction.MoveAction if output else Qt.DropAction.CopyAction)
            captured.append(drop(target, self.mime, destination, action))
            return action if captured[-1] else Qt.DropAction.IgnoreAction

    monkeypatch.setattr(simple_composer_ui, "QDrag", DragLoop)

    def mouse(kind, at, button, buttons):
        event = QMouseEvent(kind, QPointF(at), QPointF(source.viewport().mapToGlobal(at)),
                            button, buttons, Qt.KeyboardModifier.NoModifier)
        app.sendEvent(source.viewport(), event)

    mouse(QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    mouse(QEvent.Type.MouseMove, point + QPoint(2, 0), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    mouse(QEvent.Type.MouseMove, point + QPoint(app.startDragDistance() + 8, 0), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    mouse(QEvent.Type.MouseButtonRelease, point, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert captured == [True]
    if output:
        assert [uid for uid, _ref in target.pages.entries] == [uid for i, uid in enumerate(before_ids) if i not in rows] + [before_ids[i] for i in rows]
    else:
        assert dialog.plan.pages() == before + [PageRef(sources[1], i) for i in rows]
    dialog.undo()
    assert dialog.plan.pages() == before
    assert [uid for uid, _ref in target.pages.entries] == before_ids


def test_drag_leave_clears_marker_without_changing_plan(app, sources, factory):
    dialog = factory(sources)
    view = dialog.simple.output
    before = dialog.plan.pages()
    point = view.visualRect(view.pages.index(1)).center()
    mime = mime_payload(page_payload(dialog, sources[1], [0]))
    for event_type in (QDragEnterEvent, QDragMoveEvent):
        event = event_type(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        app.sendEvent(view.viewport(), event)
        assert event.isAccepted()
    assert view.drop_at is not None and view.scroll_timer.isActive()
    app.sendEvent(view.viewport(), QDragLeaveEvent())
    assert view.drop_at is None and not view.scroll_timer.isActive()
    assert dialog.plan.pages() == before


def test_pages_dropped_back_on_material_pane_are_rejected(sources, factory):
    dialog = factory(sources)
    before = dialog.plan.pages()
    assert not drop(dialog.simple.source, mime_payload(page_payload(dialog, sources[1], [0])), QPoint(20, 20))
    assert dialog.plan.pages() == before


def test_move_duplicate_page_by_stable_identity(sources, factory):
    dialog = factory(sources)
    ui = dialog.simple
    ui.apply_drop(page_payload(dialog, sources[0], [0]), 3)
    entries = list(ui.output.pages.entries)
    ui.apply_drop({"session": dialog.session, "kind": "nodes", "nodes": [entries[-1][0]]}, 1)
    assert [uid for uid, _ref in ui.output.pages.entries] == [entries[0][0], entries[3][0], entries[1][0], entries[2][0]]
    dialog.undo()
    assert ui.output.pages.entries == entries


def test_move_multiple_pages_and_nested_groups(sources, factory):
    dialog = factory(sources)
    group = dialog.plan.root.children[0]
    dialog.insert_pages([PageRef(sources[1], 0), PageRef(sources[1], 1)], group="nested", target=(group.uid, 1))
    entries = list(dialog.simple.output.pages.entries)
    dialog.simple.apply_drop({"session": dialog.session, "kind": "nodes", "nodes": [entries[1][0], entries[3][0]]}, 5)
    assert dialog.plan.pages() == [entries[i][1] for i in [0, 2, 4, 1, 3]]
    dialog.undo()
    assert dialog.simple.output.pages.entries == entries


def test_mode_switch_preserves_tree_history_and_destination(sources, factory):
    dialog = factory(sources)
    group = dialog.plan.root.children[0]
    dialog.insert_pages([PageRef(sources[1], 0)], group="bookmarks", target=(group.uid, 1))
    dialog.output_edit.setText("custom.pdf")
    dialog.after_combo.setCurrentIndex(0)
    before, history = deepcopy(dialog.plan.root), deepcopy(dialog.plan.history)
    for enabled in [True, False, True, False]:
        dialog.set_advanced(enabled)
        assert dialog.quick_button.isVisible() == enabled
    assert dialog.plan.root == before and dialog.plan.history == history
    assert dialog.output_edit.text() == "custom.pdf" and not dialog.load_after_merge()


def test_cancel_save_preserves_workspace(sources, factory, monkeypatch):
    dialog = factory(sources)
    before = deepcopy(dialog.plan.root)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    dialog.simple.save_button.click()
    assert dialog.plan.root == before and not dialog._closing


def test_save_uses_shared_source_protection(sources, factory, monkeypatch):
    dialog = factory(sources)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(sources[1]), ""))
    dialog.simple.save()
    assert not dialog._closing
    assert "不能覆盖" in dialog.simple.notice.text()


def test_save_accepts_new_path(sources, factory, monkeypatch, tmp_path):
    dialog = factory(sources)
    target = tmp_path / "new.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    dialog.simple.save()
    assert dialog._closing and dialog._result == QDialog.DialogCode.Accepted
    assert dialog.output_path() == target


def test_file_drops_right_are_materials_left_first_is_base(app, sources, factory):
    dialog = factory()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in sources])
    assert drop(dialog.simple.source, mime, QPoint(20, 20))
    wait(app, lambda: len(dialog.infos) == 3)
    assert not dialog.plan.pages() and len(dialog.paths) == 3
    assert drop(dialog.simple.output, mime, QPoint(20, 20))
    assert dialog.plan.pages() == [PageRef(sources[0], i) for i in range(3)]
    assert drop(dialog.simple.output, mime, QPoint(20, 20))
    assert len(dialog.plan.pages()) == 3  # no implicit whole-file concatenation


def test_continuous_large_source_lazy_thumbnails(app, tmp_path, factory):
    path = pdf(tmp_path / "large.pdf", 110)
    dialog = factory([path])
    view = dialog.simple.source
    assert view.pages.rowCount() == 110
    assert 0 < len(view.visible_refs()) < 24
    assert len(dialog.wanted_thumbnails()) < 40
    wait(app, lambda: view.verticalScrollBar().maximum() > 0)
    view.scrollTo(view.pages.index(109))
    app.processEvents()
    assert PageRef(path, 109) in view.visible_refs()
    assert len(view.visible_refs()) < 24
    wait(app, lambda: (PageRef(path, 109), dialog.infos[path].state) in dialog.cache)


def test_drag_auto_scroll(app, tmp_path, factory):
    path = pdf(tmp_path / "many.pdf", 90)
    dialog = factory([path])
    view = dialog.simple.output
    wait(app, lambda: view.verticalScrollBar().maximum() > 0)
    view.drag_point = QPoint(20, view.viewport().height() - 4)
    view.auto_scroll()
    assert view.verticalScrollBar().value() > 0
    view.clear_drop()


@pytest.mark.parametrize("payload", [
    {"kind": "pages", "pages": [["unknown.pdf", 0]]},
    {"kind": "pages", "pages": [["unknown.pdf", True]]},
    {"kind": "pages", "pages": [[]]},
    {"kind": "pages", "pages": []},
    {"kind": "nodes", "nodes": ["root"]},
    {"kind": "nodes", "nodes": [[]]},
    {"kind": "documents", "paths": []},
])
def test_malformed_drop_does_not_change_plan(sources, factory, payload):
    dialog = factory(sources)
    before = deepcopy(dialog.plan.root)
    with pytest.raises(CompressionError):
        dialog.simple.apply_drop({"session": dialog.session, **payload}, 0)
    assert dialog.plan.root == before


def test_foreign_session_rejected(sources, factory):
    dialog = factory(sources)
    payload = page_payload(dialog, sources[1], [0])
    payload["session"] = "another-session"
    assert not drop(dialog.simple.output, mime_payload(payload), QPoint(10, 10))


def test_source_selection_survives_metadata_arrival(sources, factory):
    dialog = factory(sources)
    dialog.simple.documents.setCurrentIndex(2)
    dialog.info_ready(dialog._generation, inspect_merge_input(sources[0]))
    assert dialog.simple.source.pages.source == sources[2]


@pytest.mark.parametrize("accepted", [False, True])
def test_advanced_whole_merge_routes_or_cancel_preserves(sources, factory, accepted):
    dialog = factory(sources)
    before = deepcopy(dialog.plan.root)
    requested = []

    class QuickDialog:
        def __init__(self, parent, initial_paths, protected_paths):
            assert initial_paths == sources and set(sources) <= set(protected_paths)
        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
        def source_paths(self): return sources
        def output_path(self): return sources[0].with_name("whole.pdf")
        def source_states(self): return dialog.source_states()
        def load_after_merge(self): return False
        def deleteLater(self): requested.append(True)

    dialog.quick_merge_factory = QuickDialog
    dialog.set_advanced(True)
    dialog.quick_button.click()
    assert requested and dialog.plan.root == before
    assert bool(dialog.quick_merge_request) == accepted
    assert dialog._closing == accepted


def test_minimum_simple_window_actions_visible(app, sources, factory):
    dialog = factory(sources)
    dialog.resize(900, 620)
    app.processEvents()
    for widget in (dialog.simple.save_button, dialog.simple.add_button, dialog.mode_button):
        top_left = widget.mapTo(dialog, QPoint(0, 0))
        bottom_right = widget.mapTo(dialog, widget.rect().bottomRight())
        assert dialog.rect().contains(top_left) and dialog.rect().contains(bottom_right)


def test_hover_remove_and_keyboard_undo(app, sources, factory):
    from PySide6.QtTest import QTest
    from simple_composer_ui import PageDelegate
    dialog = factory(sources)
    view = dialog.simple.output
    before = dialog.plan.pages()
    rect = PageDelegate.remove_rect(view.visualRect(view.pages.index(1)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert dialog.plan.pages() == [before[0], before[2]]
    QTest.keyClick(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert dialog.plan.pages() == before
    view.setCurrentIndex(view.pages.index(0))
    QTest.keyClick(view, Qt.Key.Key_Delete)
    assert dialog.plan.pages() == before[1:]


def test_undo_redo_while_simple_button_has_focus(app, sources, factory):
    from PySide6.QtTest import QTest
    dialog = factory(sources)
    dialog.simple.apply_drop(page_payload(dialog, sources[1], [0]), 3)
    dialog.simple.add_button.setFocus()
    app.processEvents()
    QTest.keyClick(dialog.simple.add_button, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(dialog.plan.pages()) == 3
    QTest.keyClick(dialog.simple.add_button, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert len(dialog.plan.pages()) == 4


@pytest.mark.parametrize("output", [False, True])
def test_drag_source_emits_copy_output_emits_move(sources, factory, monkeypatch, output):
    import simple_composer_ui
    from PySide6.QtCore import QItemSelectionModel
    dialog = factory(sources)
    view = dialog.simple.output if output else dialog.simple.source
    for row in [0, 2]:
        view.selectionModel().select(view.pages.index(row), QItemSelectionModel.SelectionFlag.Select)
    captured = {}

    class Drag:
        def __init__(self, parent): pass
        def setMimeData(self, mime): captured["payload"] = json.loads(bytes(mime.data(simple_composer_ui.PAGE_MIME)))
        def setPixmap(self, pixmap): pass
        def exec(self, action): captured["action"] = action

    monkeypatch.setattr(simple_composer_ui, "QDrag", Drag)
    view.startDrag(None)
    assert captured["action"] == (Qt.DropAction.MoveAction if output else Qt.DropAction.CopyAction)
    before = len(dialog.plan.pages())
    dialog.simple.apply_drop(captured["payload"], before)
    assert len(dialog.plan.pages()) == before + (0 if output else 2)


def test_main_window_routes_advanced_whole_merge(app, sources, monkeypatch):
    import qt_app
    captured = []

    class Composer:
        def __init__(self, *args):
            self.quick_merge_request = (sources, sources[0].with_name("whole.pdf"), {}, False)
        def exec(self): return QDialog.DialogCode.Accepted
        def deleteLater(self): pass

    window = qt_app.MainWindow()
    try:
        monkeypatch.setattr(qt_app, "PDFComposerDialog", Composer)
        monkeypatch.setattr(window, "_start_merge", lambda *args: captured.append(args))
        window.open_merge_dialog(sources)
        assert len(captured) == 1 and len(captured[0]) == 3
        assert captured[0][0] == sources and not window.merge_load_after
        assert not hasattr(window, "quick_merge_button")
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
