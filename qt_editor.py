"""Page-oriented editor UI. PDF parsing/rendering is delegated to a worker factory."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import uuid

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QInputDialog,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from pdf_editor import TextEdit, TextStyle, nearest_text_region, parse_page_ranges, format_page_ranges, validate_deleted_pages, PDFEditError


class EditHistory:
    def __init__(self):
        self.edits = ()
        self.deleted_pages = frozenset()
        self.undo_stack = []
        self.redo_stack = []

    def put(self, edit):
        present = any(previous.key == edit.key for previous in self.edits)
        after = tuple(edit if previous.key == edit.key else previous for previous in self.edits) if present else self.edits + (edit,)
        if after == self.edits:
            return False
        self._checkpoint()
        self.edits = after
        return True

    def _checkpoint(self):
        self.undo_stack.append((self.edits, self.deleted_pages))
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()

    def set_deleted(self, pages):
        pages = frozenset(pages)
        if pages != self.deleted_pages:
            self._checkpoint()
            self.deleted_pages = pages

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append((self.edits, self.deleted_pages))
            self.edits, self.deleted_pages = self.undo_stack.pop()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append((self.edits, self.deleted_pages))
            self.edits, self.deleted_pages = self.redo_stack.pop()


class PageEditView(QGraphicsView):
    region_clicked = Signal(str)
    box_drawn = Signal(object)

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.drawing = False
        self.start_point = None
        self.rubber = None

    def set_drawing(self, enabled):
        self.drawing = enabled
        self.setDragMode(QGraphicsView.DragMode.NoDrag if enabled else QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if 0.1 <= self.transform().m11() * factor <= 10:
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_point = self.mapToScene(event.position().toPoint())
            if self.drawing:
                self.rubber = self.scene().addRect(QRectF(self.start_point, self.start_point), QPen(QColor("#635BFF"), 1))
                self.rubber.setZValue(10)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drawing and self.rubber is not None:
            self.rubber.setRect(QRectF(self.start_point, self.mapToScene(event.position().toPoint())).normalized())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.start_point is not None:
            point = self.mapToScene(event.position().toPoint())
            if self.drawing:
                rect = QRectF(self.start_point, point).normalized().intersected(self.sceneRect())
                if self.rubber is not None:
                    self.scene().removeItem(self.rubber)
                    self.rubber = None
                self.start_point = None
                if rect.width() >= 8 and rect.height() >= 8:
                    self.box_drawn.emit(rect)
                event.accept()
                return
            if (point - self.start_point).manhattanLength() < 4:
                for item in self.scene().items(point):
                    if isinstance(item.data(0), str):
                        self.region_clicked.emit(item.data(0))
                        break
            self.start_point = None
        super().mouseReleaseEvent(event)


class PDFEditorDialog(QDialog):
    def __init__(self, source, source_state, worker_factory, parent=None):
        super().__init__(parent)
        self.source, self.source_state = Path(source), source_state
        self.worker_factory = worker_factory
        self.layout_data = None
        self.history = EditHistory()
        self.saved_edits = ()
        self.saved_deleted_pages = frozenset()
        self.last_output = None
        self.load_after = False
        self.thread = self.worker = None
        self._operation = ""
        self._selected = None
        self._reference_style = TextStyle()
        self._region_key = None
        self._form_dirty = False
        self._filling = False
        self._closing = False
        self._close_confirmed = False
        self._preview_valid = False
        self._save_for_compression = False
        self._region_items = {}
        self._form_outline = None
        self.setWindowTitle("PDF 编辑 · 自动分区与文字匹配")
        self.resize(1320, 850)
        self.setMinimumSize(980, 640)
        self._build_ui()
        QTimer.singleShot(0, self._render)

    def _build_ui(self):
        root = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.page_number = QSpinBox()
        self.page_number.setRange(1, 1)
        self.page_number.setPrefix("第 ")
        self.page_number.setSuffix(" 页")
        self.page_number.valueChanged.connect(self._page_changed)
        toolbar.addWidget(self.page_number)
        self.page_count = QLabel("正在读取…")
        toolbar.addWidget(self.page_count)
        self.zones_button = QCheckBox("显示自动分区")
        self.zones_button.setChecked(True)
        self.zones_button.toggled.connect(self._toggle_zones)
        toolbar.addWidget(self.zones_button)
        self.add_button = QPushButton("框选新增文字")
        self.add_button.setCheckable(True)
        self.add_button.toggled.connect(self._drawing_toggled)
        toolbar.addWidget(self.add_button)
        self.delete_page_button = QPushButton("删除本页")
        self.delete_page_button.clicked.connect(self._delete_current_page)
        toolbar.addWidget(self.delete_page_button)
        self.manage_pages_button = QPushButton("页面管理…")
        self.manage_pages_button.clicked.connect(self._manage_pages)
        toolbar.addWidget(self.manage_pages_button)
        self.undo_button = QPushButton("撤销")
        self.undo_button.clicked.connect(lambda: self._history_action(False))
        toolbar.addWidget(self.undo_button)
        self.redo_button = QPushButton("重做")
        self.redo_button.clicked.connect(lambda: self._history_action(True))
        toolbar.addWidget(self.redo_button)
        fit = QPushButton("适应页面")
        fit.clicked.connect(self._fit)
        toolbar.addWidget(fit)
        toolbar.addStretch()
        root.addLayout(toolbar)

        splitter = QSplitter()
        root.addWidget(splitter, 1)
        self.scene = QGraphicsScene(self)
        self.view = PageEditView(self.scene)
        self.view.region_clicked.connect(self._select)
        self.view.box_drawn.connect(self._new_box)
        splitter.addWidget(self.view)
        panel_scroll = QScrollArea()
        panel_scroll.setWidgetResizable(True)
        panel_scroll.setMinimumWidth(320)
        panel = QWidget()
        side = QVBoxLayout(panel)
        intro = QLabel("点击文字分区修改；或框选空白区域新增。\n蓝色：原文字　绿色：草稿　灰色：图片区")
        intro.setWordWrap(True)
        side.addWidget(intro)
        self.region_list = QListWidget()
        self.region_list.setMinimumHeight(100)
        self.region_list.setMaximumHeight(120)
        self.region_list.currentItemChanged.connect(self._list_selected)
        side.addWidget(self.region_list)
        self.selection_label = QLabel("请选择文字分区")
        self.selection_label.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_label.setWordWrap(True)
        side.addWidget(self.selection_label)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("输入替换或新增的文字；删除原文字时留空。")
        self.text_edit.setMinimumHeight(120)
        self.text_edit.setMaximumHeight(150)
        self.text_edit.textChanged.connect(self._mark_dirty)
        side.addWidget(self.text_edit)

        self.reference_combo = QComboBox()
        self.reference_combo.currentIndexChanged.connect(self._reference_changed)
        side.addWidget(QLabel("匹配样式参考"))
        side.addWidget(self.reference_combo)
        self.font_label = QLabel("自动匹配原字体、字号与颜色")
        self.font_label.setTextFormat(Qt.TextFormat.PlainText)
        self.font_label.setWordWrap(True)
        side.addWidget(self.font_label)
        form = QFormLayout()
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4, 200)
        self.size_spin.setDecimals(2)
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(self._mark_dirty)
        form.addRow("字号", self.size_spin)
        self.leading_spin = QDoubleSpinBox()
        self.leading_spin.setRange(0.8, 3)
        self.leading_spin.setSingleStep(0.05)
        self.leading_spin.valueChanged.connect(self._mark_dirty)
        form.addRow("行距倍数", self.leading_spin)
        self.align_combo = QComboBox()
        self.align_combo.addItems(["左对齐", "居中", "右对齐"])
        self.align_combo.currentIndexChanged.connect(self._mark_dirty)
        form.addRow("对齐", self.align_combo)
        self.color_button = QPushButton("#000000")
        self.color_button.clicked.connect(self._choose_color)
        self._color = 0
        form.addRow("文字颜色", self.color_button)
        self.coordinates = []
        for label in ("X（pt）", "Y（pt）", "宽（pt）", "高（pt）"):
            spin = QDoubleSpinBox()
            spin.setRange(0, 100_000)
            spin.setDecimals(2)
            spin.valueChanged.connect(self._mark_dirty)
            self.coordinates.append(spin)
            form.addRow(label, spin)
        side.addLayout(form)
        self.apply_button = QPushButton("应用到草稿并预览")
        self.apply_button.clicked.connect(self._apply_current)
        note = QLabel("保持原字号，超出区域时会提示，不会自动缩字。混合字体区使用主要样式；扫描图、表格结构和竖排文字不属于本版直接编辑范围。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #71717A; font-size: 11px;")
        side.addWidget(note)
        side.addStretch()
        panel_scroll.setWidget(panel)
        splitter.addWidget(panel_scroll)
        splitter.setSizes([920, 350])

        self.status = QLabel("正在独立进程中分析页面…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        buttons = QHBoxLayout()
        hint = QLabel("原 PDF 不变 · 另存为副本 · 保存后可继续定容压缩")
        hint.setWordWrap(True)
        buttons.addWidget(hint, 1)
        buttons.addWidget(self.apply_button)
        self.cancel_button = QPushButton("取消后台任务")
        self.cancel_button.clicked.connect(self._cancel_job)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton("另存为 PDF")
        self.save_button.clicked.connect(lambda: self._save(False))
        buttons.addWidget(self.save_button)
        self.compress_button = QPushButton("保存并继续压缩")
        self.compress_button.clicked.connect(lambda: self._save(True))
        buttons.addWidget(self.compress_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        root.addLayout(buttons)
        self._set_controls(False)

    def _mark_dirty(self, *_args):
        if not self._filling and self._selected:
            self._form_dirty = True
            self._update_outline()
            self._set_controls(self.thread is not None)

    def _update_outline(self):
        if self.layout_data is None or self._selected is None:
            return
        rect = self._rect()
        transform = QTransform(*self.layout_data.rotation_matrix)
        box = transform.mapRect(QRectF(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]))
        if self._form_outline is None:
            self._form_outline = self.scene.addRect(box, QPen(QColor("#E49416"), 1.4))
            self._form_outline.setZValue(5)
        else:
            self._form_outline.setRect(box)

    def _set_controls(self, busy):
        ready = self.layout_data is not None and not busy and not self._closing
        for widget in (self.page_number, self.add_button, self.region_list, self.view):
            widget.setEnabled(ready)
        deleted = ready and self.layout_data.page_index in self.history.deleted_pages
        self.delete_page_button.setEnabled(ready)
        self.delete_page_button.setText("恢复本页" if deleted else "删除本页")
        self.manage_pages_button.setEnabled(ready)
        self.add_button.setEnabled(ready and not deleted)
        can_edit = ready and not deleted and self._selected is not None and (self._region_key is None or any(region.key == self._region_key and region.editable for region in self.layout_data.regions))
        for widget in (self.text_edit, self.reference_combo, self.size_spin, self.leading_spin, self.align_combo, self.color_button, self.apply_button, *self.coordinates):
            widget.setEnabled(can_edit)
        self.undo_button.setEnabled(ready and bool(self.history.undo_stack))
        self.redo_button.setEnabled(ready and bool(self.history.redo_stack))
        self.save_button.setEnabled(ready and bool(self.history.edits or self.history.deleted_pages or self._form_dirty))
        self.compress_button.setEnabled(self.save_button.isEnabled())
        self.cancel_button.setEnabled(busy and not self._closing)

    def _start_job(self, mode, destination=None):
        if self.thread is not None:
            return
        self._operation = mode
        self.status.setStyleSheet("")
        self.status.setText("正在生成实际 PDF 排版预览…" if mode == "preview" else "正在验证全部修改并另存为 PDF…")
        self.thread = QThread(self)
        self.worker = self.worker_factory(mode, self.source, self.page_number.value() - 1, self.history.edits, self.source_state, destination, self.history.deleted_pages)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(lambda _value, message: self.status.setText(message))
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        for signal in (self.worker.completed, self.worker.failed, self.worker.cancelled):
            signal.connect(self.thread.quit)
            signal.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._job_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self._set_controls(True)
        self.thread.start()

    def _render(self):
        if not self._closing:
            self._start_job("preview")

    def _completed(self, result):
        if self._closing:
            return
        if self._operation == "save":
            self.saved_edits = self.history.edits
            self.saved_deleted_pages = self.history.deleted_pages
            self.last_output = result.output_path
            self.status.setText(f"已保存 {result.edit_count} 处文字修改、删除 {len(result.deleted_pages)} 页，剩余 {result.page_count} 页：{result.output_path}\n" + "\n".join(result.warnings))
            if self._save_for_compression:
                self.load_after = True
                self._close_confirmed = True
                self._closing = True
            return
        layout, png, warnings = result
        first_page = self.layout_data is None or self.layout_data.page_index != layout.page_index
        self.layout_data = layout
        self.page_number.blockSignals(True)
        self.page_number.setMaximum(layout.page_count)
        self.page_number.setValue(layout.page_index + 1)
        self.page_number.blockSignals(False)
        self.page_count.setText(f"/ 原 {layout.page_count} 页 · 保留 {layout.page_count - len(self.history.deleted_pages)} 页")
        pixmap = QPixmap()
        if not pixmap.loadFromData(png, "PNG"):
            self._failed("预览图片解码失败。")
            return
        self.scene.clear()
        self._form_outline = None
        self._region_items.clear()
        item = self.scene.addPixmap(pixmap)
        box = QRectF(layout.display_rect[0], layout.display_rect[1], layout.display_rect[2] - layout.display_rect[0], layout.display_rect[3] - layout.display_rect[1])
        item.setTransform(QTransform.fromScale(box.width() / pixmap.width(), box.height() / pixmap.height()))
        self.scene.setSceneRect(box)
        self._populate_regions()
        self._update_outline()
        if layout.page_index in self.history.deleted_pages:
            marker = self.scene.addText("此页已标记删除")
            marker.setDefaultTextColor(QColor("#C03535"))
            marker.setScale(2)
            marker.setPos(20, 12)
            marker.setZValue(8)
        if first_page:
            self._fit()
        self._preview_valid = True
        detail = "\n".join(warnings)
        self.status.setText(f"原第 {layout.page_index + 1} 页 · {len(layout.regions)} 个分区 · {len(self.history.edits)} 处文字草稿 · 待删除 {len(self.history.deleted_pages)} 页" + ("\n" + detail if detail else ""))

    def _failed(self, message):
        if not self._closing:
            if self._operation == "preview":
                self._preview_valid = False
                message = "预览未更新（保留上次成功画面）：" + message
                self._restore_page_number()
            self.status.setStyleSheet("color: #C03535;")
            self.status.setText(message)

    def _cancelled(self):
        if not self._closing:
            if self._operation == "preview":
                self._preview_valid = False
                self._restore_page_number()
            self.status.setText("任务已取消；原文件和已有输出未被替换。可调整草稿后再次预览或保存。")

    def _restore_page_number(self):
        if self.layout_data is not None:
            self.page_number.blockSignals(True)
            self.page_number.setValue(self.layout_data.page_index + 1)
            self.page_number.blockSignals(False)

    def _job_finished(self):
        self.thread = self.worker = None
        self._set_controls(False)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def _populate_regions(self):
        self.region_list.blockSignals(True)
        self.region_list.clear()
        self.reference_combo.blockSignals(True)
        self.reference_combo.clear()
        self.reference_combo.addItem("最近文字（自动匹配）", None)
        pending = {edit.region_key: edit for edit in self.history.edits if edit.page_index == self.layout_data.page_index and edit.region_key}
        transform = QTransform(*self.layout_data.rotation_matrix)
        def add(key, rect, text, color, z):
            x0, y0, x1, y1 = rect
            shape = self.scene.addRect(transform.mapRect(QRectF(x0, y0, x1 - x0, y1 - y0)), QPen(QColor(color), 0.8))
            shape.setData(0, key)
            shape.setZValue(z)
            shape.setVisible(self.zones_button.isChecked())
            self._region_items[key] = shape
            entry = QListWidgetItem(text)
            entry.setData(Qt.ItemDataRole.UserRole, key)
            self.region_list.addItem(entry)
            if key == self._selected:
                self.region_list.setCurrentItem(entry)
        for region in self.layout_data.regions:
            edit = pending.get(region.key)
            label = "图片（只读）" if region.kind == "image" else ((edit.text if edit else region.text).replace("\n", " ")[:40] or "已删除文字")
            add(region.key, edit.rect if edit else region.rect, ("[草稿] " if edit else "") + label, "#199B62" if edit else ("#635BFF" if region.kind == "text" else "#A1A1AA"), 3 if edit else (2 if region.kind == "text" else 1))
            if region.kind == "text" and region.editable:
                self.reference_combo.addItem(region.text.replace("\n", " ")[:32], region.key)
        for edit in self.history.edits:
            if edit.page_index == self.layout_data.page_index and edit.region_key is None:
                add(edit.key, edit.rect, "[新增] " + edit.text[:35], "#199B62", 3)
        self.region_list.blockSignals(False)
        self.reference_combo.blockSignals(False)

    def _fit(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _toggle_zones(self, enabled):
        for item in self._region_items.values():
            item.setVisible(enabled)

    def _drawing_toggled(self, enabled):
        self.view.set_drawing(enabled)
        if enabled:
            self.status.setText("在页面拖出一个矩形。新增文本会匹配同栏附近文字；可在右侧更换参考区。")

    def _discard_form(self):
        if not self._form_dirty:
            return True
        answer = QMessageBox.question(self, "尚未应用的文字", "文本框内的修改还没有应用到草稿。放弃这部分输入吗？\n选择“否”可返回并点击“应用到草稿并预览”。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._form_dirty = False
            return True
        return False

    def _list_selected(self, current, _previous):
        if current:
            self._select(current.data(Qt.ItemDataRole.UserRole))

    def _select(self, key):
        if self.thread or not self.layout_data or key == self._selected:
            return
        if not self._discard_form():
            self.region_list.blockSignals(True)
            for index in range(self.region_list.count()):
                item = self.region_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == self._selected:
                    self.region_list.setCurrentItem(item)
                    break
            self.region_list.blockSignals(False)
            return
        edit = next((edit for edit in self.history.edits if edit.key == key), None)
        region = next((region for region in self.layout_data.regions if region.key == key), None)
        if region is None and edit is None:
            return
        self._selected = key
        self._region_key = edit.region_key if edit else region.key
        self._fill(edit or TextEdit(key, self.layout_data.page_index, key, region.rect, region.text, region.style))
        self.selection_label.setText(region.note or "修改当前文字区；留空并应用可删除原文字。" if region else "新增文字：已自动匹配附近样式。")
        self._set_controls(False)

    def _fill(self, edit):
        self._filling = True
        self.text_edit.setPlainText(edit.text)
        x0, y0, x1, y1 = edit.rect
        for spin, value in zip(self.coordinates, (x0, y0, x1 - x0, y1 - y0)):
            spin.setValue(value)
        self._fill_style(edit.style)
        self._filling = False
        self._form_dirty = False
        self._update_outline()

    def _fill_style(self, style):
        was_filling = self._filling
        self._filling = True
        self._reference_style = style
        self.font_label.setText(f"{style.font_name} · 优先复用原字体；缺字时会提示替代")
        self.size_spin.setValue(style.font_size)
        self.leading_spin.setValue(style.line_height)
        self.align_combo.setCurrentIndex(style.alignment)
        self._color = style.color
        self.color_button.setText(f"#{self._color:06X}")
        self._filling = was_filling

    def _rect(self):
        x, y, width, height = (spin.value() for spin in self.coordinates)
        return x, y, x + width, y + height

    def _new_box(self, display_box):
        if not self.layout_data or self.thread or not self._discard_form():
            return
        inverse, valid = QTransform(*self.layout_data.rotation_matrix).inverted()
        if not valid:
            return
        rect = inverse.mapRect(display_box)
        coordinates = (rect.left(), rect.top(), rect.right(), rect.bottom())
        reference = nearest_text_region(self.layout_data, coordinates)
        key = "new:" + uuid.uuid4().hex
        self._selected, self._region_key = key, None
        self._fill(TextEdit(key, self.layout_data.page_index, None, coordinates, "", reference.style if reference else TextStyle()))
        self.selection_label.setText("新增文字 · 参考：" + (reference.text.replace("\n", " ")[:35] if reference else "未找到文字层，使用默认样式"))
        self.add_button.setChecked(False)
        self._set_controls(False)
        self.text_edit.setFocus()

    def _reference_changed(self, _index):
        if self._filling or not self.layout_data or not self._selected:
            return
        key = self.reference_combo.currentData()
        region = next((region for region in self.layout_data.regions if region.key == key), None) if key else nearest_text_region(self.layout_data, self._rect())
        if region:
            self._fill_style(region.style)
            self._mark_dirty()

    def _choose_color(self):
        color = QColorDialog.getColor(QColor(f"#{self._color:06x}"), self, "匹配文字颜色")
        if color.isValid():
            self._color = color.rgb() & 0xFFFFFF
            self.color_button.setText(f"#{self._color:06X}")
            self._mark_dirty()

    def _current_edit(self):
        style = replace(self._reference_style, font_size=self.size_spin.value(), line_height=self.leading_spin.value(), alignment=self.align_combo.currentIndex(), color=self._color)
        return TextEdit(self._selected, self.layout_data.page_index, self._region_key, self._rect(), self.text_edit.toPlainText(), style)

    def _apply_current(self):
        if not self._selected or self.thread:
            return
        edit = self._current_edit()
        if edit.region_key is None and not edit.text.strip():
            self.status.setText("请先输入新增文字。")
            return
        self.history.put(edit)
        self._form_dirty = False
        self._render()

    def _history_action(self, redo):
        if self.thread or not self._discard_form():
            return
        self.history.redo() if redo else self.history.undo()
        self._selected = self._region_key = None
        self._filling = True
        self.text_edit.clear()
        self._filling = False
        self._render()

    def _page_changed(self, number):
        if not self.layout_data or self.thread:
            return
        if not self._discard_form():
            self.page_number.blockSignals(True)
            self.page_number.setValue(self.layout_data.page_index + 1)
            self.page_number.blockSignals(False)
            return
        self._selected = self._region_key = None
        self._render()

    def _set_deleted_pages(self, pages):
        try:
            pages = validate_deleted_pages(self.layout_data.page_count, pages)
        except PDFEditError as exc:
            QMessageBox.warning(self, "无法删除页面", str(exc))
            return
        if not self._discard_form():
            return
        self.history.set_deleted(pages)
        self._selected = self._region_key = None
        self.add_button.setChecked(False)
        self._render()

    def _delete_current_page(self):
        if self.layout_data is not None and self.thread is None:
            self._set_deleted_pages(self.history.deleted_pages ^ {self.layout_data.page_index})

    def _manage_pages(self):
        if self.layout_data is None or self.thread is not None:
            return
        text, accepted = QInputDialog.getText(
            self, "页面管理 · 使用原始页码",
            f"共 {self.layout_data.page_count} 页。输入要删除的原始页码，例如 2, 4-6。\n删除后其他页仍使用原页码显示，避免连续删除时选错页。\n清空列表可恢复全部页面；所有操作可撤销，另存为时才生效。\n指向被删页的链接会移除，对应书签会停用。",
            text=format_page_ranges(self.history.deleted_pages),
        )
        if accepted:
            try:
                pages = parse_page_ranges(text, self.layout_data.page_count)
            except PDFEditError as exc:
                QMessageBox.warning(self, "页码无效", str(exc))
                return
            self._set_deleted_pages(pages)

    def _save(self, compress):
        if self.thread:
            return
        if self._form_dirty:
            self._apply_current()
            QMessageBox.information(self, "先预览再保存", "已将输入应用到草稿。请确认预览，再点击保存。")
            return
        if not self.history.edits and not self.history.deleted_pages:
            return
        if not self._preview_valid:
            self.status.setText("请先修正排版错误并成功预览，再保存。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "另存为编辑后的 PDF", str(self.last_output or self.source.with_name(self.source.stem + "_edited.pdf")), "PDF 文件 (*.pdf)")
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != ".pdf":
            destination = destination.with_suffix(".pdf")
        if destination.resolve() == self.source.resolve():
            QMessageBox.warning(self, "保留原文件", "请选择不同的文件名；编辑器不会覆盖源 PDF。")
            return
        self._save_for_compression = compress
        self._start_job("save", destination)

    def _cancel_job(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("正在安全取消后台编辑任务…")

    def reject(self):
        # Escape must use the same cancellation/unsaved-draft path as window close.
        self.close()

    def closeEvent(self, event):
        if not self._close_confirmed and (self._form_dirty or self.history.edits != self.saved_edits or self.history.deleted_pages != self.saved_deleted_pages):
            answer = QMessageBox.question(self, "未保存的编辑", "还有未保存的修改。放弃修改并关闭编辑器吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._close_confirmed = True
        self._closing = True
        if self.worker:
            self.worker.cancel()
        if self.thread is not None:
            self._set_controls(True)
            self.status.setText("正在结束后台编辑进程…")
            event.ignore()
            return
        event.accept()


def editor_self_test(worker_factory, app_style):
    """Exercise the real editor, spawned preview, writer and close path when frozen."""
    import os
    import tempfile
    import time
    import pymupdf as fitz
    from PySide6.QtGui import QFontDatabase, QFont
    from PySide6.QtWidgets import QApplication
    from compressor import get_pdf_source_state, compress_pdf

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    # Windows' offscreen Qt plugin does not populate the system font database.
    # Register known local UI fonts for legible diagnostic screenshots only.
    for name in ("segoeui.ttf", "msyh.ttc"):
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei", 9))
    app.setStyle("Fusion")
    app.setStyleSheet(app_style)

    def wait(dialog):
        deadline = time.monotonic() + 30
        while dialog.thread is not None or dialog.layout_data is None:
            app.processEvents()
            if time.monotonic() > deadline:
                raise RuntimeError("Editor self-test timed out")
            time.sleep(0.01)
        app.processEvents()

    with tempfile.TemporaryDirectory(prefix="pdf-editor-self-test-") as directory:
        source, output = Path(directory) / "source.pdf", Path(directory) / "edited.pdf"
        with fitz.open() as document:
            page = document.new_page(width=540, height=660)
            page.insert_text((38, 60), "PDF editing workspace", fontname="hebo", fontsize=22, color=(0.2, 0.2, 0.5))
            page.insert_text((38, 110), "Left column paragraph\nSecond line of body text", fontsize=12)
            page.insert_text((295, 110), "Right column paragraph\nKeep its original layout", fontsize=12)
            page.draw_rect((38, 190, 250, 310), fill=(0.9, 0.94, 1), color=(0.5, 0.6, 0.9))
            document.new_page().insert_text((40, 80), "Page to delete")
            document.new_page().insert_text((40, 80), "Last page is kept")
            document.save(source)
        dialog = PDFEditorDialog(source, get_pdf_source_state(source), worker_factory)
        try:
            dialog.show()
            app.processEvents()
            wait(dialog)
            print("Editor self-test: initial preview ready", flush=True)
            if not dialog._preview_valid:
                return 20
            first = dialog.layout_data.regions[0]
            dialog._select(first.key)
            dialog.text_edit.setPlainText("Edited PDF workspace")
            dialog._apply_current()
            wait(dialog)
            print("Editor self-test: replacement preview ready", flush=True)
            if not dialog._preview_valid:
                return 21
            dialog._new_box(QRectF(38, 350, 370, 70))
            dialog.text_edit.setPlainText("新增中文说明 / New matching text")
            dialog.size_spin.setValue(12)
            dialog._apply_current()
            wait(dialog)
            print("Editor self-test: addition preview ready", flush=True)
            if not dialog._preview_valid:
                return 22
            # Optional screenshot for visual verification; no document is uploaded.
            screenshot = os.environ.get("PDF_EDITOR_TEST_SCREENSHOT")
            if screenshot:
                dialog.grab().save(screenshot)
            dialog._set_deleted_pages({1})
            wait(dialog)
            print("Editor self-test: deletion draft ready", flush=True)
            dialog._save_for_compression = True
            dialog._start_job("save", output)
            wait(dialog)
            print("Editor self-test: edited copy saved", flush=True)
            if dialog.last_output is None or dialog.last_output.resolve() != output.resolve() or not dialog.load_after:
                print("Editor self-test save failure: " + dialog.status.text(), flush=True)
                return 23
            with fitz.open(output) as document:
                if document.page_count != 2 or "Edited PDF workspace" not in document[0].get_text() or "新增中文说明" not in document[0].get_text() or "Last page is kept" not in document[1].get_text():
                    return 24
            compressed = Path(directory) / "compressed.pdf"
            print("Editor self-test: checking compression handoff", flush=True)
            result = compress_pdf(output, compressed, output.stat().st_size + 1024)
            if not result.output_path.exists():
                return 25
        finally:
            dialog._close_confirmed = True
            dialog.close()
            if dialog.thread is not None:
                wait(dialog)
            dialog.deleteLater()
            app.processEvents()
    print("Editor self-test: passed", flush=True)
    return 0
