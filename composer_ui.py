"""Three-column visual PDF page composition workspace."""
from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QSpinBox,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from compressor import CompressionError, format_bytes, get_pdf_source_state
from merge_ui import MergeInputInfo, suggest_merge_output
from pdf_composer import CompositionPlan, MAX_OUTPUT_PAGES, PageRef, parse_pages, walk
from qt_dispatch import GuiJobReceiver

PAGE_MIME = "application/x-pdf-size-reducer-composition"
ROLE = Qt.ItemDataRole.UserRole


def button(text, action, layout):
    widget = QPushButton(text)
    widget.setAutoDefault(False)
    widget.clicked.connect(action)
    layout.addWidget(widget)
    return widget


class PageGrid(QListWidget):
    def __init__(self, browser):
        super().__init__()
        self.browser = browser
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setIconSize(QSize(116, 158))
        self.setGridSize(QSize(142, 194))
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setWordWrap(True)
        self.setDragEnabled(not browser.output_mode)

    def startDrag(self, _actions):
        refs = self.browser.selected_refs()
        if not refs:
            return
        mime = QMimeData()
        mime.setData(PAGE_MIME, json.dumps({
            "session": self.browser.owner.session, "kind": "pages",
            "pages": [[str(ref.source), ref.index] for ref in refs],
        }).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class PageBrowser(QWidget):
    """Paginated thumbnails keep large documents from creating unbounded widgets."""
    def __init__(self, owner, output_mode=False):
        super().__init__()
        self.owner, self.output_mode = owner, output_mode
        self.source, self.page_count, self.output_refs = None, 0, []
        self.selected, self.chunk, self.chunk_size = [], 0, 24
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel("选择 PDF 后显示页面缩略图" if not output_mode else "合成树里的页序会实时显示在这里")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.label)
        self.grid = PageGrid(self)
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        self.grid.itemDoubleClicked.connect(lambda item: owner.open_preview(self.ref_at(item.data(ROLE))))
        self.grid.verticalScrollBar().valueChanged.connect(owner.schedule_thumbnails)
        layout.addWidget(self.grid, 1)
        navigation = QHBoxLayout()
        button("上一组", lambda: self.show_chunk(self.chunk - 1), navigation)
        self.jump = QSpinBox()
        self.jump.setPrefix("跳至第 ")
        self.jump.setSuffix(" 页")
        self.jump.setMinimum(1)
        self.jump.valueChanged.connect(lambda value: self.show_chunk((value - 1) // self.chunk_size))
        navigation.addWidget(self.jump, 1)
        button("下一组", lambda: self.show_chunk(self.chunk + 1), navigation)
        layout.addLayout(navigation)
        if not output_mode:
            ranges = QHBoxLayout()
            self.range_edit = QLineEdit()
            self.range_edit.setPlaceholderText("选页：1, 3-5（可跨组）")
            self.range_edit.returnPressed.connect(self.select_range)
            ranges.addWidget(self.range_edit, 1)
            button("选中页码", self.select_range, ranges)
            layout.addLayout(ranges)
            actions = QHBoxLayout()
            self.insert_button = button("插入选中页 →", self.insert_selected, actions)
            self.all_button = button("整份加入树", self.insert_all, actions)
            layout.addLayout(actions)
        self.refresh()

    def count(self):
        return len(self.output_refs) if self.output_mode else self.page_count

    def ref_at(self, index):
        return self.output_refs[index] if self.output_mode else PageRef(self.source, index)

    def set_document(self, source, count):
        if (source, count) == (self.source, self.page_count):
            return
        self.source, self.page_count = source, count
        self.selected, self.chunk = [], 0
        self.range_edit.clear()
        self.refresh()

    def set_output(self, refs):
        self.output_refs = list(refs)
        self.selected = []
        self.refresh()

    def selected_refs(self):
        return [self.ref_at(index) for index in self.selected if 0 <= index < self.count()]

    def current_refs(self):
        return [self.ref_at(self.grid.item(row).data(ROLE)) for row in range(self.grid.count())]

    def show_chunk(self, chunk):
        new = max(0, min(chunk, max(0, (self.count() - 1) // self.chunk_size)))
        if new != self.chunk:
            self.chunk = new
            self.refresh()

    def reveal(self, index):
        if 0 <= index < self.count():
            self.show_chunk(index // self.chunk_size)
            self.grid.setCurrentRow(index % self.chunk_size)

    def refresh(self):
        self._updating = True
        self.chunk = min(self.chunk, max(0, (self.count() - 1) // self.chunk_size))
        self.grid.clear()
        start = self.chunk * self.chunk_size
        for index in range(start, min(start + self.chunk_size, self.count())):
            ref = self.ref_at(index)
            caption = f"输出第 {index + 1} 页\n{ref.source.name} · {ref.index + 1}" if self.output_mode else f"第 {index + 1} 页"
            item = QListWidgetItem(caption)
            item.setData(ROLE, index)
            item.setToolTip(f"{ref.source}\n原第 {ref.index + 1} 页；双击放大查看")
            item.setIcon(self.owner.thumbnail_icon(ref))
            self.grid.addItem(item)
            item.setSelected(index in self.selected)
        self.jump.blockSignals(True)
        self.jump.setMaximum(max(1, self.count()))
        self.jump.setValue(start + 1)
        self.jump.blockSignals(False)
        self._updating = False
        self.update_label()
        self.owner.schedule_thumbnails()

    def update_label(self):
        if self.output_mode:
            text = f"成品共 {self.count()} 页 · 与合成树一致 · 双击放大"
        elif self.source:
            text = f"{self.source.name}\n共 {self.count()} 页 · 已选 {len(self.selected)} 页 · Ctrl / Shift 多选"
        else:
            text = "尚未选择可用的 PDF"
        self.label.setText(text)
        if not self.output_mode:
            self.insert_button.setEnabled(bool(self.selected))
            self.all_button.setEnabled(bool(self.page_count))

    def _selection_changed(self):
        if self._updating:
            return
        visible = {self.grid.item(row).data(ROLE) for row in range(self.grid.count())}
        self.selected = [index for index in self.selected if index not in visible]
        self.selected.extend(sorted(item.data(ROLE) for item in self.grid.selectedItems()))
        self.update_label()

    def select_range(self):
        try:
            self.selected = parse_pages(self.range_edit.text(), self.count())
            if self.selected:
                self.chunk = self.selected[0] // self.chunk_size
            self.refresh()
        except CompressionError as exc:
            self.owner.tell(str(exc))

    def insert_selected(self):
        self.owner.insert_pages(self.selected_refs())

    def insert_all(self):
        if self.count() > MAX_OUTPUT_PAGES:
            self.owner.tell(f"该文件超过 {MAX_OUTPUT_PAGES:,} 页，请选择需要的页面分批合成。")
            return
        self.owner.insert_pages([self.ref_at(index) for index in range(self.count())], group=self.source.name if self.source else None)

    def update_thumbnail(self, ref):
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if self.ref_at(item.data(ROLE)) == ref:
                item.setIcon(self.owner.thumbnail_icon(ref))


class MaterialList(QListWidget):
    files_dropped = Signal(object)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(100)
        self.setMaximumHeight(180)

    def startDrag(self, _actions):
        mime = QMimeData()
        mime.setData(PAGE_MIME, json.dumps({"session": self.owner.session, "kind": "documents", "paths": [str(item.data(ROLE)) for item in self.selectedItems()]}).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.files_dropped.emit([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()


class CompositionTree(QTreeWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setHeaderLabels(["合成结构 / 来源", "成品页码"])
        self.setColumnWidth(0, 270)
        self.setIconSize(QSize(36, 48))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setAlternatingRowColors(True)
        self.verticalScrollBar().valueChanged.connect(owner.schedule_thumbnails)

    def mimeTypes(self):
        return [PAGE_MIME]

    def startDrag(self, _actions):
        mime = QMimeData()
        mime.setData(PAGE_MIME, json.dumps({"session": self.owner.session, "kind": "nodes", "ids": self.owner.selected_ids()}).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def valid_payload(self, mime):
        if not mime.hasFormat(PAGE_MIME) or len(mime.data(PAGE_MIME)) > 4 * 1024**2:
            return None
        try:
            data = json.loads(bytes(mime.data(PAGE_MIME)))
            return data if isinstance(data, dict) and data.get("session") == self.owner.session else None
        except (ValueError, UnicodeError):
            return None

    def dragEnterEvent(self, event):
        if self.valid_payload(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self.valid_payload(event.mimeData()):
            super().dragMoveEvent(event)
            event.acceptProposedAction()

    def drop_location(self, position):
        item = self.itemAt(position)
        if item is None or item.data(0, ROLE) == "root":
            return "root", len(self.owner.plan.root.children)
        uid = item.data(0, ROLE)
        node = self.owner.plan.find(uid)
        rect = self.visualItemRect(item)
        if node.page is None and rect.top() + rect.height() * .25 <= position.y() <= rect.bottom() - rect.height() * .25:
            return uid, len(node.children)
        parent = self.owner.plan.parent(uid)
        at = parent.children.index(node)
        return parent.uid, at + (position.y() >= rect.center().y())

    def dropEvent(self, event):
        payload = self.valid_payload(event.mimeData())
        if not payload:
            event.ignore()
            return
        parent, index = self.drop_location(event.position().toPoint())
        try:
            if payload.get("kind") == "pages":
                refs = [PageRef(Path(path).resolve(), page) for path, page in payload["pages"]]
                self.owner.insert_pages(refs, target=(parent, index), raise_errors=True)
                event.setDropAction(Qt.DropAction.CopyAction)
            elif payload.get("kind") == "nodes":
                self.owner.plan.move(payload["ids"], parent, index)
                self.owner.refresh_tree()
                event.setDropAction(Qt.DropAction.MoveAction)
            elif payload.get("kind") == "documents":
                self.owner.insert_documents([Path(path).resolve() for path in payload["paths"]], target=(parent, index))
                event.setDropAction(Qt.DropAction.CopyAction)
            else:
                event.ignore()
                return
        except (CompressionError, ValueError, TypeError, KeyError) as exc:
            self.owner.tell(str(exc))
            event.ignore()
            return
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.owner.remove_nodes()
        elif event.modifiers() == Qt.KeyboardModifier.AltModifier and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.owner.shift_nodes(-1 if event.key() == Qt.Key.Key_Up else 1)
        else:
            super().keyPressEvent(event)


class PageCanvas(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.page_scene = QGraphicsScene(self)
        self.setScene(self.page_scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def show_pixmap(self, pixmap):
        self.scene().clear()
        self.scene().addPixmap(pixmap)
        self.scene().setSceneRect(QRectF(pixmap.rect()))
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        scale = self.transform().m11() * factor
        if .05 <= scale <= 8:
            self.scale(factor, factor)
        event.accept()


class ComposerDialog(QDialog):
    def __init__(self, parent, initial_paths, protected_paths, inspect_factory, render_factory):
        super().__init__(parent)
        self.session = uuid4().hex
        self.plan = CompositionPlan()
        self.inspect_factory, self.render_factory = inspect_factory, render_factory
        self.protected = {Path(path).resolve() for path in protected_paths}
        self.paths, self.infos, self.material_items = [], {}, {}
        self.main_source, self.auto_main = None, None
        self.jobs, self.cache, self.tree_items = {}, OrderedDict(), {}
        self.job_receivers = {}
        self.ref_items = {}
        self._closing, self._result, self._building = False, QDialog.DialogCode.Rejected, True
        self._generation, self._output_custom = 0, False
        self.preview_dialog = self.preview_canvas = None
        self.preview_pending = None
        self.preview_generation = 0
        self.setWindowTitle("可视化 PDF 合成 · 主 PDF / 合成树 / 素材库")
        self.resize(1390, 880)
        self.setMinimumSize(1100, 700)
        self.setAcceptDrops(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        heading = QLabel("把需要的页面，放到想要的位置")
        heading.setProperty("title", True)
        root.addWidget(heading)
        hint = QLabel("左侧主 PDF 与右侧素材都能选页拖入中间树；从上到下、逐层展开的顺序，就是新 PDF 的页序。只创建新文件，不改源 PDF。")
        hint.setWordWrap(True)
        root.addWidget(hint)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 6, 0)
        main_actions = QHBoxLayout()
        main_actions.addWidget(QLabel("① 主 PDF / 成品"), 1)
        button("选择主 PDF…", self.choose_main, main_actions)
        left_layout.addLayout(main_actions)
        self.left_tabs = QTabWidget()
        self.main_browser = PageBrowser(self)
        self.output_browser = PageBrowser(self, output_mode=True)
        self.left_tabs.addTab(self.main_browser, "主 PDF 页面")
        self.left_tabs.addTab(self.output_browser, "合成预览（实时）")
        self.left_tabs.currentChanged.connect(self.schedule_thumbnails)
        left_layout.addWidget(self.left_tabs)
        splitter.addWidget(left)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(4, 0, 4, 0)
        center_layout.addWidget(QLabel("② 合成树 · 分组可以嵌套，页面可以跨组移动"))
        tools = QHBoxLayout()
        button("新分组", self.new_group, tools)
        button("改组名", self.rename_group, tools)
        button("移出树", self.remove_nodes, tools)
        button("↑", lambda: self.shift_nodes(-1), tools)
        button("↓", lambda: self.shift_nodes(1), tools)
        center_layout.addLayout(tools)
        tools2 = QHBoxLayout()
        self.undo_button = button("撤销", self.undo, tools2)
        self.redo_button = button("重做", self.redo, tools2)
        button("展开", lambda: self.tree.expandAll(), tools2)
        button("折叠", self.collapse_tree, tools2)
        center_layout.addLayout(tools2)
        self.tree = CompositionTree(self)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        self.tree.itemDoubleClicked.connect(self.tree_double_click)
        center_layout.addWidget(self.tree, 1)
        self.insert_mode = QComboBox()
        self.insert_mode.addItems(["按钮插入：放在选中项之后", "按钮插入：放在选中项之前", "按钮插入：放入选中分组", "按钮插入：追加到整个 PDF 末尾"])
        self.insert_mode.currentIndexChanged.connect(self.update_summary)
        center_layout.addWidget(self.insert_mode)
        self.tree_summary = QLabel()
        self.tree_summary.setWordWrap(True)
        self.tree_summary.setTextFormat(Qt.TextFormat.PlainText)
        center_layout.addWidget(self.tree_summary)
        splitter.addWidget(center)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        materials = QHBoxLayout()
        materials.addWidget(QLabel("③ 素材库 · 可继续加入多个 PDF"), 1)
        button("＋ 添加…", self.choose_materials, materials)
        right_layout.addLayout(materials)
        self.material_search = QLineEdit()
        self.material_search.setPlaceholderText("搜索素材文件名…（素材库支持多选和整份拖入树）")
        self.material_search.textChanged.connect(self.filter_materials)
        right_layout.addWidget(self.material_search)
        self.materials = MaterialList(self)
        self.materials.files_dropped.connect(self.add_materials)
        self.materials.currentItemChanged.connect(self.material_selected)
        right_layout.addWidget(self.materials)
        material_actions = QHBoxLayout()
        button("选中 PDF 整份入树", self.insert_material_documents, material_actions)
        button("重新检查", self.recheck, material_actions)
        right_layout.addLayout(material_actions)
        self.material_browser = PageBrowser(self)
        right_layout.addWidget(self.material_browser, 1)
        splitter.addWidget(right)
        splitter.setSizes([385, 525, 385])
        self.notice = QLabel("先选择主 PDF，或将多个 PDF 拖入右侧素材库；素材不会自动追加到成品。")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        output = QHBoxLayout()
        output.addWidget(QLabel("另存为"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("自动生成不覆盖原文件的保存名称，也可自行修改")
        self.output_edit.textEdited.connect(lambda _text: setattr(self, "_output_custom", True))
        self.output_edit.textChanged.connect(self.update_summary)
        output.addWidget(self.output_edit, 1)
        button("选择位置…", self.choose_output, output)
        self.after_combo = QComboBox()
        self.after_combo.addItems(["只合成并保存", "保存后继续压缩"])
        self.after_combo.setCurrentIndex(1)
        output.addWidget(self.after_combo)
        root.addLayout(output)
        footer = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self.status, 1)
        button("返回", self.reject, footer)
        self.save_button = button("生成新的 PDF", self.validate_and_accept, footer)
        self.save_button.setObjectName("composeSaveButton")
        root.addLayout(footer)
        self.setStyleSheet("""
            QTreeWidget, QListWidget { background: #FFFFFF; border: 1px solid #E4E5EC; border-radius: 10px; }
            QTreeWidget::item { padding: 5px 2px; }
            QTreeWidget::item:selected, QListWidget::item:selected { background: #E7E4FF; color: #222238; }
            QPushButton#composeSaveButton { background: #635BFF; color: white; padding: 10px 24px; border: none; }
            QPushButton#composeSaveButton:disabled { background: #CBC9E2; }
        """)
        self._building = False
        self.thumb_timer = QTimer(self)
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.setInterval(60)
        self.thumb_timer.timeout.connect(self.start_thumbnails)
        self.refresh_tree()
        if initial_paths:
            self.set_main(initial_paths[0])
            self.add_materials(initial_paths[1:])
            if len(initial_paths) > 1:
                item = self.material_items.get(Path(initial_paths[1]).resolve())
                if item:
                    self.materials.setCurrentItem(item, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def tell(self, message):
        self.notice.setText(message)

    def selected_ids(self):
        return [item.data(0, ROLE) for item in self.tree.selectedItems()]

    def source_paths(self):
        return list(dict.fromkeys(ref.source for ref in self.plan.pages()))

    def source_states(self):
        return {path: self.infos[path].state for path in self.source_paths()}

    def output_path(self):
        path = Path(self.output_edit.text().strip()).expanduser()
        if not path.is_absolute() and self.paths:
            path = self.paths[0].parent / path
        return path.with_suffix(".pdf") if not path.suffix else path

    def load_after_merge(self):
        return self.after_combo.currentIndex() == 1

    def add_materials(self, paths):
        added = skipped = 0
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
                if path in self.paths or not path.is_file() or path.suffix.lower() != ".pdf":
                    skipped += 1
                    continue
            except (OSError, ValueError):
                skipped += 1
                continue
            self.paths.append(path)
            item = QListWidgetItem(f"{path.name} · 检查中…")
            item.setData(ROLE, path)
            item.setToolTip(str(path))
            self.materials.addItem(item)
            self.material_items[path] = item
            added += 1
        self.tell(f"素材库共 {len(self.paths)} 个 PDF；新增 {added} 个，跳过 {skipped} 个重复或无效文件。按需读取缩略图。")
        self.filter_materials(self.material_search.text())
        if self.paths and not self._output_custom:
            try:
                self.output_edit.setText(str(suggest_merge_output(self.main_source or self.paths[0], [*self.paths, *self.protected])))
            except CompressionError as exc:
                self.tell(str(exc))
        if self.materials.currentRow() < 0 and self.materials.count():
            self.materials.setCurrentRow(0)
        QTimer.singleShot(0, self.start_inspection)

    def set_main(self, path):
        path = Path(path).resolve()
        self.main_source = path
        self.auto_main = path if not self.plan.pages() else None
        if path not in self.paths:
            self.add_materials([path])
        if path in self.infos:
            self.info_ready(self._generation, self.infos[path])
        else:
            self.main_browser.set_document(None, 0)

    def choose_main(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择主 PDF（不覆盖或清空现有合成树）", "", "PDF (*.pdf)")
        if path:
            self.set_main(path)

    def choose_materials(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加素材 PDF（可以继续多次添加）", "", "PDF (*.pdf)")
        if paths:
            self.add_materials(paths)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.add_materials([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()

    def material_selected(self, item, _previous=None):
        path = item.data(ROLE) if item else None
        info = self.infos.get(path)
        self.material_browser.set_document(path if info and not info.error else None, info.pages if info and not info.error else 0)
        if info and info.error:
            self.tell(f"{path.name}：{info.error}")

    def filter_materials(self, text):
        for path, item in self.material_items.items():
            item.setHidden(text.casefold() not in path.name.casefold())

    def _start_job(self, name, worker, on_item, on_error, on_finished):
        thread = QThread(self)
        self.jobs[name] = (thread, worker)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        for signal in (worker.completed, worker.failed, worker.cancelled):
            signal.connect(thread.quit)
            signal.connect(worker.deleteLater)

        def finished():
            self.jobs.pop(name, None)
            self.job_receivers.pop(name, None)
            if self._closing:
                if not self.jobs:
                    super(ComposerDialog, self).done(self._result)
            else:
                on_finished()

        receiver = GuiJobReceiver(self, on_item, on_error, finished)
        self.job_receivers[name] = receiver
        worker.item_ready.connect(receiver.item, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(receiver.error, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(receiver.finished, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def start_inspection(self):
        if self._closing or "inspect" in self.jobs:
            return
        batch = [path for path in self.paths if path not in self.infos][:64]
        if not batch:
            self.update_summary()
            return
        generation = self._generation

        def failed(message):
            for path in batch:
                self.info_ready(generation, MergeInputInfo(path, error=message))

        self._start_job("inspect", self.inspect_factory(batch),
                        lambda info: self.info_ready(generation, info), failed,
                        lambda: QTimer.singleShot(0, self.start_inspection))

    def info_ready(self, generation, info):
        if self._closing or generation != self._generation or info.path not in self.paths:
            return
        self.infos[info.path] = info
        item = self.material_items[info.path]
        item.setText(f"{info.path.name} · " + (f"不可用：{info.error}" if info.error else f"{info.pages} 页 · {format_bytes(info.state.size)}"))
        item.setForeground(QColor("#C03535" if info.error else "#18181B"))
        if info.path == self.main_source:
            self.main_browser.set_document(info.path if not info.error else None, info.pages if not info.error else 0)
            if self.auto_main == info.path and not info.error:
                self.auto_main = None
                if not self.plan.pages() and info.pages <= MAX_OUTPUT_PAGES:
                    self.insert_pages([PageRef(info.path, index) for index in range(info.pages)], group=f"主 PDF · {info.path.name}", target=("root", 0))
                elif info.pages > MAX_OUTPUT_PAGES:
                    self.tell(f"主 PDF 超过 {MAX_OUTPUT_PAGES:,} 页，请选择需要的页面。")
        current = self.materials.currentItem()
        if current and current.data(ROLE) == info.path:
            self.material_selected(current)
        for ref, items in self.ref_items.items():
            if ref.source == info.path:
                color = QColor("#C03535" if info.error or ref.index >= info.pages else "#18181B")
                for tree_item in items:
                    tree_item.setForeground(0, color)
        self.update_summary()
        self.schedule_thumbnails()

    def recheck(self):
        self._generation += 1
        self.preview_generation += 1
        if self.preview_dialog:
            self.preview_dialog.close()
        self.infos.clear()
        self.cache.clear()
        for name in ("inspect", "thumb", "preview"):
            if name in self.jobs:
                self.jobs[name][1].cancel()
        self.main_browser.set_document(None, 0)
        self.material_browser.set_document(None, 0)
        for path, item in self.material_items.items():
            item.setText(f"{path.name} · 重新检查中…")
        self.refresh_tree()
        self.tell("正在重新检查所有素材；合成树保持原页码，请确认变化后的页面内容。")
        QTimer.singleShot(0, self.start_inspection)

    def insertion_target(self):
        mode = self.insert_mode.currentIndex()
        item = self.tree.currentItem()
        node = self.plan.find(item.data(0, ROLE)) if item else self.plan.root
        if node is None or node is self.plan.root or mode == 3:
            return "root", len(self.plan.root.children)
        if mode == 2:
            if node.page is not None:
                raise CompressionError("“放入分组”需要先选中分组；插入页面前后请切换插入方式。")
            return node.uid, len(node.children)
        parent = self.plan.parent(node.uid)
        return parent.uid, parent.children.index(node) + (mode == 0)

    def insert_pages(self, refs, group=None, target=None, raise_errors=False):
        try:
            if not refs:
                raise CompressionError("请先选择需要插入的页面。")
            for ref in refs:
                info = self.infos.get(ref.source)
                if not info or info.error or type(ref.index) is not int or not 0 <= ref.index < info.pages:
                    raise CompressionError("所选页面不可用或尚未检查完成，请确认素材及页码。")
            parent, at = target or self.insertion_target()
            selected = self.plan.insert(refs, parent, at, group)
            self.refresh_tree(selected)
            self.tell(f"已插入 {len(refs)} 页；源文件未改动。成品共 {len(self.plan.pages())} 页，可撤销。")
        except CompressionError as exc:
            if raise_errors:
                raise
            self.tell(str(exc))

    def insert_material_documents(self):
        selected = [item for item in self.materials.selectedItems() if not item.isHidden()]
        if not selected:
            self.tell("请先在素材库中选择 PDF（Ctrl / Shift 可多选）。")
            return
        try:
            self.insert_documents([item.data(ROLE) for item in selected])
        except CompressionError as exc:
            self.tell(str(exc))

    def insert_documents(self, paths, target=None):
        if not paths or any(path not in self.infos or self.infos[path].error for path in paths):
            raise CompressionError("所选素材尚未检查完毕或存在不可用文件。")
        if sum(self.infos[path].pages for path in paths) + len(self.plan.pages()) > MAX_OUTPUT_PAGES:
            raise CompressionError(f"单次最多合成 {MAX_OUTPUT_PAGES:,} 页，请选择部分页面。")
        parent, at = target or self.insertion_target()
        selected = self.plan.insert_groups([(path.name, [PageRef(path, index) for index in range(self.infos[path].pages)]) for path in paths], parent, at)
        self.refresh_tree(selected)
        self.tell(f"已把 {len(paths)} 份 PDF 分别作为分支加入合成树；可以整次撤销。")

    def new_group(self):
        title, ok = QInputDialog.getText(self, "新建分组", "分组名称（导出为书签）")
        if ok and title.strip():
            try:
                parent, at = self.insertion_target()
                selected = self.plan.insert([], parent, at, title.strip()[:200])
                self.refresh_tree(selected)
            except CompressionError as exc:
                self.tell(str(exc))

    def rename_group(self):
        item = self.tree.currentItem()
        node = self.plan.find(item.data(0, ROLE)) if item else None
        if not node or node.page is not None:
            self.tell("请先选择要改名的分组。")
            return
        title, ok = QInputDialog.getText(self, "分组名称", "名称", text=node.title)
        if ok:
            self.plan.rename(node.uid, title)
            self.refresh_tree([node.uid])

    def remove_nodes(self):
        self.plan.remove(self.selected_ids())
        self.refresh_tree()
        self.tell("已移出合成树；仅影响新 PDF 的方案，原文件未删除，可撤销。")

    def shift_nodes(self, direction):
        ids = self.selected_ids()
        self.plan.shift(ids, direction)
        self.refresh_tree(ids)

    def undo(self):
        self.plan.undo()
        self.refresh_tree()

    def redo(self):
        self.plan.redo()
        self.refresh_tree()

    def refresh_tree(self, selected=()):
        if self._building:
            return
        expanded = {uid for uid, item in self.tree_items.items() if item.isExpanded()}
        previous = set(self.tree_items)
        self.tree.blockSignals(True)
        self.tree.clear()
        self.tree_items = {}
        self.ref_items = {}
        page_offset = 0

        def add(node, parent=None):
            nonlocal page_offset
            item = QTreeWidgetItem([node.title, ""])
            item.setData(0, ROLE, node.uid)
            item.setToolTip(0, str(node.page.source) if node.page else "分组按展开后的顺序合成；可以拖入页面或其他分组")
            self.tree_items[node.uid] = item
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            if node.page:
                self.ref_items.setdefault(node.page, []).append(item)
                item.setText(0, f"原第 {node.page.index + 1} 页 · {node.page.source.name}")
                page_offset += 1
                item.setText(1, str(page_offset))
                item.setIcon(0, self.thumbnail_icon(node.page))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
                info = self.infos.get(node.page.source)
                if not info or info.error or node.page.index >= info.pages:
                    item.setForeground(0, QColor("#C03535"))
            else:
                start = page_offset
                if node.uid == "root":
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                for child in node.children:
                    add(child, item)
                item.setText(1, f"{page_offset - start} 页")
                item.setExpanded(node.uid == "root" or node.uid in expanded or node.uid not in previous)
            item.setSelected(node.uid in selected)
            return item

        add(self.plan.root)
        if selected and selected[0] in self.tree_items:
            self.tree.setCurrentItem(self.tree_items[selected[0]], 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.tree.blockSignals(False)
        self.output_browser.set_output(self.plan.pages())
        self.update_summary()
        self.schedule_thumbnails()

    def collapse_tree(self):
        self.tree.collapseAll()
        self.tree_items["root"].setExpanded(True)

    def selection_changed(self):
        item = self.tree.currentItem()
        if item:
            node = self.plan.find(item.data(0, ROLE))
            if node and node.page:
                self.left_tabs.setCurrentWidget(self.output_browser)
                self.output_browser.reveal(int(item.text(1)) - 1)
        self.update_summary()

    def tree_double_click(self, item, _column):
        node = self.plan.find(item.data(0, ROLE))
        if node and node.page:
            self.open_preview(node.page)

    def update_summary(self, *_args):
        if self._building:
            return
        refs = self.plan.pages()
        sources = {ref.source for ref in refs}
        invalid = sum(bool(not self.infos.get(ref.source) or self.infos[ref.source].error or ref.index >= self.infos[ref.source].pages) for ref in refs)
        used_bytes = sum(self.infos[path].state.size for path in sources if path in self.infos and self.infos[path].state)
        self.undo_button.setEnabled(bool(self.plan.history))
        self.redo_button.setEnabled(bool(self.plan.future))
        item = self.tree.currentItem()
        target = item.text(0) if item else "整个 PDF"
        self.tree_summary.setText(f"成品：{len(refs)} 页，来自 {len(sources)} 个 PDF\n当前插入参考：{target}")
        self.status.setText(f"素材库 {len(self.paths)} 份 · 合成 {len(refs)} 页" + (f" · {invalid} 页待检查或不可用" if invalid else " · 分组将保存为书签") + (" · 使用的源文件超过 16 GiB，请分批" if used_bytes > 16 * 1024**3 else ""))
        self.save_button.setEnabled(bool(refs) and not invalid and used_bytes <= 16 * 1024**3 and bool(self.output_edit.text().strip()) and not self._closing)

    def thumbnail_icon(self, ref):
        info = self.infos.get(ref.source)
        key = (ref, info.state if info else None)
        pixmap = self.cache.get(key)
        return QIcon(pixmap) if isinstance(pixmap, QPixmap) else QIcon()

    def schedule_thumbnails(self, *_args):
        if not self._building and not self._closing and hasattr(self, "thumb_timer"):
            self.thumb_timer.start()

    def wanted_thumbnails(self):
        refs = self.left_tabs.currentWidget().current_refs() + self.material_browser.current_refs()
        item = self.tree.itemAt(QPoint(6, 3))
        if item is None and self.tree.topLevelItemCount():
            item = self.tree.topLevelItem(0)
        for _ in range(32):
            if item is None:
                break
            node = self.plan.find(item.data(0, ROLE))
            if node and node.page:
                refs.append(node.page)
            item = self.tree.itemBelow(item)
        return list(dict.fromkeys(refs))

    def start_thumbnails(self):
        if self._closing or "thumb" in self.jobs:
            return
        requests = []
        for ref in self.wanted_thumbnails():
            info = self.infos.get(ref.source)
            if info and not info.error and ref.index < info.pages and (ref, info.state) not in self.cache:
                requests.append((ref, info.state))
        if not requests:
            return
        requests = requests[:12]

        def failed(message):
            for ref, state in requests:
                self.cache[(ref, state)] = message
            while len(self.cache) > 192:
                (old_ref, _old_state), _old_value = self.cache.popitem(last=False)
                self.refresh_icon(old_ref)
            self.tell(f"部分页面缩略图无法读取：{message}；可重新检查后重试。")

        self._start_job("thumb", self.render_factory(requests, 250), self.thumbnail_ready,
                        failed, self.schedule_thumbnails)

    def thumbnail_ready(self, item):
        ref, state, data, error = item
        info = self.infos.get(ref.source)
        if self._closing or not info or state != info.state:
            return
        pixmap = QPixmap()
        if error or not pixmap.loadFromData(data):
            self.cache[(ref, state)] = error or "无法读取页面图像"
            self.tell(f"{ref.source.name} 第 {ref.index + 1} 页预览失败，可双击重试。")
        else:
            self.cache[(ref, state)] = pixmap
        self.cache.move_to_end((ref, state))
        while len(self.cache) > 192:
            (old_ref, _old_state), _old_pixmap = self.cache.popitem(last=False)
            self.refresh_icon(old_ref)
        self.refresh_icon(ref)

    def refresh_icon(self, ref):
        for browser in (self.main_browser, self.output_browser, self.material_browser):
            browser.update_thumbnail(ref)
        for tree_item in self.ref_items.get(ref, []):
            tree_item.setIcon(0, self.thumbnail_icon(ref))

    def open_preview(self, ref):
        info = self.infos.get(ref.source)
        if not info or info.error:
            self.tell("请先完成源文件检查。")
            return
        self.preview_generation += 1
        generation = self.preview_generation
        if self.preview_dialog:
            self.preview_dialog.close()
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{ref.source.name} · 原第 {ref.index + 1} 页 · 滚轮缩放 / 拖动查看")
        dialog.resize(850, 850)
        layout = QVBoxLayout(dialog)
        label = QLabel("正在读取高清页面…")
        label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(label)
        canvas = PageCanvas()
        layout.addWidget(canvas, 1)
        self.preview_dialog, self.preview_canvas = dialog, canvas
        self.preview_pending = (ref, info.state, generation, label)

        def closed():
            if self.preview_dialog is dialog:
                self.preview_dialog = None
                self.preview_pending = None
                if "preview" in self.jobs:
                    self.jobs["preview"][1].cancel()
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.show()
        if "preview" in self.jobs:
            self.jobs["preview"][1].cancel()
        self.start_preview()

    def start_preview(self):
        if self._closing or not self.preview_pending or "preview" in self.jobs:
            return
        ref, state, generation, label = self.preview_pending
        self.preview_pending = None

        def ready(item):
            if self._closing or not self.preview_dialog or generation != self.preview_generation:
                return
            _ref, _state, data, error = item
            pixmap = QPixmap()
            if error or not pixmap.loadFromData(data):
                label.setText(error or "无法读取预览")
                return
            self.preview_canvas.show_pixmap(pixmap)
            label.setText(f"{ref.source.name} · 第 {ref.index + 1} 页（原始页面） · 滚轮缩放，按住鼠标拖动")

        def failed(message):
            if self.preview_dialog and generation == self.preview_generation:
                label.setText(message)

        self._start_job("preview", self.render_factory([(ref, state)], 1400), ready, failed, self.start_preview)

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存合成 PDF", str(self.output_path()) if self.output_edit.text() else "", "PDF (*.pdf)", options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self._output_custom = True
            self.output_edit.setText(path)

    def validate_and_accept(self):
        try:
            refs = self.plan.pages()
            if not refs:
                raise CompressionError("合成树里还没有页面，请从左右两侧选页加入。")
            self.plan._validate()
            for ref in refs:
                info = self.infos.get(ref.source)
                if not info or info.error or ref.index >= info.pages:
                    raise CompressionError("合成树含有不可用页面，请重新检查素材并确认页码。")
            for path, state in self.source_states().items():
                if get_pdf_source_state(path) != state:
                    raise CompressionError("源 PDF 已变化，请重新检查素材并确认页面后再生成。")
            if not self.output_edit.text().strip():
                raise CompressionError("请选择结果的保存位置。")
            destination = self.output_path().resolve()
            if destination in {*self.paths, *self.protected}:
                raise CompressionError("不能覆盖任何素材、主 PDF 或压缩工作区中的文件，请另存为新 PDF。")
            if destination.suffix.lower() != ".pdf" or (destination.exists() and not destination.is_file()):
                raise CompressionError("请选择有效的 PDF 文件名。")
            if sum(state.size for state in self.source_states().values()) > 16 * 1024**3:
                raise CompressionError("使用的源文件超过 16 GiB，请分批合成。")
        except (CompressionError, OSError, ValueError) as exc:
            self.tell(str(exc))
            return
        if destination.exists():
            answer = QMessageBox.question(self, "替换已有结果？", f"已存在：\n{destination}\n\n选择“否”可保留当前合成树并修改文件名。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.output_edit.setText(str(destination))
        self.done(QDialog.DialogCode.Accepted)

    def done(self, result):
        self._closing, self._result = True, result
        self.thumb_timer.stop()
        if self.preview_dialog:
            self.preview_dialog.close()
        if self.jobs:
            for _thread, worker in self.jobs.values():
                worker.cancel()
            self.setEnabled(False)
            self.status.setText("正在结束页面读取…")
        else:
            super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self.reject()
        event.ignore() if self.jobs else event.accept()


def smoke_test(dialog_factory, worker_factory, app_style, screenshot=None):
    """Exercise real inspection, thumbnails, page preview, tree export and ordering."""
    import os
    import tempfile
    import time
    import pymupdf as fitz
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(app_style)
    if os.name == "nt" and not QFontDatabase.families():
        for filename in ("segoeui.ttf", "msyh.ttc"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Microsoft YaHei", 9))

    def wait_for(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            if time.monotonic() >= deadline:
                raise RuntimeError("Visual composer self-test timed out")
            time.sleep(.01)
        app.processEvents()

    with tempfile.TemporaryDirectory(prefix="pdf-composer-check-") as folder:
        workspace = Path(folder).resolve()
        sources = []
        for name, count, color in (("Main report", 4, (.27, .29, .7)),
                                    ("Figure collection", 3, (.1, .5, .57)),
                                    ("Appendix", 2, (.65, .35, .18))):
            path = workspace / f"{name}.pdf"
            with fitz.open() as document:
                for index in range(count):
                    page = document.new_page(width=420, height=590)
                    page.draw_rect(fitz.Rect(0, 0, 420, 125), color=color, fill=color)
                    page.insert_text((28, 50), name, fontsize=24, color=(1, 1, 1))
                    page.insert_text((28, 90), f"PAGE {index + 1:02d}", fontsize=18, color=(1, 1, 1))
                    page.insert_text((28, 160), "Original vector content / searchable text", fontsize=13)
                    for row in range(3):
                        y = 215 + row * 72
                        page.draw_rect(fitz.Rect(30, y, 110 + index * 50 + row * 20, y + 32), color=color, fill=color)
                        page.insert_text((30, y + 51), f"Section {row + 1} - source page {index + 1}", fontsize=11)
                    page.insert_text((28, 550), "PDF Size Reducer - visual composition demo", fontsize=11)
                document.save(path)
            sources.append(path)
        dialog = dialog_factory(None, sources)
        dialog.show()
        try:
            wait_for(lambda: len(dialog.infos) == 3 and len(dialog.cache) >= 7 and not dialog.jobs)
            assert len(dialog.plan.pages()) == 4
            group = dialog.plan.root.children[0]
            dialog.insert_pages([PageRef(sources[1], 0), PageRef(sources[1], 2)], group="补充图表", target=(group.uid, 2))
            dialog.insert_pages([PageRef(sources[2], 1)], group="附件", target=("root", 1))
            desired = [PageRef(sources[0], 0), PageRef(sources[0], 1), PageRef(sources[1], 0), PageRef(sources[1], 2), PageRef(sources[0], 2), PageRef(sources[0], 3), PageRef(sources[2], 1)]
            assert dialog.plan.pages() == desired
            dialog.undo()
            assert len(dialog.plan.pages()) == 6
            dialog.redo()
            assert dialog.plan.pages() == desired
            dialog.left_tabs.setCurrentWidget(dialog.output_browser)
            wait_for(lambda: not dialog.jobs and all((ref, dialog.infos[ref.source].state) in dialog.cache for ref in desired))
            dialog.open_preview(PageRef(sources[1], 2))
            wait_for(lambda: "preview" not in dialog.jobs)
            assert dialog.preview_canvas.scene().items()
            dialog.preview_dialog.close()
            dialog.output_edit.setText(str(workspace / "Combined report.pdf"))
            if screenshot:
                dialog.tree.clearSelection()
                dialog.tree.setCurrentItem(dialog.tree_items[group.uid])
                dialog.tree.scrollToTop()
                app.processEvents()
                assert dialog.grab().save(str(screenshot))
            states, bookmarks = dialog.source_states(), dialog.plan.bookmarks()
            dialog.validate_and_accept()
            wait_for(lambda: not dialog.jobs)
            assert dialog.result() == QDialog.DialogCode.Accepted
            result, errors = [], []
            writer = worker_factory(dialog.source_paths(), dialog.output_path(), states, desired, bookmarks)
            writer.completed.connect(result.append)
            writer.failed.connect(errors.append)
            writer.cancelled.connect(lambda: errors.append("Unexpected cancellation"))
            writer.run()
            assert not errors and len(result) == 1, errors
            with fitz.open(dialog.output_path()) as output:
                assert output.page_count == len(desired)
                assert output.get_toc() == bookmarks
                for index, ref in enumerate(desired):
                    with fitz.open(ref.source) as source:
                        assert output[index].get_text() == source[ref.index].get_text()
        finally:
            dialog.close()
            wait_for(lambda: not dialog.jobs)
            dialog.deleteLater()
            app.processEvents()
    return 0
