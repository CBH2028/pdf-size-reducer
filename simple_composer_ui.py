"""Simple, virtualized drag-and-drop page composition over the shared plan."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QDrag, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListView,
    QMenu, QPushButton, QSplitter, QStyledItemDelegate, QStyle, QVBoxLayout, QWidget,
)

from compressor import CompressionError
from pdf_composer import PageRef, walk

PAGE_MIME = "application/x-pdf-size-reducer-composition"


class PageModel(QAbstractListModel):
    def __init__(self, owner, output):
        super().__init__(owner)
        self.owner, self.output = owner, output
        self.entries, self.source, self.count = [], None, 0

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.count

    def ref(self, row):
        if not 0 <= row < self.count:
            return None
        return self.entries[row][1] if self.output else PageRef(self.source, row)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        ref = self.ref(index.row()) if index.isValid() else None
        if ref is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return f"第 {index.row() + 1} 页"
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{ref.source.name} · 原第 {ref.index + 1} 页\n拖动插入 / 排序；双击放大" + ("；Delete 移出成品" if self.output else "")
        if role == Qt.ItemDataRole.DecorationRole:
            return self.owner.thumbnail_icon(ref)

    def flags(self, index):
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled) if index.isValid() else Qt.ItemFlag.NoItemFlags

    def reset_output(self):
        self.beginResetModel()
        self.entries = [(node.uid, node.page) for node in walk(self.owner.plan.root) if node.page]
        self.count = len(self.entries)
        self.endResetModel()

    def set_source(self, source):
        info = self.owner.infos.get(source)
        count = info.pages if info and not info.error else 0
        if (source, count) == (self.source, self.count):
            return
        self.beginResetModel()
        self.source, self.count = source, count
        self.endResetModel()


class PageDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(150, 218)

    @staticmethod
    def remove_rect(rect):
        return QRect(rect.right() - 26, rect.top() + 5, 22, 22)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(5, 5, -5, -5)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.setPen(QPen(QColor("#635BFF" if selected else "#DFE3EC"), 2 if selected else 1))
        painter.setBrush(QColor("#F0EEFF" if selected else "#FFFFFF"))
        painter.drawRoundedRect(rect, 9, 9)
        image_rect = rect.adjusted(10, 10, -10, -48)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon and not icon.isNull():
            icon.paint(painter, image_rect, Qt.AlignmentFlag.AlignCenter)
        else:
            painter.setPen(QColor("#858A9A"))
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, "预览加载中…")
        painter.setPen(QColor("#252A3D"))
        painter.drawText(QRect(rect.left() + 8, rect.bottom() - 43, rect.width() - 16, 22), Qt.AlignmentFlag.AlignCenter, index.data())
        ref = index.model().ref(index.row())
        provenance = f"{ref.source.name} · {ref.index + 1}"
        painter.setPen(QColor("#777D90"))
        painter.drawText(QRect(rect.left() + 8, rect.bottom() - 22, rect.width() - 16, 18), Qt.AlignmentFlag.AlignCenter,
                         option.fontMetrics.elidedText(provenance, Qt.TextElideMode.ElideMiddle, rect.width() - 16))
        if index.model().output and hovered:
            close = self.remove_rect(option.rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#EDEBF8"))
            painter.drawEllipse(close)
            painter.setPen(QColor("#514D71"))
            painter.drawText(close, Qt.AlignmentFlag.AlignCenter, "×")
        painter.restore()


class DragPageView(QListView):
    """Copy source pages or move stable leaf IDs, never mutate source documents."""
    def __init__(self, workspace, output):
        super().__init__(workspace)
        self.workspace, self.owner, self.output = workspace, workspace.owner, output
        self.pages = PageModel(self.owner, output)
        self.setModel(self.pages)
        self.setItemDelegate(PageDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setGridSize(QSize(150, 218))
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(64)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        # The viewport receives the actual drag/drop events. In the PySide
        # constructor the outer flag can be true while this one stays false.
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setMouseTracking(True)
        self.setStyleSheet("QListView { background: #F6F7FB; border: 1px solid #E4E6EF; border-radius: 12px; }")
        self.drop_at, self.drag_point = None, None
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setInterval(45)
        self.scroll_timer.timeout.connect(self.auto_scroll)
        self.verticalScrollBar().valueChanged.connect(self.owner.schedule_thumbnails)
        self.doubleClicked.connect(lambda index: self.owner.open_preview(self.pages.ref(index.row())))

    def visible_rows(self):
        # Sample the uniform grid, not all document pages. At most one viewport.
        rows = set()
        viewport = self.viewport().rect()
        for y in range(0, viewport.height() + 109, 109):
            for x in range(0, viewport.width() + 75, 75):
                index = self.indexAt(QPoint(min(x, viewport.right()), min(y, viewport.bottom())))
                if index.isValid():
                    rows.add(index.row())
        return sorted(rows)

    def visible_refs(self):
        return [self.pages.ref(row) for row in self.visible_rows()]

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.owner.schedule_thumbnails()

    def startDrag(self, _actions):
        rows = sorted(index.row() for index in self.selectedIndexes())
        if not rows:
            return
        payload = {"session": self.owner.session}
        if self.output:
            payload.update(kind="nodes", nodes=[self.pages.entries[row][0] for row in rows])
        else:
            payload.update(kind="pages", pages=[[str(self.pages.ref(row).source), row] for row in rows])
        mime = QMimeData()
        mime.setData(PAGE_MIME, json.dumps(payload).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        icon = self.owner.thumbnail_icon(self.pages.ref(rows[0]))
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(90, 120))
        drag.exec(Qt.DropAction.MoveAction if self.output else Qt.DropAction.CopyAction)
        self.clear_drop()

    def payload(self, mime):
        if not self.output or not mime.hasFormat(PAGE_MIME) or mime.data(PAGE_MIME).size() > 4 * 1024**2:
            return None
        try:
            payload = json.loads(bytes(mime.data(PAGE_MIME)))
            if isinstance(payload, dict) and payload.get("session") == self.owner.session and payload.get("kind") in ("pages", "nodes"):
                return payload
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    def file_paths(mime):
        return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() == ".pdf"]

    def dragEnterEvent(self, event):
        if self.payload(event.mimeData()) or self.file_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def insertion_index(self, point):
        rows = self.visible_rows()
        if not rows:
            return self.pages.count
        for row in rows:
            rect = self.visualRect(self.pages.index(row))
            if point.y() < rect.top():
                return row
            if point.y() <= rect.bottom() and point.x() < rect.center().x():
                return row
        return min(rows[-1] + 1, self.pages.count)

    def dragMoveEvent(self, event):
        if self.payload(event.mimeData()):
            self.drag_point = event.position().toPoint()
            self.drop_at = self.insertion_index(self.drag_point)
            self.scroll_timer.start()
            self.viewport().update()
            event.acceptProposedAction()
        elif self.file_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def auto_scroll(self):
        if self.drag_point is None:
            return
        y, height = self.drag_point.y(), self.viewport().height()
        delta = -28 if y < 45 else 28 if y > height - 45 else 0
        if delta:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + delta)
            self.drop_at = self.insertion_index(self.drag_point)
            self.viewport().update()

    def clear_drop(self):
        self.scroll_timer.stop()
        self.drop_at = self.drag_point = None
        self.viewport().update()

    def dragLeaveEvent(self, event):
        self.clear_drop()
        event.accept()

    def dropEvent(self, event):
        payload = self.payload(event.mimeData())
        try:
            if payload:
                self.workspace.apply_drop(payload, self.insertion_index(event.position().toPoint()))
                event.setDropAction(Qt.DropAction.MoveAction if payload["kind"] == "nodes" else Qt.DropAction.CopyAction)
                event.accept()
            elif self.file_paths(event.mimeData()):
                self.workspace.add_files(self.file_paths(event.mimeData()), use_as_base=self.output)
                event.acceptProposedAction()
            else:
                event.ignore()
        except (CompressionError, ValueError, TypeError, KeyError) as exc:
            self.owner.tell(str(exc))
            event.ignore()
        finally:
            self.clear_drop()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        if not self.pages.count:
            painter.setPen(QColor("#777D90"))
            text = "拖入主 PDF 开始\n\n或从右侧拖入需要的页面" if self.output else "拖入素材 PDF\n\n选好页面，拖到左边"
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, text)
        if self.drop_at is not None and self.pages.count:
            row = min(self.drop_at, self.pages.count - 1)
            rect = self.visualRect(self.pages.index(row))
            x = rect.left() + 2 if self.drop_at < self.pages.count else rect.right() - 2
            painter.setPen(QPen(QColor("#635BFF"), 4))
            painter.drawLine(x, rect.top() + 8, x, rect.bottom() - 8)
        painter.end()

    def remove_selected(self):
        self.owner.plan.remove([self.pages.entries[index.row()][0] for index in self.selectedIndexes()])
        self.owner.refresh_tree()
        self.owner.tell("已从成品移出页面；原 PDF 未改动。Ctrl+Z 可撤销。")

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if self.output and event.button() == Qt.MouseButton.LeftButton and index.isValid() and PageDelegate.remove_rect(self.visualRect(index)).contains(event.position().toPoint()):
            self.owner.plan.remove([self.pages.entries[index.row()][0]])
            self.owner.refresh_tree()
            self.owner.tell("已移出这一页；Ctrl+Z 可撤销，原 PDF 未改动。")
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Undo):
            self.owner.undo()
        elif event.matches(QKeySequence.StandardKey.Redo):
            self.owner.redo()
        elif self.output and event.key() == Qt.Key.Key_Delete:
            self.remove_selected()
        else:
            return super().keyPressEvent(event)
        event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("撤销  Ctrl+Z", self.owner.undo).setEnabled(bool(self.owner.plan.history))
        menu.addAction("重做  Ctrl+Shift+Z", self.owner.redo).setEnabled(bool(self.owner.plan.future))
        if self.output:
            menu.addAction("移出选中页  Delete", self.remove_selected).setEnabled(bool(self.selectedIndexes()))
        menu.exec(event.globalPos())


class SimpleCompositionView(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.shortcuts = []
        for sequence, action in (("Ctrl+Z", owner.undo), ("Ctrl+Shift+Z", owner.redo), ("Ctrl+Y", owner.redo)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
            self.shortcuts.append(shortcut)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("把右边的页面拖到左边，放开就插入；拖动左边的页面，就能调整顺序。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)
        left, right = QWidget(), QWidget()
        a, b = QVBoxLayout(left), QVBoxLayout(right)
        a.setContentsMargins(0, 8, 8, 0)
        b.setContentsMargins(8, 8, 0, 0)
        self.output_label = QLabel("新的 PDF")
        a.addWidget(self.output_label)
        self.output = DragPageView(self, True)
        a.addWidget(self.output, 1)
        header = QHBoxLayout()
        header.addWidget(QLabel("素材页面"), 1)
        self.add_button = QPushButton("添加 PDF")
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(self.choose_files)
        header.addWidget(self.add_button)
        b.addLayout(header)
        self.documents = QComboBox()
        self.documents.setMinimumContentsLength(12)
        self.documents.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.documents.setToolTip("选择要取用页面的 PDF；可以继续添加多个素材")
        self.documents.currentIndexChanged.connect(self.source_changed)
        b.addWidget(self.documents)
        self.source = DragPageView(self, False)
        b.addWidget(self.source, 1)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([740, 420])
        self.notice = QLabel("只生成新文件，不修改原 PDF。双击页面可放大；Ctrl / Shift 可多选。")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        footer = QHBoxLayout()
        self.status = QLabel()
        footer.addWidget(self.status, 1)
        self.save_button = QPushButton("保存 PDF")
        self.save_button.setObjectName("composeSaveButton")
        self.save_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def refresh_sources(self):
        current = self.documents.currentData()
        self.documents.blockSignals(True)
        self.documents.clear()
        for path in self.owner.paths:
            info = self.owner.infos.get(path)
            suffix = "不可用" if info and info.error else f"{info.pages} 页" if info else "检查中…"
            self.documents.addItem(f"{path.name} · {suffix}", path)
            self.documents.setItemData(self.documents.count() - 1, str(path), Qt.ItemDataRole.ToolTipRole)
        index = next((i for i, path in enumerate(self.owner.paths) if path == current), 0)
        self.documents.setCurrentIndex(max(0, index))
        self.documents.blockSignals(False)
        self.source_changed()

    def select_source(self, path):
        self.documents.setCurrentIndex(next((i for i, candidate in enumerate(self.owner.paths) if candidate == path), 0))

    def source_changed(self, *_args):
        path = self.documents.currentData()
        self.source.pages.set_source(path)
        info = self.owner.infos.get(path)
        if info and info.error:
            self.owner.tell(f"{path.name}：{info.error}；可在高级组合中重新检查。")
        self.owner.schedule_thumbnails()

    def add_files(self, paths, use_as_base=False):
        paths = [Path(path).resolve() for path in paths if Path(path).is_file() and Path(path).suffix.lower() == ".pdf"]
        if not paths:
            return
        if use_as_base and not self.owner.plan.pages() and self.owner.auto_main is None:
            self.owner.set_main(paths[0])
        self.owner.add_materials(paths)
        self.refresh_sources()
        selected = paths[1] if use_as_base and len(paths) > 1 else paths[0]
        self.select_source(selected)

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加 PDF 素材", "", "PDF (*.pdf)")
        self.add_files(paths)

    def target(self, position):
        entries = self.output.pages.entries
        if not 0 <= position <= len(entries):
            raise CompressionError("插入位置已变化，请重新拖动。")
        if position == len(entries):
            return "root", len(self.owner.plan.root.children)
        node = self.owner.plan.find(entries[position][0])
        parent = self.owner.plan.parent(node.uid)
        return parent.uid, parent.children.index(node)

    def apply_drop(self, payload, position):
        if payload.get("session") != self.owner.session:
            raise CompressionError("请从当前窗口拖入页面。")
        target = self.target(position)
        if payload.get("kind") == "pages":
            pages = payload.get("pages")
            if not isinstance(pages, list) or not pages or len(pages) > 20000:
                raise CompressionError("无效的页面选择。")
            refs = []
            for entry in pages:
                if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[0], str) or type(entry[1]) is not int:
                    raise CompressionError("无效的页面选择。")
                refs.append(PageRef(Path(entry[0]), entry[1]))
            self.owner.insert_pages(refs, target=target, raise_errors=True)
        elif payload.get("kind") == "nodes":
            ids = payload.get("nodes")
            valid = {uid for uid, _ref in self.output.pages.entries}
            if not isinstance(ids, list) or not ids or any(not isinstance(uid, str) or uid not in valid for uid in ids):
                raise CompressionError("页面已经变化，请重新拖动。")
            self.owner.plan.move(ids, *target)
            self.owner.refresh_tree(ids)
            self.owner.tell("页序已更新；Ctrl+Z 可撤销。")
        else:
            raise CompressionError("整份合并请使用高级组合。")

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存新的 PDF", self.owner.output_edit.text(), "PDF (*.pdf)", options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.owner._output_custom = True
            self.owner.output_edit.setText(path)
            self.owner.validate_and_accept()
