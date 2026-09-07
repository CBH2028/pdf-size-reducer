"""Focused merge queue UI; PDF inspection is supplied by an isolated worker."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

import pymupdf as fitz
from PySide6.QtCore import QItemSelectionModel, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout,
)

from compressor import CompressionError, PDFSourceState, format_bytes, get_pdf_source_state
from qt_dispatch import GuiJobReceiver


@dataclass(frozen=True)
class MergeInputInfo:
    path: Path
    state: PDFSourceState | None = None
    pages: int = 0
    has_forms: bool = False
    error: str = ""


def inspect_merge_input(path):
    """Read basic metadata only; full merge validation still runs before saving."""
    path = Path(path).resolve()
    try:
        state = get_pdf_source_state(path)
        if state.size > 4 * 1024**3:
            raise CompressionError("单个文件不能超过 4 GiB")
        with fitz.open(path) as document:
            if document.needs_pass:
                raise CompressionError("文件受密码保护，请先解密")
            if not document.is_pdf or document.page_count == 0:
                raise CompressionError("不是有效的 PDF，或没有可合并的页面")
            pages, forms = document.page_count, bool(document.is_form_pdf)
        if get_pdf_source_state(path) != state:
            raise CompressionError("检查期间文件已变化，请重新检查")
        return MergeInputInfo(path, state, pages, forms)
    except Exception as exc:
        return MergeInputInfo(path, error=str(exc))


def natural_filename_key(path):
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"([0-9]+)", Path(path).name))


def suggest_merge_output(first, excluded=()):
    first = Path(first)
    excluded = {Path(path).resolve() for path in excluded}
    for number in range(1, 10_001):
        suffix = "" if number == 1 else f" ({number})"
        candidate = first.with_name(f"{first.stem}_合并{suffix}.pdf")
        if not candidate.exists() and candidate.resolve() not in excluded:
            return candidate
    raise CompressionError("无法自动生成空闲文件名，请手动选择保存位置。")


class MergeFileList(QListWidget):
    files_dropped = Signal(object)
    remove_requested = Signal()
    move_requested = Signal(int)
    drag_started = Signal()
    drag_finished = Signal()

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)
        self.empty_label = QLabel("把 PDF 拖到这里\n或点击上方“添加 PDF”\n\n列表顺序就是合并顺序；双击文件可打开查看", self.viewport())
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty_label.setStyleSheet("color: #71717A; background: transparent;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.viewport().rect())

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.files_dropped.emit([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            self.drag_started.emit()
            super().dropEvent(event)
            self.drag_finished.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.remove_requested.emit()
        elif event.modifiers() == Qt.KeyboardModifier.AltModifier and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.move_requested.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
        else:
            super().keyPressEvent(event)


class MergeDialog(QDialog):
    def __init__(self, parent, initial_paths, protected_paths, worker_factory):
        super().__init__(parent)
        self.worker_factory = worker_factory
        self.protected_paths = {Path(path).resolve() for path in protected_paths}
        self.thread = self.worker = None
        self._infos = {}
        self._sizes = {}
        self._history = []
        self._batch = []
        self._generation = 0
        self._closing = False
        self._finish_result = QDialog.DialogCode.Rejected
        self._output_custom = False
        self.setWindowTitle("合并 PDF · 整理顺序后一步保存")
        self.resize(900, 700)
        self.setMinimumSize(740, 580)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        title = QLabel("按你的顺序，合成一份 PDF")
        title.setProperty("title", True)
        root.addWidget(title)
        description = QLabel("拖入文件 → 调整顺序 → 选择保存方式。移除只影响列表，源文件不会被修改。")
        description.setWordWrap(True)
        root.addWidget(description)
        actions = QHBoxLayout()
        add = QPushButton("＋ 添加 PDF")
        add.clicked.connect(self._choose_files)
        self.remove_button = QPushButton("移出列表")
        self.remove_button.clicked.connect(self._remove_selected)
        self.up_button = QPushButton("上移")
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button = QPushButton("下移")
        self.down_button.clicked.connect(lambda: self._move_selected(1))
        self.sort_button = QPushButton("按文件名排序")
        self.sort_button.clicked.connect(self._sort_files)
        self.undo_button = QPushButton("撤销列表操作")
        self.undo_button.clicked.connect(self._undo)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self._clear)
        for button in (add, self.remove_button, self.up_button, self.down_button, self.sort_button, self.undo_button, self.clear_button):
            actions.addWidget(button)
        root.addLayout(actions)
        self.file_list = MergeFileList()
        self.file_list.files_dropped.connect(self.add_paths)
        self.file_list.remove_requested.connect(self._remove_selected)
        self.file_list.move_requested.connect(self._move_selected)
        self.file_list.drag_started.connect(self._remember)
        self.file_list.drag_finished.connect(self._drag_finished)
        self.file_list.model().rowsMoved.connect(self._list_changed)
        self.file_list.itemSelectionChanged.connect(self._update_controls)
        self.file_list.itemDoubleClicked.connect(self._open_item)
        root.addWidget(self.file_list, 1)
        summary_row = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setTextFormat(Qt.TextFormat.PlainText)
        summary_row.addWidget(self.count_label, 1)
        self.recheck_button = QPushButton("重新检查文件")
        self.recheck_button.clicked.connect(self._recheck)
        summary_row.addWidget(self.recheck_button)
        root.addLayout(summary_row)
        self.notice = QLabel("支持 Ctrl / Shift 多选；多选后可整组移动，Alt+↑/↓ 也可排序。")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.notice)
        root.addWidget(QLabel("保存为（可直接修改文件名或路径）"))
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("添加文件后自动生成不覆盖已有文件的名称")
        self.output_edit.textEdited.connect(self._output_edited)
        self.output_edit.textChanged.connect(self._update_controls)
        output_row.addWidget(self.output_edit, 1)
        output_button = QPushButton("选择位置…")
        output_button.clicked.connect(self._choose_output)
        output_row.addWidget(output_button)
        root.addLayout(output_row)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("合并完成后"))
        self.after_combo = QComboBox()
        self.after_combo.addItems(["只合并，保存 PDF", "继续压缩：先预览，再设置目标大小"])
        self.after_combo.setCurrentIndex(1)
        self.after_combo.currentIndexChanged.connect(self._update_controls)
        mode_row.addWidget(self.after_combo, 1)
        root.addLayout(mode_row)
        buttons = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        buttons.addWidget(self.status, 1)
        cancel = QPushButton("返回")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.merge_button = QPushButton("合并并继续压缩")
        self.merge_button.setObjectName("mergeConfirmButton")
        self.merge_button.clicked.connect(self._validate_and_accept)
        buttons.addWidget(self.merge_button)
        root.addLayout(buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.merge_button.setDefault(True)
        self.setStyleSheet("""
            QListWidget { background: white; border: 1px solid #E4E5EC; border-radius: 12px; padding: 6px; font-size: 12px; }
            QListWidget::item { padding: 9px 8px; border-radius: 7px; }
            QListWidget::item:selected { background: #ECEAFF; color: #18181B; }
            QPushButton#mergeConfirmButton { color: white; background: #635BFF; border: none; padding: 9px 18px; font-weight: 700; }
            QPushButton#mergeConfirmButton:disabled { background: #CBC9E2; color: white; }
        """)
        self.add_paths(initial_paths or [])
        self._history.clear()
        self._update_controls()

    def source_paths(self):
        return [Path(self.file_list.item(row).data(Qt.ItemDataRole.UserRole)) for row in range(self.file_list.count())]

    def source_states(self):
        return {path: self._infos[path].state for path in self.source_paths() if path in self._infos and self._infos[path].state is not None}

    def output_path(self):
        path = Path(self.output_edit.text().strip()).expanduser()
        if not path.is_absolute() and self.source_paths():
            path = self.source_paths()[0].parent / path
        return path.with_suffix(".pdf") if not path.suffix else path

    def load_after_merge(self):
        return self.after_combo.currentIndex() == 1

    def _remember(self):
        self._history.append(tuple(self.source_paths()))
        self._history = self._history[-40:]

    def _append_item(self, path):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        item.setToolTip(str(path))
        self.file_list.addItem(item)
        try:
            self._sizes[path] = path.stat().st_size
        except OSError:
            self._sizes[path] = 0

    def add_paths(self, paths):
        existing = {os.path.normcase(str(path)) for path in self.source_paths()}
        added, duplicates, invalid, overflow = [], 0, 0, 0
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
                key = os.path.normcase(str(path))
                if key in existing:
                    duplicates += 1
                elif not path.is_file() or path.suffix.lower() != ".pdf":
                    invalid += 1
                elif len(existing) >= 100:
                    overflow += 1
                else:
                    added.append(path)
                    existing.add(key)
            except (OSError, ValueError):
                invalid += 1
        if added:
            self._remember()
            for path in added:
                self._infos.pop(path, None)
                self._append_item(path)
        if added or duplicates or invalid or overflow:
            self.notice.setText(f"已添加 {len(added)} 个文件；跳过 {duplicates} 个重复、{invalid} 个非 PDF/不存在文件" + (f"、{overflow} 个超出上限的文件（最多 100 个）" if overflow else "") + "。")
        self._list_changed()
        QTimer.singleShot(0, self._start_inspection)

    def _choose_files(self):
        initial = str(self.source_paths()[-1].parent) if self.source_paths() else ""
        paths, _ = QFileDialog.getOpenFileNames(self, "添加 PDF（Ctrl / Shift 可多选）", initial, "PDF 文件 (*.pdf)")
        if paths:
            self.add_paths(paths)

    def _remove_selected(self):
        selected = self.file_list.selectedItems()
        if selected:
            self._remember()
            for item in selected:
                self.file_list.takeItem(self.file_list.row(item))
            self.notice.setText(f"已从列表移出 {len(selected)} 个文件；原文件未删除，可撤销。")
            self._list_changed()

    def _clear(self):
        if self.file_list.count():
            self._remember()
            self.file_list.clear()
            self.notice.setText("列表已清空；原文件未删除，可撤销。")
            self._list_changed()

    def _move_selected(self, offset):
        selected = self.file_list.selectedItems()
        if not selected or offset not in (-1, 1):
            return
        before = tuple(self.source_paths())
        current = self.file_list.currentItem()
        ordered = sorted(selected, key=self.file_list.row, reverse=offset > 0)
        self._remember()
        self.file_list.blockSignals(True)
        for item in ordered:
            row = self.file_list.row(item)
            target = row + offset
            if 0 <= target < self.file_list.count() and self.file_list.item(target) not in selected:
                self.file_list.takeItem(row)
                self.file_list.insertItem(target, item)
        for item in selected:
            item.setSelected(True)
        if current is not None:
            self.file_list.setCurrentItem(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.file_list.blockSignals(False)
        if tuple(self.source_paths()) == before:
            self._history.pop()
        self._list_changed()

    def _replace_paths(self, paths):
        previous = set(self.source_paths())
        self.file_list.clear()
        for path in paths:
            if path not in previous:
                self._infos.pop(path, None)
            self._append_item(path)
        self._list_changed()
        QTimer.singleShot(0, self._start_inspection)

    def _sort_files(self):
        before = self.source_paths()
        after = sorted(before, key=natural_filename_key)
        if after != before:
            self._remember()
            self._replace_paths(after)

    def _undo(self):
        if self._history:
            self._replace_paths(self._history.pop())
            self.notice.setText("已撤销上一次列表操作；源文件未作修改。")

    def _drag_finished(self):
        if self._history and self._history[-1] == tuple(self.source_paths()):
            self._history.pop()
        self._list_changed()

    def _list_changed(self, *_args):
        paths = self.source_paths()
        if not self._output_custom:
            try:
                output = suggest_merge_output(paths[0], [*paths, *self.protected_paths]) if paths else ""
                self.output_edit.setText(str(output))
            except CompressionError as exc:
                self.notice.setText(str(exc))
        self._update_controls()

    def _output_edited(self, _text):
        self._output_custom = True

    def _choose_output(self):
        selected, _ = QFileDialog.getSaveFileName(self, "保存合并后的 PDF", str(self.output_path()) if self.output_edit.text() else "", "PDF 文件 (*.pdf)", options=QFileDialog.Option.DontConfirmOverwrite)
        if selected:
            self._output_custom = True
            path = Path(selected)
            self.output_edit.setText(str(path.with_suffix(".pdf") if not path.suffix else path))

    def _open_item(self, item):
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        if not path.is_file() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.notice.setText("无法打开此文件，请检查文件是否存在及默认 PDF 阅读器设置。")

    def _start_inspection(self):
        if self._closing or self.thread is not None:
            return
        self._batch = [path for path in self.source_paths() if path not in self._infos]
        if not self._batch:
            self._update_controls()
            return
        generation = self._generation
        self.thread = QThread(self)
        self.worker = self.worker_factory(list(self._batch))
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.receiver = GuiJobReceiver(
            self, lambda info: self._info_ready(generation, info),
            lambda message: self._inspection_failed(generation, message),
            self._inspection_finished,
        )
        self.worker.item_ready.connect(self.receiver.item, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self.receiver.error, Qt.ConnectionType.QueuedConnection)
        for signal in (self.worker.completed, self.worker.failed, self.worker.cancelled):
            signal.connect(self.thread.quit)
            signal.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.receiver.finished, Qt.ConnectionType.QueuedConnection)
        self.thread.finished.connect(self.thread.deleteLater)
        self._update_controls()
        self.thread.start()

    def _info_ready(self, generation, info):
        if not self._closing and generation == self._generation and info.path in self.source_paths():
            self._infos[info.path] = info
            if info.state:
                self._sizes[info.path] = info.state.size
            self._update_controls()

    def _inspection_failed(self, generation, message):
        if not self._closing and generation == self._generation:
            for path in self._batch:
                if path not in self._infos:
                    self._infos[path] = MergeInputInfo(path, error=f"检查失败：{message}")
            self._update_controls()

    def _inspection_finished(self):
        self.thread = self.worker = None
        if self._closing:
            super().done(self._finish_result)
        else:
            self._update_controls()
            QTimer.singleShot(0, self._start_inspection)

    def _recheck(self):
        self._generation += 1
        self._infos.clear()
        if self.worker:
            self.worker.cancel()
        self.notice.setText("正在重新读取文件状态和页数…")
        self._update_controls()
        QTimer.singleShot(0, self._start_inspection)

    def _update_controls(self, *_args):
        paths = self.source_paths()
        ready = [self._infos[path] for path in paths if path in self._infos and not self._infos[path].error]
        failed = [path for path in paths if path in self._infos and self._infos[path].error]
        pending = len(paths) - len(ready) - len(failed)
        size = sum(self._sizes.get(path, 0) for path in paths)
        for row, path in enumerate(paths):
            info = self._infos.get(path)
            detail = "正在读取页数…" if info is None else (info.error or f"{info.pages} 页" + (" · 含表单，使用兼容合并" if info.has_forms else ""))
            item = self.file_list.item(row)
            item.setText(f"{row + 1:02d}  {path.name}\n{detail} · {format_bytes(self._sizes.get(path, 0))} · {path.parent}")
            item.setForeground(QColor("#C03535" if info and info.error else "#18181B"))
        self.file_list.empty_label.setVisible(not paths)
        count_text = f"{len(paths)} 个文件 · {sum(info.pages for info in ready)} 页 · {format_bytes(size)}"
        if pending:
            count_text += f" · 待检查 {pending} 个"
        if failed:
            count_text += f" · {len(failed)} 个不可用"
        self.count_label.setText(count_text)
        rows = {self.file_list.row(item) for item in self.file_list.selectedItems()}
        self.remove_button.setEnabled(bool(rows))
        self.up_button.setEnabled(any(row > 0 and row - 1 not in rows for row in rows))
        self.down_button.setEnabled(any(row < len(paths) - 1 and row + 1 not in rows for row in rows))
        self.sort_button.setEnabled(len(paths) > 1)
        self.clear_button.setEnabled(bool(paths))
        self.undo_button.setEnabled(bool(self._history))
        self.recheck_button.setEnabled(bool(paths))
        self.merge_button.setText("合并并继续压缩" if self.load_after_merge() else "合并并保存")
        enabled = len(paths) >= 2 and not pending and not failed and self.thread is None and size <= 16 * 1024**3
        self.merge_button.setEnabled(enabled and bool(self.output_edit.text().strip()) and not self._closing)
        if len(paths) < 2:
            message = "再添加文件即可合并（至少两个）。"
        elif failed:
            message = "请移出不可用文件，或修复后点击“重新检查文件”。"
        elif size > 16 * 1024**3:
            message = "总输入不能超过 16 GiB，请分批合并。"
        elif pending or self.thread is not None:
            message = "正在后台读取页数，你可以继续添加或排序。"
        else:
            message = f"将按 01 → {len(paths):02d} 的顺序合并；源 PDF 保持不变。"
        self.status.setText(message)

    def _validate_and_accept(self):
        paths = self.source_paths()
        if len(paths) < 2 or self.thread is not None or any(path not in self._infos or self._infos[path].error for path in paths):
            self.notice.setText("请至少添加两个可用的 PDF，并等待后台检查完成。")
            return
        if sum(self._infos[path].state.size for path in paths) > 16 * 1024**3:
            return
        try:
            for path in paths:
                if get_pdf_source_state(path) != self._infos[path].state:
                    raise CompressionError("待合并文件已变化，请点击“重新检查文件”，确认页数后再合并。")
            if not self.output_edit.text().strip():
                raise CompressionError("请选择合并结果的保存位置。")
            destination = self.output_path().resolve()
            if destination.suffix.lower() != ".pdf":
                raise CompressionError("合并结果必须保存为 PDF 文件。")
            if destination in {*paths, *self.protected_paths}:
                raise CompressionError("合并结果不能覆盖源 PDF 或压缩工作区正在使用的 PDF。")
            if destination.exists() and not destination.is_file():
                raise CompressionError("保存位置是一个文件夹，请选择 PDF 文件名。")
        except (CompressionError, OSError, ValueError) as exc:
            self.notice.setText(str(exc))
            return
        if destination.exists():
            answer = QMessageBox.question(self, "确认替换已有结果", f"已存在：\n{destination}\n\n是否替换？选择“否”将保留当前列表，方便修改保存位置。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.notice.setText("未覆盖任何文件；请修改保存名称或位置后继续。")
                return
        self.output_edit.setText(str(destination))
        self.accept()

    def done(self, result):
        self._closing = True
        self._finish_result = result
        if self.worker is not None:
            self.worker.cancel()
            self.setEnabled(False)
            self.status.setText("正在结束文件检查…")
        else:
            super().done(result)

    def accept(self):
        self.done(QDialog.DialogCode.Accepted)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self.reject()
        if self.thread is not None:
            event.ignore()
        else:
            event.accept()


def smoke_test(dialog_factory, merge_worker_factory, app_style, screenshot=None):
    """Check the actual merge queue and guarded writer in source/frozen builds."""
    import tempfile
    import time
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(app_style)
    if os.name == "nt" and not QFontDatabase.families():
        # Some headless Windows sessions don't populate Qt's font database.
        for filename in ("segoeui.ttf", "msyh.ttc"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Microsoft YaHei", 9))

    def wait_until(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            if time.monotonic() >= deadline:
                raise RuntimeError("Merge queue smoke test timed out")
            time.sleep(0.01)
        app.processEvents()

    with tempfile.TemporaryDirectory(prefix="pdf-merge-ui-check-") as directory:
        workspace = Path(directory).resolve()
        sources = []
        for number in (10, 2, 1):
            path = workspace / f"Report {number}.pdf"
            with fitz.open() as document:
                for page in range(number):
                    document.new_page().insert_text((40, 40), f"Report {number} / Page {page + 1}")
                document.save(path)
            sources.append(path)
        dialog = dialog_factory(None, sources)
        dialog.show()
        try:
            wait_until(lambda: dialog.thread is None and len(dialog._infos) == 3)
            assert dialog.merge_button.isEnabled()
            assert sum(info.pages for info in dialog._infos.values()) == 13
            dialog._sort_files()
            assert dialog.source_paths() == list(reversed(sources)), (dialog.source_paths(), sources)
            dialog.file_list.clearSelection()
            for row in (1, 2):
                dialog.file_list.item(row).setSelected(True)
            dialog._move_selected(-1)
            assert dialog.source_paths() == [sources[1], sources[0], sources[2]]
            dialog._undo()
            assert dialog.source_paths() == list(reversed(sources))
            dialog.output_edit.setText(str(workspace / "Combined report.pdf"))
            if screenshot:
                app.processEvents()
                assert dialog.grab().save(str(screenshot))
            dialog._validate_and_accept()
            assert dialog.result() == QDialog.DialogCode.Accepted
            assert dialog.load_after_merge()
            results, errors = [], []
            worker = merge_worker_factory(dialog.source_paths(), dialog.output_path(), dialog.source_states())
            worker.completed.connect(results.append)
            worker.failed.connect(errors.append)
            worker.cancelled.connect(lambda: errors.append("Unexpected cancellation"))
            worker.run()
            assert not errors and len(results) == 1, errors
            assert results[0].native_worker_used
            with fitz.open(dialog.output_path()) as merged:
                assert merged.page_count == 13
                assert "Report 1 /" in merged[0].get_text()
                assert "Report 2 /" in merged[1].get_text()
                assert "Report 10 /" in merged[3].get_text()
        finally:
            dialog.reject()
            wait_until(lambda: dialog.thread is None)
            dialog.deleteLater()
            app.processEvents()
    return 0
