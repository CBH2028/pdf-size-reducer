"""Apple-inspired Qt 6 interface for the PDF size reducer."""

from __future__ import annotations

import os
import re
import sys
import threading
import math
import multiprocessing
import queue
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from compressor import (
    CompressionCancelled,
    CompressionError,
    CompressionResult,
    NoCompressibleImagesError,
    PDFAsset,
    TargetTooSmallError,
    compress_pdf,
    format_bytes,
    list_pdf_assets,
    render_asset_image,
    render_asset_thumbnail,
)


APP_NAME = "PDF 定容压缩工具"
APP_VERSION = "3.2"
ACCENT = "#5E5CE6"
ACCENT_HOVER = "#4F46D5"
TEXT = "#1D1D1F"
MUTED = "#6E6E73"
BACKGROUND = "#F5F5F7"
CARD = "#FFFFFF"
BORDER = "#E5E5EA"
SUCCESS = "#248A3D"
ERROR = "#D70015"


APP_STYLE = f"""
* {{
    font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Segoe UI";
    color: {TEXT};
    outline: none;
}}
QMainWindow, QWidget#root {{
    background: {BACKGROUND};
}}
QFrame#topBar {{
    background: rgba(255, 255, 255, 245);
    border-bottom: 1px solid {BORDER};
}}
QLabel#appTitle {{
    font-size: 21px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#appSubtitle {{
    font-size: 12px;
    color: {MUTED};
}}
QLabel#versionPill {{
    color: {ACCENT};
    background: #F0EFFF;
    border-radius: 10px;
    padding: 5px 10px;
    font-size: 10px;
    font-weight: 700;
}}
QFrame[card="true"] {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 18px;
}}
QLabel[step="true"] {{
    color: {ACCENT};
    font-size: 11px;
    font-weight: 700;
}}
QLabel[title="true"] {{
    color: {TEXT};
    font-size: 17px;
    font-weight: 700;
}}
QLabel[secondary="true"] {{
    color: {MUTED};
    font-size: 11px;
}}
QLabel#selectionInfo {{
    color: {ACCENT};
    background: #F3F2FF;
    border-radius: 9px;
    padding: 8px 10px;
    font-size: 10px;
    font-weight: 600;
}}
QPushButton {{
    min-height: 34px;
    padding: 0 14px;
    border-radius: 10px;
    border: 1px solid {BORDER};
    background: #F9F9FB;
    color: {TEXT};
    font-size: 11px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: #F0F0F4;
    border-color: #D1D1D6;
}}
QPushButton:pressed {{
    background: #E8E8ED;
}}
QPushButton:disabled {{
    color: #AEAEB2;
    background: #F5F5F7;
}}
QPushButton#primaryButton {{
    min-height: 46px;
    border: none;
    border-radius: 12px;
    background: {ACCENT};
    color: white;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#primaryButton:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primaryButton:pressed {{ background: #3F3DB8; }}
QPushButton#primaryButton:disabled {{ background: #C7C7E9; color: white; }}
QLineEdit, QComboBox {{
    min-height: 39px;
    border: 1px solid #D8D8DC;
    border-radius: 10px;
    background: #FAFAFC;
    padding: 0 12px;
    font-size: 12px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{
    border: 2px solid {ACCENT};
    background: white;
}}
QComboBox::drop-down {{
    border: none;
    width: 28px;
}}
QComboBox QAbstractItemView {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background: white;
    selection-background-color: #EEEDFF;
    selection-color: {TEXT};
    padding: 5px;
}}
QProgressBar {{
    height: 7px;
    border: none;
    border-radius: 3px;
    background: #ECECF0;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 3px;
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 4px 1px;
}}
QScrollBar::handle:vertical {{
    background: #C7C7CC;
    border-radius: 4px;
    min-height: 35px;
}}
QScrollBar::handle:vertical:hover {{ background: #AEAEB2; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QCheckBox {{
    spacing: 8px;
    font-size: 11px;
    font-weight: 600;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: 1px solid #C7C7CC;
    border-radius: 5px;
    background: white;
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: none;
}}
QFrame[assetCard="true"] {{
    background: white;
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QFrame[assetCard="true"]:hover {{
    border: 1px solid #C9C7F7;
    background: #FEFEFF;
}}
QLabel#previewImage {{
    background: #F7F7F9;
    border: 1px solid #EFEFF1;
    border-radius: 10px;
}}
QLabel#typePill {{
    color: {ACCENT};
    background: #F0EFFF;
    border-radius: 8px;
    padding: 4px 7px;
    font-size: 8px;
    font-weight: 700;
}}
QLabel#sizePill {{
    color: {ACCENT};
    background: #F3F2FF;
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 9px;
    font-weight: 600;
}}
QLabel#zoomHint {{ color: {MUTED}; font-size: 9px; }}
QFrame#readingGlass {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 rgba(248, 248, 255, 248),
        stop: 0.55 rgba(255, 255, 255, 252),
        stop: 1 rgba(244, 243, 255, 248)
    );
    border: 1px solid #E6E4FF;
    border-radius: 24px;
}}
QLabel#readingEyebrow {{
    color: {ACCENT};
    font-size: 10px;
    font-weight: 750;
    letter-spacing: 1px;
}}
QLabel#readingTitle {{
    color: {TEXT};
    font-size: 22px;
    font-weight: 750;
}}
QLabel#readingStage {{
    color: {TEXT};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#readingTip {{
    color: {MUTED};
    font-size: 10px;
}}
QLabel#readingMeta {{
    color: {SUCCESS};
    background: #EEF8F0;
    border-radius: 9px;
    padding: 6px 10px;
    font-size: 9px;
    font-weight: 650;
}}
QPushButton#scanCancelButton {{
    min-height: 31px;
    color: {MUTED};
    background: rgba(255, 255, 255, 185);
    border: 1px solid #DDDBF5;
    border-radius: 10px;
    padding: 0 16px;
}}
QPushButton#scanCancelButton:hover {{
    color: {ERROR};
    background: #FFF3F4;
    border-color: #F1C5CA;
}}
QPushButton#scanCancelButton:disabled {{
    color: #AEAEB2;
    background: #F4F4F7;
}}
QProgressBar#readingProgress {{
    height: 8px;
    border: none;
    border-radius: 4px;
    background: #EAE9F4;
}}
QProgressBar#readingProgress::chunk {{
    border-radius: 4px;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #7D7AFF,
        stop: 0.52 #5E5CE6,
        stop: 1 #AF8BFF
    );
}}
QFrame#toast {{
    background: rgba(29, 29, 31, 238);
    border-radius: 13px;
}}
QFrame#toast QLabel {{ color: white; font-size: 11px; font-weight: 600; }}
QDialog {{ background: {BACKGROUND}; }}
QGraphicsView {{
    background: #EAEAEE;
    border: none;
    border-radius: 14px;
}}
"""


def make_app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(QRectF(4, 4, 56, 56), 15, 15)
    painter.setPen(QColor("white"))
    font = QFont("Segoe UI", 12, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "PDF")
    painter.end()
    return QIcon(pixmap)


def add_shadow(widget: QWidget, blur: int = 24, y_offset: int = 6) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y_offset)
    shadow.setColor(QColor(0, 0, 0, 22))
    widget.setGraphicsEffect(shadow)


class AnimatedProgressBar(QProgressBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextVisible(False)
        self._animation = QPropertyAnimation(self, b"value", self)
        self._animation.setDuration(260)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def set_smooth_value(self, value: int) -> None:
        self._animation.stop()
        self._animation.setStartValue(self.value())
        self._animation.setEndValue(max(0, min(100, value)))
        self._animation.start()


class WarmLoadingOrb(QWidget):
    """A calm, playful loading mark drawn at a smooth 60 FPS."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(126, 126)
        self._clock = QElapsedTimer()
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.start()

    def start(self) -> None:
        self._clock.restart()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._phase = self._clock.elapsed() / 1000.0
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center_x = self.width() / 2
        center_y = self.height() / 2
        breath = (math.sin(self._phase * 2.25) + 1.0) / 2.0

        # A soft breathing halo keeps the mark alive without looking busy.
        for index in range(4, 0, -1):
            radius = 31 + index * 6 + breath * 3
            alpha = max(5, 24 - index * 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(94, 92, 230, alpha))
            painter.drawEllipse(
                QRectF(
                    center_x - radius,
                    center_y - radius,
                    radius * 2,
                    radius * 2,
                )
            )

        orbit_rect = QRectF(center_x - 45, center_y - 45, 90, 90)
        orbit_pen = QPen(QColor(94, 92, 230, 48), 1.5)
        orbit_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(orbit_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        start_angle = int((-self._phase * 48) % 360 * 16)
        painter.drawArc(orbit_rect, start_angle, 212 * 16)

        # Three unequal satellites make the motion feel organic rather than mechanical.
        satellites = (
            (0.0, 45.0, 4.2, QColor(94, 92, 230)),
            (2.25, 38.0, 3.2, QColor(175, 139, 255)),
            (4.35, 49.0, 2.7, QColor(82, 196, 255)),
        )
        for offset, radius, dot_size, color in satellites:
            angle = self._phase * (1.35 + radius / 155.0) + offset
            x = center_x + math.cos(angle) * radius
            y = center_y + math.sin(angle) * radius
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x - dot_size, y - dot_size, dot_size * 2, dot_size * 2))

        tile_rect = QRectF(center_x - 25, center_y - 25, 50, 50)
        tile_gradient = QRadialGradient(center_x - 8, center_y - 10, 56)
        tile_gradient.setColorAt(0.0, QColor("#8583FF"))
        tile_gradient.setColorAt(1.0, QColor(ACCENT))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tile_gradient)
        painter.drawRoundedRect(tile_rect, 15, 15)

        # A tiny paper glyph keeps the animation related to the task.
        paper = QRectF(center_x - 10, center_y - 13, 20, 26)
        painter.setBrush(QColor(255, 255, 255, 245))
        painter.drawRoundedRect(paper, 3.2, 3.2)
        line_pen = QPen(QColor(94, 92, 230, 180), 1.6)
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(line_pen)
        for line_y, width in ((center_y - 5, 11), (center_y, 13), (center_y + 5, 8)):
            painter.drawLine(
                QPoint(int(center_x - 6), int(line_y)),
                QPoint(int(center_x - 6 + width), int(line_y)),
            )
        painter.end()


class ReadingPanel(QWidget):
    """Friendly, truthful progress feedback while a PDF is being inspected."""

    cancel_requested = Signal()

    _TIPS = (
        "我在分辨文字、位图和矢量线条，页面本身不会被改动。",
        "正在把分散的图形对象重新理解为完整 Figure。",
        "稍后可以逐张预览，再决定哪些内容需要压缩。",
        "复杂论文会多花一点时间，但窗口仍然可以正常移动。",
    )

    def __init__(self, file_name: str, file_size: str) -> None:
        super().__init__()
        self.setMinimumHeight(470)
        self._tip_index = 0
        self._tip_animation: QPropertyAnimation | None = None
        self._elapsed_clock = QElapsedTimer()
        self._elapsed_clock.start()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 30)
        outer.addStretch(1)

        glass = QFrame()
        glass.setObjectName("readingGlass")
        glass.setMaximumWidth(570)
        glass.setMinimumWidth(430)
        add_shadow(glass, 34, 8)
        content = QVBoxLayout(glass)
        content.setContentsMargins(40, 28, 40, 28)
        content.setSpacing(10)

        self.orb = WarmLoadingOrb()
        content.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignHCenter)
        self.eyebrow = QLabel("正在打开 · 2%")
        self.eyebrow.setObjectName("readingEyebrow")
        self.eyebrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.eyebrow)
        title = QLabel("正在读懂这篇论文")
        title.setObjectName("readingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(title)
        file_label = QLabel(f"{file_name}  ·  {file_size}")
        file_label.setProperty("secondary", True)
        file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        file_label.setWordWrap(True)
        content.addWidget(file_label)
        content.addSpacing(8)

        self.progress = AnimatedProgressBar()
        self.progress.setObjectName("readingProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(2)
        content.addWidget(self.progress)
        self.stage = QLabel("正在验证 PDF 并读取目录…")
        self.stage.setObjectName("readingStage")
        self.stage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stage.setWordWrap(True)
        content.addWidget(self.stage)

        self.tip = QLabel(self._TIPS[0])
        self.tip.setObjectName("readingTip")
        self.tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tip.setWordWrap(True)
        self._tip_opacity = QGraphicsOpacityEffect(self.tip)
        self.tip.setGraphicsEffect(self._tip_opacity)
        content.addWidget(self.tip)

        self.meta = QLabel("已用 0.0 秒  ·  界面响应正常")
        self.meta.setObjectName("readingMeta")
        self.meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignHCenter)

        self.cancel_button = QPushButton("停止读取")
        self.cancel_button.setObjectName("scanCancelButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(
            lambda _checked=False: self.cancel_requested.emit()
        )
        content.addWidget(
            self.cancel_button, 0, Qt.AlignmentFlag.AlignHCenter
        )

        outer.addWidget(glass, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        self._tip_timer = QTimer(self)
        self._tip_timer.setInterval(2600)
        self._tip_timer.timeout.connect(self._change_tip)
        self._tip_timer.start()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(200)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start()

    def update_progress(self, value: int, message: str) -> None:
        value = max(self.progress.value(), max(0, min(100, value)))
        self.progress.set_smooth_value(value)
        self.eyebrow.setText(f"正在分析 · {value}%")
        self.stage.setText(message)

    def complete(self, asset_count: int) -> None:
        self.progress.set_smooth_value(100)
        self.eyebrow.setText("分析完成 · 100%")
        self.stage.setText(f"找到了 {asset_count} 个可预览图形，正在为你铺开…")
        self.meta.setText(
            f"读取用时 {self._elapsed_seconds():.1f} 秒  ·  即将显示预览"
        )
        self.cancel_button.setText("读取完成")
        self.cancel_button.setEnabled(False)
        self._tip_timer.stop()
        self._elapsed_timer.stop()

    def mark_cancelling(self) -> None:
        self.stage.setText("正在停在一个安全的位置…")
        self.meta.setText("取消请求已收到  ·  不会生成或修改文件")
        self.cancel_button.setText("正在停止…")
        self.cancel_button.setEnabled(False)

    def stop(self) -> None:
        self.orb.stop()
        self._tip_timer.stop()
        self._elapsed_timer.stop()
        if self._tip_animation:
            self._tip_animation.stop()

    def _elapsed_seconds(self) -> float:
        return max(0, self._elapsed_clock.elapsed()) / 1000.0

    def _update_elapsed(self) -> None:
        self.meta.setText(
            f"已用 {self._elapsed_seconds():.1f} 秒  ·  界面响应正常"
        )

    def _change_tip(self) -> None:
        self._tip_animation = QPropertyAnimation(
            self._tip_opacity, b"opacity", self
        )
        self._tip_animation.setDuration(220)
        self._tip_animation.setStartValue(self._tip_opacity.opacity())
        self._tip_animation.setEndValue(0.0)
        self._tip_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def reveal_next() -> None:
            self._tip_index = (self._tip_index + 1) % len(self._TIPS)
            self.tip.setText(self._TIPS[self._tip_index])
            fade_in = QPropertyAnimation(self._tip_opacity, b"opacity", self)
            fade_in.setDuration(280)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._tip_animation = fade_in
            fade_in.start()

        self._tip_animation.finished.connect(reveal_next)
        self._tip_animation.start()


class ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class SmoothCheckBox(QCheckBox):
    """QSS-styled checkbox with a crisp high-DPI check mark."""

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = painter.pen()
        pen.setColor(QColor("white"))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(
            QPoint(indicator.left() + 4, indicator.center().y()),
            QPoint(indicator.left() + 7, indicator.bottom() - 4),
        )
        painter.drawLine(
            QPoint(indicator.left() + 7, indicator.bottom() - 4),
            QPoint(indicator.right() - 3, indicator.top() + 4),
        )
        painter.end()


class AssetCard(QFrame):
    preview_requested = Signal(object)
    selection_changed = Signal(str, bool)

    def __init__(self, asset: PDFAsset, selected: bool = True) -> None:
        super().__init__()
        self.asset = asset
        self._source_pixmap: QPixmap | None = None
        self.setProperty("assetCard", True)
        self.setMinimumWidth(285)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 11, 11, 11)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.checkbox = SmoothCheckBox(asset.display_name)
        self.checkbox.setChecked(selected)
        self.checkbox.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.checkbox.stateChanged.connect(self._selection_changed)
        header.addWidget(self.checkbox, 1)
        type_label = QLabel("FIGURE" if asset.kind == "figure" else "IMAGE")
        type_label.setObjectName("typePill")
        header.addWidget(type_label, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(header)

        self.preview = ClickableLabel("正在生成全景预览…")
        self.preview.setObjectName("previewImage")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(158)
        self.preview.setMaximumHeight(178)
        self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview.clicked.connect(lambda: self.preview_requested.emit(self.asset))
        layout.addWidget(self.preview)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        size_label = QLabel(asset.storage_label)
        size_label.setObjectName("sizePill")
        footer.addWidget(size_label)
        footer.addStretch(1)
        zoom = QLabel("点击放大  ↗")
        zoom.setObjectName("zoomHint")
        zoom.setCursor(Qt.CursorShape.PointingHandCursor)
        zoom.mousePressEvent = lambda _event: self.preview_requested.emit(  # type: ignore[method-assign]
            self.asset
        )
        footer.addWidget(zoom)
        layout.addLayout(footer)

    def _selection_changed(self, state: int) -> None:
        self.selection_changed.emit(
            self.asset.key, state == Qt.CheckState.Checked.value
        )

    def set_selected(self, selected: bool) -> None:
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(selected)
        self.checkbox.blockSignals(False)

    def set_thumbnail(self, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.preview.setText("预览加载失败")
            return
        self._source_pixmap = pixmap
        self._update_preview_pixmap()

    def set_thumbnail_error(self, message: str) -> None:
        self.preview.setText(f"预览失败\n{message}")
        self.preview.setStyleSheet(f"color: {ERROR};")

    def _update_preview_pixmap(self) -> None:
        if not self._source_pixmap:
            return
        target = QSize(max(100, self.preview.width() - 12), 166)
        self.preview.setPixmap(
            self._source_pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._update_preview_pixmap()


def _asset_scan_process(
    path: str,
    result_queue,
    cancel_event,
) -> None:
    """Inspect a PDF outside the GUI process so dense pages cannot hold its GIL."""
    try:
        assets, page_count = list_pdf_assets(
            Path(path),
            progress_callback=lambda value, message: result_queue.put(
                ("progress", value, message)
            ),
            cancel_event=cancel_event,
        )
    except CompressionCancelled:
        result_queue.put(("cancelled",))
    except Exception as exc:
        result_queue.put(("failed", str(exc)))
    else:
        result_queue.put(("completed", assets, page_count))


class AssetScanWorker(QObject):
    progress = Signal(int, int, str)
    completed = Signal(int, object, object, int)
    failed = Signal(int, object, str)
    cancelled = Signal(int, object)

    def __init__(self, path: Path, generation: int) -> None:
        super().__init__()
        self.path = path
        self.generation = generation
        self.cancel_event = threading.Event()
        self._process_cancel_event = None

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._process_cancel_event is not None:
            self._process_cancel_event.set()

    @Slot()
    def run(self) -> None:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process_cancel_event = context.Event()
        self._process_cancel_event = process_cancel_event
        process = context.Process(
            target=_asset_scan_process,
            args=(str(self.path), result_queue, process_cancel_event),
            name="PDFAssetScanner",
            daemon=True,
        )
        process.start()
        terminal_received = False
        cancel_started: float | None = None
        process_exited_at: float | None = None
        try:
            while not terminal_received:
                if self.cancel_event.is_set():
                    process_cancel_event.set()
                    if cancel_started is None:
                        cancel_started = time.monotonic()
                    elif process.is_alive() and time.monotonic() - cancel_started > 1:
                        process.terminate()
                        self.cancelled.emit(self.generation, self.path)
                        terminal_received = True
                        break
                try:
                    message = result_queue.get(timeout=0.05)
                except queue.Empty:
                    if not process.is_alive():
                        if process_exited_at is None:
                            process_exited_at = time.monotonic()
                        elif time.monotonic() - process_exited_at > 0.5:
                            if self.cancel_event.is_set():
                                self.cancelled.emit(self.generation, self.path)
                            else:
                                self.failed.emit(
                                    self.generation,
                                    self.path,
                                    "PDF 扫描进程意外结束。",
                                )
                            terminal_received = True
                    else:
                        process_exited_at = None
                    continue

                kind, *payload = message
                if kind == "progress":
                    value, text = payload
                    self.progress.emit(self.generation, int(value), str(text))
                elif kind == "completed":
                    assets, page_count = payload
                    self.completed.emit(
                        self.generation,
                        self.path,
                        assets,
                        int(page_count),
                    )
                    terminal_received = True
                elif kind == "cancelled":
                    self.cancelled.emit(self.generation, self.path)
                    terminal_received = True
                elif kind == "failed":
                    self.failed.emit(
                        self.generation, self.path, str(payload[0])
                    )
                    terminal_received = True
        finally:
            process_cancel_event.set()
            if process.is_alive():
                process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
            process.join(timeout=1.0)
            result_queue.close()
            result_queue.join_thread()
            self._process_cancel_event = None


def _thumbnail_process(
    path: str,
    assets: list[PDFAsset],
    result_queue,
    cancel_event,
) -> None:
    """Render card thumbnails outside the GUI process."""
    for index, asset in enumerate(assets):
        if cancel_event.is_set():
            break
        try:
            data = render_asset_thumbnail(
                Path(path), asset, size=(360, 190)
            )
        except Exception as exc:
            result_queue.put(("ready_error", index, str(exc)))
        else:
            result_queue.put(("ready", index, data))
    result_queue.put(("done",))


class ThumbnailWorker(QObject):
    ready = Signal(int, int, object)
    done = Signal(int)

    def __init__(
        self, path: Path, assets: list[PDFAsset], generation: int
    ) -> None:
        super().__init__()
        self.path = path
        self.assets = assets
        self.generation = generation
        self.cancel_event = threading.Event()
        self._process_cancel_event = None

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._process_cancel_event is not None:
            self._process_cancel_event.set()

    @Slot()
    def run(self) -> None:
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process_cancel_event = context.Event()
        self._process_cancel_event = process_cancel_event
        process = context.Process(
            target=_thumbnail_process,
            args=(
                str(self.path),
                self.assets,
                result_queue,
                process_cancel_event,
            ),
            name="PDFThumbnailRenderer",
            daemon=True,
        )
        process.start()
        terminal_received = False
        process_exited_at: float | None = None
        cancel_started: float | None = None
        try:
            while not terminal_received:
                if self.cancel_event.is_set():
                    process_cancel_event.set()
                    if cancel_started is None:
                        cancel_started = time.monotonic()
                    elif process.is_alive() and time.monotonic() - cancel_started > 1:
                        process.terminate()
                        self.done.emit(self.generation)
                        terminal_received = True
                        break
                try:
                    message = result_queue.get(timeout=0.05)
                except queue.Empty:
                    if not process.is_alive():
                        if process_exited_at is None:
                            process_exited_at = time.monotonic()
                        elif time.monotonic() - process_exited_at > 0.5:
                            self.done.emit(self.generation)
                            terminal_received = True
                    else:
                        process_exited_at = None
                    continue

                kind, *payload = message
                if kind == "ready":
                    index, data = payload
                    self.ready.emit(self.generation, int(index), data)
                elif kind == "ready_error":
                    index, error_message = payload
                    self.ready.emit(
                        self.generation,
                        int(index),
                        RuntimeError(str(error_message)),
                    )
                elif kind == "done":
                    self.done.emit(self.generation)
                    terminal_received = True
        finally:
            process_cancel_event.set()
            if process.is_alive():
                process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
            process.join(timeout=1.0)
            result_queue.close()
            result_queue.join_thread()
            self._process_cancel_event = None


class CompressionWorker(QObject):
    progress = Signal(int, str)
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        source: Path,
        destination: Path,
        target: int,
        image_xrefs: set[int],
        vector_pages: set[int],
        figure_regions: dict[int, list[tuple[float, float, float, float]]],
    ) -> None:
        super().__init__()
        self.source = source
        self.destination = destination
        self.target = target
        self.image_xrefs = image_xrefs
        self.vector_pages = vector_pages
        self.figure_regions = figure_regions
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            result = compress_pdf(
                self.source,
                self.destination,
                self.target,
                progress_callback=lambda value, message: self.progress.emit(
                    value, message
                ),
                cancel_event=self.cancel_event,
                selected_image_xrefs=self.image_xrefs,
                selected_vector_pages=self.vector_pages,
                selected_figure_regions=self.figure_regions,
            )
        except CompressionCancelled:
            self.cancelled.emit()
        except (
            TargetTooSmallError,
            NoCompressibleImagesError,
            CompressionError,
        ) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"处理失败：{exc}")
        else:
            self.completed.emit(result)


class PreviewRenderWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, asset: PDFAsset) -> None:
        super().__init__()
        self.path = path
        self.asset = asset

    @Slot()
    def run(self) -> None:
        try:
            data = render_asset_image(self.path, self.asset, dpi=240)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(data)


class SmoothGraphicsView(QGraphicsView):
    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._zoom = 1.0

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.14 if event.angleDelta().y() > 0 else 1 / 1.14
        next_zoom = self._zoom * factor
        if 0.08 <= next_zoom <= 12:
            self.scale(factor, factor)
            self._zoom = next_zoom
        event.accept()

    def fit_item(self, item: QGraphicsPixmapItem) -> None:
        self.resetTransform()
        self.fitInView(item, Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom = max(self.transform().m11(), 0.01)

    def actual_size(self) -> None:
        self.resetTransform()
        self._zoom = 1.0


class PreviewDialog(QDialog):
    def __init__(self, path: Path, asset: PDFAsset, parent: QWidget) -> None:
        super().__init__(parent)
        self.path = path
        self.asset = asset
        self.thread: QThread | None = None
        self.worker: PreviewRenderWorker | None = None
        self.pixmap_item: QGraphicsPixmapItem | None = None
        self.setWindowTitle(f"高清预览 · {asset.title or asset.display_name}")
        self.setWindowIcon(make_app_icon())
        self.resize(1080, 760)
        self.setMinimumSize(760, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(asset.display_name)
        title.setProperty("title", True)
        subtitle = QLabel(f"{asset.storage_label} · 滚轮缩放 · 按住左键拖动")
        subtitle.setProperty("secondary", True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        toolbar.addLayout(title_box, 1)
        minus = QPushButton("−")
        minus.setFixedWidth(42)
        plus = QPushButton("+")
        plus.setFixedWidth(42)
        fit_button = QPushButton("适应窗口")
        actual_button = QPushButton("100%")
        toolbar.addWidget(minus)
        toolbar.addWidget(plus)
        toolbar.addWidget(fit_button)
        toolbar.addWidget(actual_button)
        root.addLayout(toolbar)

        self.scene = QGraphicsScene(self)
        self.view = SmoothGraphicsView(self.scene)
        root.addWidget(self.view, 1)
        self.status = QLabel("正在后台生成高清预览…")
        self.status.setProperty("secondary", True)
        root.addWidget(self.status)

        self.loading_orb = WarmLoadingOrb()
        self.loading_card = QFrame()
        self.loading_card.setObjectName("readingGlass")
        self.loading_card.setFixedSize(390, 250)
        loading_layout = QVBoxLayout(self.loading_card)
        loading_layout.setContentsMargins(28, 18, 28, 22)
        loading_layout.setSpacing(3)
        loading_layout.addWidget(
            self.loading_orb, 0, Qt.AlignmentFlag.AlignHCenter
        )
        loading_title = QLabel("正在冲洗高清全景")
        loading_title.setObjectName("readingTitle")
        loading_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(loading_title)
        self.loading_message = QLabel("细小字符和线条会完整保留，请稍候…")
        self.loading_message.setProperty("secondary", True)
        self.loading_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(self.loading_message)
        self.loading_proxy = self.scene.addWidget(self.loading_card)
        self.scene.setSceneRect(self.loading_proxy.boundingRect())

        minus.clicked.connect(lambda: self._scale(1 / 1.18))
        plus.clicked.connect(lambda: self._scale(1.18))
        fit_button.clicked.connect(self._fit)
        actual_button.clicked.connect(self.view.actual_size)
        self._start_render()

    def _scale(self, factor: float) -> None:
        next_zoom = self.view._zoom * factor
        if 0.08 <= next_zoom <= 12:
            self.view.scale(factor, factor)
            self.view._zoom = next_zoom

    def _fit(self) -> None:
        if self.pixmap_item:
            self.view.fit_item(self.pixmap_item)

    def _start_render(self) -> None:
        self.thread = QThread(self)
        self.worker = PreviewRenderWorker(self.path, self.asset)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.completed.connect(self._render_completed)
        self.worker.failed.connect(self._render_failed)
        self.worker.completed.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.start()

    @Slot(object)
    def _render_completed(self, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._render_failed("无法读取预览图像。")
            return
        self.loading_orb.stop()
        self.scene.clear()
        self.pixmap_item = self.scene.addPixmap(pixmap)
        self.scene.setSceneRect(self.pixmap_item.boundingRect())
        self.view.fit_item(self.pixmap_item)
        self.status.setText(
            f"高清全景：{pixmap.width()} × {pixmap.height()} 像素"
        )

    @Slot(str)
    def _render_failed(self, message: str) -> None:
        self.loading_orb.stop()
        self.loading_message.setText("生成失败，请关闭窗口后重试。")
        self.loading_message.setStyleSheet(f"color: {ERROR};")
        self.status.setText(f"高清预览失败：{message}")
        self.status.setStyleSheet(f"color: {ERROR};")


class Toast(QFrame):
    def __init__(self, parent: QWidget, text: str) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 11, 16, 11)
        label = QLabel(text)
        layout.addWidget(label)
        self.adjustSize()
        self.move(parent.width() - self.width() - 26, 108)
        self.raise_()

        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        self._fade_in = QPropertyAnimation(effect, b"opacity", self)
        self._fade_in.setDuration(220)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_in.start()
        QTimer.singleShot(2600, self._fade_out)

    def _fade_out(self) -> None:
        effect = self.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            self.deleteLater()
            return
        self._fade_animation = QPropertyAnimation(effect, b"opacity", self)
        self._fade_animation.setDuration(260)
        self._fade_animation.setStartValue(effect.opacity())
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.finished.connect(self.deleteLater)
        self._fade_animation.start()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setWindowIcon(make_app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(1080, 700)
        self.setAcceptDrops(True)

        self.input_path: Path | None = None
        self.output_path: Path | None = None
        self.output_custom = False
        self.last_output: Path | None = None
        self.assets: list[PDFAsset] = []
        self.asset_cards: dict[str, AssetCard] = {}
        self.selected_asset_keys: set[str] = set()
        self.assets_loading = False
        self.scan_generation = 0
        self.preview_generation = 0
        self.scan_thread: QThread | None = None
        self.scan_worker: AssetScanWorker | None = None
        self.thumbnail_thread: QThread | None = None
        self.thumbnail_worker: ThumbnailWorker | None = None
        self.compression_thread: QThread | None = None
        self.compression_worker: CompressionWorker | None = None
        self.preview_dialogs: list[PreviewDialog] = []
        self._fade_animations: list[QPropertyAnimation] = []
        self.loading_panel: ReadingPanel | None = None
        self._asset_population_generation = 0

        self._build_ui()

    def _card(self) -> QFrame:
        frame = QFrame()
        frame.setProperty("card", True)
        return frame

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_bar.setFixedHeight(92)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(26, 13, 26, 13)
        top_layout.setSpacing(14)

        icon_label = QLabel()
        icon_label.setPixmap(make_app_icon().pixmap(48, 48))
        icon_label.setFixedSize(50, 50)
        top_layout.addWidget(icon_label)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("精确定容 · 原生文字层 · 科研图表清晰优先")
        subtitle.setObjectName("appSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        top_layout.addLayout(brand)
        top_layout.addStretch(1)
        safe = QLabel("原 PDF 始终不变")
        safe.setProperty("secondary", True)
        top_layout.addWidget(safe)
        version = QLabel(f"VERSION {APP_VERSION}")
        version.setObjectName("versionPill")
        top_layout.addWidget(version)
        outer.addWidget(top_bar)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(20, 18, 20, 20)
        body_layout.setSpacing(18)
        outer.addWidget(body, 1)

        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        sidebar_scroll.setFixedWidth(390)
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(4, 4, 8, 8)
        sidebar_layout.setSpacing(11)
        sidebar_scroll.setWidget(sidebar)
        body_layout.addWidget(sidebar_scroll)

        file_card = self._card()
        add_shadow(file_card, 20, 4)
        file_layout = QVBoxLayout(file_card)
        file_layout.setContentsMargins(16, 14, 16, 14)
        file_layout.setSpacing(9)
        file_header = QHBoxLayout()
        step = QLabel("01   PDF 文件")
        step.setProperty("step", True)
        file_header.addWidget(step)
        file_header.addStretch(1)
        self.file_button = QPushButton("选择文件")
        self.file_button.clicked.connect(self.choose_input)
        file_header.addWidget(self.file_button)
        file_layout.addLayout(file_header)
        self.input_info = QLabel("尚未选择 PDF 文件")
        self.input_info.setProperty("secondary", True)
        self.input_info.setWordWrap(True)
        file_layout.addWidget(self.input_info)
        sidebar_layout.addWidget(file_card)

        target_card = self._card()
        add_shadow(target_card, 20, 4)
        target_layout = QVBoxLayout(target_card)
        target_layout.setContentsMargins(16, 14, 16, 14)
        target_layout.setSpacing(9)
        target_step = QLabel("02   目标大小")
        target_step.setProperty("step", True)
        target_layout.addWidget(target_step)
        target_row = QHBoxLayout()
        self.target_edit = QLineEdit("5")
        self.target_edit.setPlaceholderText("输入目标大小")
        self.target_edit.textChanged.connect(self._target_changed)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["MB", "KB"])
        self.unit_combo.setFixedWidth(82)
        self.unit_combo.currentTextChanged.connect(self._target_changed)
        target_row.addWidget(self.target_edit, 1)
        target_row.addWidget(self.unit_combo)
        target_layout.addLayout(target_row)
        target_hint = QLabel("尽量贴近目标，并优先保证小字符和细线清晰。")
        target_hint.setProperty("secondary", True)
        target_hint.setWordWrap(True)
        target_layout.addWidget(target_hint)
        self.selection_info = QLabel("选择文件后可在右侧预览并勾选 Figure")
        self.selection_info.setObjectName("selectionInfo")
        self.selection_info.setWordWrap(True)
        target_layout.addWidget(self.selection_info)
        sidebar_layout.addWidget(target_card)

        output_card = self._card()
        add_shadow(output_card, 20, 4)
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(16, 14, 16, 14)
        output_layout.setSpacing(9)
        output_header = QHBoxLayout()
        output_step = QLabel("03   保存位置")
        output_step.setProperty("step", True)
        output_header.addWidget(output_step)
        output_header.addStretch(1)
        self.output_button = QPushButton("更改位置")
        self.output_button.clicked.connect(self.choose_output)
        output_header.addWidget(self.output_button)
        output_layout.addLayout(output_header)
        self.output_info = QLabel("选择 PDF 后自动生成保存位置")
        self.output_info.setProperty("secondary", True)
        self.output_info.setWordWrap(True)
        output_layout.addWidget(self.output_info)
        sidebar_layout.addWidget(output_card)

        status_card = self._card()
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(16, 14, 16, 14)
        status_layout.setSpacing(9)
        self.status_label = QLabel("等待选择文件")
        self.status_label.setStyleSheet("font-weight: 650;")
        status_layout.addWidget(self.status_label)
        self.progress_bar = AnimatedProgressBar()
        self.progress_bar.setRange(0, 100)
        status_layout.addWidget(self.progress_bar)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setProperty("secondary", True)
        status_layout.addWidget(self.result_label)
        sidebar_layout.addWidget(status_card)

        self.start_button = QPushButton("开始智能压缩")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_compression)
        sidebar_layout.addWidget(self.start_button)
        action_row = QHBoxLayout()
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_compression)
        self.open_button = QPushButton("打开输出文件夹")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_output_folder)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.open_button)
        sidebar_layout.addLayout(action_row)
        safety = QLabel("图内文字保持可搜索 · 未勾选内容保持原样")
        safety.setProperty("secondary", True)
        safety.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sidebar_layout.addWidget(safety)
        sidebar_layout.addStretch(1)

        preview_panel = self._card()
        add_shadow(preview_panel, 24, 5)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        body_layout.addWidget(preview_panel, 1)

        preview_header = QWidget()
        preview_header_layout = QVBoxLayout(preview_header)
        preview_header_layout.setContentsMargins(18, 15, 18, 12)
        preview_header_layout.setSpacing(9)
        title_row = QHBoxLayout()
        preview_title_box = QVBoxLayout()
        preview_title = QLabel("图形预览")
        preview_title.setProperty("title", True)
        preview_subtitle = QLabel("预览、选择并比较每个 Figure 的空间占用")
        preview_subtitle.setProperty("secondary", True)
        preview_title_box.addWidget(preview_title)
        preview_title_box.addWidget(preview_subtitle)
        title_row.addLayout(preview_title_box)
        title_row.addStretch(1)
        self.preview_count = QLabel("尚未选择 PDF")
        self.preview_count.setObjectName("versionPill")
        title_row.addWidget(self.preview_count)
        preview_header_layout.addLayout(title_row)
        controls = QHBoxLayout()
        self.all_button = QPushButton("全选")
        self.none_button = QPushButton("全不选")
        self.figures_button = QPushButton("仅选 Figure")
        self.all_button.clicked.connect(lambda: self._set_selection(None, True))
        self.none_button.clicked.connect(lambda: self._set_selection(None, False))
        self.figures_button.clicked.connect(
            lambda: self._set_selection("figure", True)
        )
        controls.addWidget(self.all_button)
        controls.addWidget(self.none_button)
        controls.addWidget(self.figures_button)
        controls.addStretch(1)
        preview_header_layout.addLayout(controls)
        preview_layout.addWidget(preview_header)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER};")
        preview_layout.addWidget(divider)

        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.preview_container = QWidget()
        self.preview_container.setStyleSheet("background: #FAFAFC;")
        self.preview_grid = QGridLayout(self.preview_container)
        self.preview_grid.setContentsMargins(12, 12, 12, 12)
        self.preview_grid.setHorizontalSpacing(12)
        self.preview_grid.setVerticalSpacing(12)
        self.preview_grid.setColumnStretch(0, 1)
        self.preview_grid.setColumnStretch(1, 1)
        self.preview_scroll.setWidget(self.preview_container)
        preview_layout.addWidget(self.preview_scroll, 1)

        self._show_empty_state("选择 PDF 后将在这里显示 Figure")
        self._set_asset_controls_enabled(False)

    def _show_empty_state(self, message: str) -> None:
        self._clear_preview_grid()
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(20, 90, 20, 90)
        layout.addStretch(1)
        icon = QLabel()
        icon.setPixmap(make_app_icon().pixmap(58, 58))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        title = QLabel(message)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 14px; font-weight: 700;")
        layout.addWidget(title)
        hint = QLabel("也可以把 PDF 文件直接拖到窗口中")
        hint.setProperty("secondary", True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        layout.addStretch(1)
        self.preview_grid.addWidget(holder, 0, 0, 1, 2)

    def _show_loading_state(self, path: Path) -> None:
        self._clear_preview_grid()
        self.loading_panel = ReadingPanel(
            path.name, format_bytes(path.stat().st_size)
        )
        self.loading_panel.cancel_requested.connect(self.cancel_scan)
        self.preview_grid.addWidget(self.loading_panel, 0, 0, 1, 2)

    def _clear_preview_grid(self) -> None:
        if self.loading_panel:
            self.loading_panel.stop()
            self.loading_panel = None
        while self.preview_grid.count():
            item = self.preview_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.asset_cards.clear()

    def _set_asset_controls_enabled(self, enabled: bool) -> None:
        self.all_button.setEnabled(enabled)
        self.none_button.setEnabled(enabled)
        self.figures_button.setEnabled(enabled)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if any(url.toLocalFile().lower().endswith(".pdf") for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if self.assets_loading:
            Toast(self, "这篇论文还在分析中，请稍等一下")
            event.ignore()
            return
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".pdf" and path.is_file():
                self._load_input(path)
                event.acceptProposedAction()
                return

    def choose_input(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self, "选择要压缩的 PDF", "", "PDF 文件 (*.pdf);;所有文件 (*.*)"
        )
        if selected:
            self._load_input(Path(selected))

    def _load_input(self, path: Path) -> None:
        if not path.is_file():
            self._show_error("请选择一个存在的 PDF 文件。")
            return
        self.input_path = path
        self.output_custom = False
        self.input_info.setText(f"{path.name}  ·  {format_bytes(path.stat().st_size)}")
        self.scan_generation += 1
        generation = self.scan_generation
        self.assets_loading = True
        self.assets = []
        self.selected_asset_keys.clear()
        self.file_button.setEnabled(False)
        self.file_button.setText("读取中…")
        self.start_button.setEnabled(False)
        self.selection_info.setText("正在识别完整 Figure，请稍候…")
        self.preview_count.setText("分析中…")
        self.status_label.setText("正在分析 PDF 图形结构…")
        self.progress_bar.setValue(2)
        self._show_loading_state(path)
        self._set_asset_controls_enabled(False)

        size = path.stat().st_size
        if size >= 1024**2:
            suggested = max(0.1, size / 1024**2 * 0.6)
            self.target_edit.setText(f"{suggested:.2f}".rstrip("0").rstrip("."))
            self.unit_combo.setCurrentText("MB")
        else:
            suggested = max(20, size / 1024 * 0.6)
            self.target_edit.setText(str(round(suggested)))
            self.unit_combo.setCurrentText("KB")
        self._set_default_output()
        self.result_label.clear()
        self.open_button.setEnabled(False)

        self.scan_thread = QThread(self)
        self.scan_worker = AssetScanWorker(path, generation)
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.progress.connect(self._scan_progress)
        self.scan_worker.completed.connect(self._assets_loaded)
        self.scan_worker.failed.connect(self._assets_failed)
        self.scan_worker.cancelled.connect(self._assets_scan_cancelled)
        self.scan_worker.completed.connect(self.scan_thread.quit)
        self.scan_worker.failed.connect(self.scan_thread.quit)
        self.scan_worker.cancelled.connect(self.scan_thread.quit)
        self.scan_thread.finished.connect(self.scan_worker.deleteLater)
        current_thread = self.scan_thread
        self.scan_thread.finished.connect(
            lambda thread=current_thread: self._scan_thread_finished(thread)
        )
        self.scan_thread.start()

    def _scan_thread_finished(self, thread: QThread) -> None:
        if self.scan_thread is thread:
            self.scan_worker = None
            self.scan_thread = None

    def cancel_scan(self) -> None:
        if not self.assets_loading or not self.scan_worker:
            return
        self.scan_worker.cancel()
        self.status_label.setText("正在安全停止 PDF 读取…")
        if self.loading_panel:
            self.loading_panel.mark_cancelling()

    @Slot(int, int, str)
    def _scan_progress(self, generation: int, value: int, message: str) -> None:
        if generation != self.scan_generation:
            return
        self.progress_bar.set_smooth_value(value)
        self.status_label.setText(message)
        if self.loading_panel:
            self.loading_panel.update_progress(value, message)

    @Slot(int, object, object, int)
    def _assets_loaded(
        self, generation: int, path: Path, assets: list[PDFAsset], page_count: int
    ) -> None:
        if generation != self.scan_generation or path != self.input_path:
            return
        self.assets = assets
        self.selected_asset_keys = {asset.key for asset in assets}
        self.progress_bar.set_smooth_value(100)
        if self.loading_panel:
            self.loading_panel.complete(len(assets))
        QTimer.singleShot(
            460,
            lambda: self._reveal_loaded_assets(
                generation, path, assets, page_count
            ),
        )

    def _reveal_loaded_assets(
        self,
        generation: int,
        path: Path,
        assets: list[PDFAsset],
        page_count: int,
    ) -> None:
        if generation != self.scan_generation or path != self.input_path:
            return
        self.status_label.setText(
            f"已读完 {page_count} 页，正在平滑铺开 {len(assets)} 个预览…"
        )
        self._populate_assets(path, assets, generation, page_count)

    @Slot(int, object, str)
    def _assets_failed(self, generation: int, path: Path, message: str) -> None:
        if generation != self.scan_generation or path != self.input_path:
            return
        self.assets_loading = False
        self.file_button.setEnabled(True)
        self.file_button.setText("选择文件")
        self.assets = []
        self.selected_asset_keys.clear()
        self.start_button.setEnabled(True)
        self.selection_info.setText(f"Figure 识别失败：{message}")
        self.preview_count.setText("读取失败")
        self._show_empty_state("无法读取 Figure 列表")
        self.status_label.setText("PDF 已选择，但 Figure 识别失败")
        self.progress_bar.setValue(0)

    @Slot(int, object)
    def _assets_scan_cancelled(self, generation: int, path: Path) -> None:
        if generation != self.scan_generation or path != self.input_path:
            return
        self.assets_loading = False
        self.file_button.setEnabled(True)
        self.file_button.setText("选择文件")
        self.assets = []
        self.selected_asset_keys.clear()
        self.start_button.setEnabled(False)
        self.selection_info.setText("读取已停止，原 PDF 没有发生任何变化。")
        self.preview_count.setText("已停止")
        self._show_empty_state("已停止读取这份 PDF")
        self.status_label.setText("PDF 读取已停止")
        self.progress_bar.setValue(0)

    def _populate_assets(
        self,
        path: Path,
        assets: list[PDFAsset],
        generation: int,
        page_count: int,
    ) -> None:
        self._clear_preview_grid()
        self._asset_population_generation = generation
        if not assets:
            self._show_empty_state("没有识别到 Figure 或独立位图")
            self.preview_count.setText("0 个图形")
            self._set_asset_controls_enabled(False)
            self._finish_asset_population(path, assets, generation, page_count)
            return
        self.preview_count.setText(f"正在准备预览 · 0 / {len(assets)}")
        self._set_asset_controls_enabled(False)
        self._populate_asset_batch(path, assets, generation, page_count, 0)

    def _populate_asset_batch(
        self,
        path: Path,
        assets: list[PDFAsset],
        generation: int,
        page_count: int,
        start_index: int,
    ) -> None:
        if (
            generation != self.scan_generation
            or generation != self._asset_population_generation
            or path != self.input_path
        ):
            return
        end_index = min(len(assets), start_index + 4)
        for index in range(start_index, end_index):
            asset = assets[index]
            card = AssetCard(asset, selected=True)
            card.preview_requested.connect(self._open_preview)
            card.selection_changed.connect(self._asset_selection_changed)
            self.preview_grid.addWidget(card, index // 2, index % 2)
            self.asset_cards[asset.key] = card
            self._fade_in(card, (index - start_index) * 28)
        self.preview_count.setText(
            f"正在准备预览 · {end_index} / {len(assets)}"
        )
        if end_index < len(assets):
            QTimer.singleShot(
                8,
                lambda: self._populate_asset_batch(
                    path, assets, generation, page_count, end_index
                ),
            )
            return
        self._finish_asset_population(path, assets, generation, page_count)

    def _finish_asset_population(
        self,
        path: Path,
        assets: list[PDFAsset],
        generation: int,
        page_count: int,
    ) -> None:
        if generation != self.scan_generation or path != self.input_path:
            return
        final_row = (len(assets) + 1) // 2
        self.preview_grid.setRowStretch(final_row, 1)
        total_storage = sum(asset.storage_bytes for asset in assets)
        self.preview_count.setText(
            f"{len(assets)} 项 · 约 {format_bytes(total_storage)}"
            if assets
            else "0 个图形"
        )
        self._set_asset_controls_enabled(bool(assets))
        self.preview_scroll.verticalScrollBar().setValue(0)

        self._start_thumbnail_loading(path, assets)
        self.assets_loading = False
        self.file_button.setEnabled(True)
        self.file_button.setText("选择文件")
        self.start_button.setEnabled(True)
        self._update_selection_info()
        self.status_label.setText(
            f"已读完 {page_count} 页，选择图形后即可开始压缩"
        )

    def _start_thumbnail_loading(
        self, path: Path, assets: list[PDFAsset]
    ) -> None:
        if not assets:
            return

        if self.thumbnail_worker:
            self.thumbnail_worker.cancel()
        self.preview_generation += 1
        generation = self.preview_generation
        self.thumbnail_thread = QThread(self)
        self.thumbnail_worker = ThumbnailWorker(path, assets, generation)
        self.thumbnail_worker.moveToThread(self.thumbnail_thread)
        self.thumbnail_thread.started.connect(self.thumbnail_worker.run)
        self.thumbnail_worker.ready.connect(self._thumbnail_ready)
        self.thumbnail_worker.done.connect(self.thumbnail_thread.quit)
        self.thumbnail_thread.finished.connect(self.thumbnail_worker.deleteLater)
        self.thumbnail_thread.start()

    def _fade_in(self, widget: QWidget, delay: int) -> None:
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", widget)
        animation.setDuration(260)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animations.append(animation)

        def start() -> None:
            animation.start()

        def cleanup() -> None:
            widget.setGraphicsEffect(None)
            if animation in self._fade_animations:
                self._fade_animations.remove(animation)

        animation.finished.connect(cleanup)
        QTimer.singleShot(delay, start)

    @Slot(int, int, object)
    def _thumbnail_ready(
        self, generation: int, index: int, result: bytes | Exception
    ) -> None:
        if generation != self.preview_generation or not (0 <= index < len(self.assets)):
            return
        card = self.asset_cards.get(self.assets[index].key)
        if not card:
            return
        if isinstance(result, Exception):
            card.set_thumbnail_error(str(result))
        else:
            card.set_thumbnail(result)

    @Slot(object)
    def _open_preview(self, asset: PDFAsset) -> None:
        if not self.input_path:
            return
        dialog = PreviewDialog(self.input_path, asset, self)
        self.preview_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _result, current=dialog: self._remove_preview_dialog(current)
        )
        dialog.show()

    def _remove_preview_dialog(self, dialog: PreviewDialog) -> None:
        if dialog in self.preview_dialogs:
            self.preview_dialogs.remove(dialog)

    @Slot(str, bool)
    def _asset_selection_changed(self, key: str, selected: bool) -> None:
        if selected:
            self.selected_asset_keys.add(key)
        else:
            self.selected_asset_keys.discard(key)
        self._update_selection_info()

    def _set_selection(self, kind: str | None, value: bool) -> None:
        for asset in self.assets:
            selected = value if kind is None else asset.kind == kind
            card = self.asset_cards.get(asset.key)
            if card:
                card.set_selected(selected)
            if selected:
                self.selected_asset_keys.add(asset.key)
            else:
                self.selected_asset_keys.discard(asset.key)
        self._update_selection_info()

    def _update_selection_info(self) -> None:
        selected = [
            asset for asset in self.assets if asset.key in self.selected_asset_keys
        ]
        figures = sum(asset.kind == "figure" for asset in selected)
        images = sum(asset.kind == "image" for asset in selected)
        storage = sum(asset.storage_bytes for asset in selected)
        self.selection_info.setText(
            f"已选 {len(selected)}/{len(self.assets)} · Figure {figures} · "
            f"位图 {images} · 约 {format_bytes(storage)}"
        )

    def _target_changed(self) -> None:
        if self.input_path and not self.output_custom:
            self._set_default_output()

    def _set_default_output(self) -> None:
        if not self.input_path:
            return
        label = self.target_edit.text().strip() or "目标"
        label = re.sub(r"[^0-9A-Za-z._-]", "_", label)
        unit = self.unit_combo.currentText()
        name = f"{self.input_path.stem}_压缩至{label}{unit}.pdf"
        self.output_path = self.input_path.with_name(name)
        self.output_info.setText(str(self.output_path))

    def choose_output(self) -> None:
        if not self.input_path:
            self._show_error("请先选择要处理的 PDF 文件。")
            return
        initial = str(self.output_path or self.input_path.with_suffix(".compressed.pdf"))
        selected, _filter = QFileDialog.getSaveFileName(
            self, "保存压缩后的 PDF", initial, "PDF 文件 (*.pdf)"
        )
        if selected:
            self.output_path = Path(selected)
            self.output_custom = True
            self.output_info.setText(str(self.output_path))

    def _target_bytes(self) -> int:
        try:
            value = Decimal(self.target_edit.text().strip())
        except InvalidOperation as exc:
            raise ValueError("请输入有效的目标大小。") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("目标大小必须大于 0。")
        multiplier = 1024**2 if self.unit_combo.currentText() == "MB" else 1024
        size = int(value * multiplier)
        if size < 1024:
            raise ValueError("目标大小不能小于 1 KB。")
        return size

    def start_compression(self) -> None:
        try:
            if self.assets_loading:
                raise ValueError("正在识别 PDF 中的 Figure，请稍候。")
            if not self.input_path or not self.input_path.is_file():
                raise ValueError("请先选择一个存在的 PDF 文件。")
            target = self._target_bytes()
            if not self.output_path:
                self._set_default_output()
            assert self.output_path is not None
            if self.input_path.resolve() == self.output_path.resolve():
                raise ValueError("输出文件不能覆盖原 PDF。")
            if self.output_path.exists():
                answer = QMessageBox.question(
                    self,
                    APP_NAME,
                    f"输出文件已经存在：\n{self.output_path.name}\n\n是否覆盖？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

            selected = [
                asset
                for asset in self.assets
                if asset.key in self.selected_asset_keys
            ]
            image_xrefs = {
                asset.xref
                for asset in selected
                if asset.kind == "image" and asset.xref is not None
            }
            vector_pages = {
                asset.page_numbers[0]
                for asset in selected
                if asset.kind == "vector"
            }
            figure_regions: dict[
                int, list[tuple[float, float, float, float]]
            ] = {}
            for asset in selected:
                if asset.kind == "figure" and asset.rect is not None:
                    figure_regions.setdefault(asset.page_numbers[0], []).append(
                        asset.rect
                    )
        except (ValueError, OSError, RuntimeError) as exc:
            self._show_error(str(exc))
            return

        self.progress_bar.setValue(0)
        self.status_label.setText("正在准备压缩…")
        self.result_label.clear()
        self.result_label.setStyleSheet("")
        self._set_busy(True)

        self.compression_thread = QThread(self)
        self.compression_worker = CompressionWorker(
            self.input_path,
            self.output_path,
            target,
            image_xrefs,
            vector_pages,
            figure_regions,
        )
        self.compression_worker.moveToThread(self.compression_thread)
        self.compression_thread.started.connect(self.compression_worker.run)
        self.compression_worker.progress.connect(self._compression_progress)
        self.compression_worker.completed.connect(self._compression_completed)
        self.compression_worker.failed.connect(self._compression_failed)
        self.compression_worker.cancelled.connect(self._compression_cancelled)
        self.compression_worker.completed.connect(self.compression_thread.quit)
        self.compression_worker.failed.connect(self.compression_thread.quit)
        self.compression_worker.cancelled.connect(self.compression_thread.quit)
        self.compression_thread.finished.connect(self.compression_worker.deleteLater)
        self.compression_thread.start()

    @Slot(int, str)
    def _compression_progress(self, value: int, message: str) -> None:
        self.progress_bar.set_smooth_value(value)
        self.status_label.setText(message)

    @Slot(object)
    def _compression_completed(self, result: CompressionResult) -> None:
        self._set_busy(False)
        self.progress_bar.set_smooth_value(100)
        self.status_label.setText("压缩完成")
        self.last_output = result.output_path
        self.open_button.setEnabled(True)
        if result.method == "images":
            method = (
                f"{result.images_processed} 张位图 · "
                f"保留 {result.image_scale:.0%} 分辨率"
            )
        elif result.method == "vectors":
            method = (
                f"{result.figures_processed} 张 Figure · "
                f"最高 {result.vector_dpi} DPI"
            )
        elif result.method == "images_vectors":
            method = (
                f"{result.figures_processed} 张 Figure + "
                f"{result.images_processed} 张位图 · 最高 {result.vector_dpi} DPI"
            )
        elif result.method == "lossless":
            method = "无损结构优化"
        elif result.method == "copied":
            method = "原文件已满足目标"
        else:
            method = result.method
        self.result_label.setText(
            f"{format_bytes(result.original_bytes)}  →  "
            f"{format_bytes(result.output_bytes)}\n"
            f"减少 {result.saved_ratio:.1%} · {method}"
        )
        self.result_label.setStyleSheet(f"color: {SUCCESS}; font-weight: 650;")
        Toast(self, f"压缩完成 · {format_bytes(result.output_bytes)}")

    @Slot(str)
    def _compression_failed(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.setText("处理失败")
        self.result_label.setText(message)
        self.result_label.setStyleSheet(f"color: {ERROR}; font-weight: 650;")
        self._show_error(message)

    @Slot()
    def _compression_cancelled(self) -> None:
        self._set_busy(False)
        self.status_label.setText("任务已取消")
        self.result_label.setText("没有生成输出文件。")

    def cancel_compression(self) -> None:
        if self.compression_worker:
            self.compression_worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在安全取消…")

    def _set_busy(self, busy: bool) -> None:
        self.file_button.setEnabled(not busy)
        self.output_button.setEnabled(not busy)
        self.target_edit.setEnabled(not busy)
        self.unit_combo.setEnabled(not busy)
        self.start_button.setEnabled(not busy and not self.assets_loading)
        self.cancel_button.setEnabled(busy)
        self._set_asset_controls_enabled(not busy and bool(self.assets))
        for card in self.asset_cards.values():
            card.checkbox.setEnabled(not busy)
        if busy:
            self.open_button.setEnabled(False)

    def open_output_folder(self) -> None:
        if not self.last_output:
            return
        try:
            os.startfile(str(self.last_output.parent))  # type: ignore[attr-defined]
        except OSError as exc:
            self._show_error(f"无法打开输出文件夹：{exc}")

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, APP_NAME, message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.scan_worker:
            self.scan_worker.cancel()
        if self.thumbnail_worker:
            self.thumbnail_worker.cancel()
        if self.compression_worker:
            self.compression_worker.cancel()
        event.accept()


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            from ctypes import windll

            windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "OpenAI.PDFSizeReducer.Qt3"
            )
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(make_app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    launch_pdf = next(
        (
            Path(argument)
            for argument in sys.argv[1:]
            if argument.lower().endswith(".pdf")
            and Path(argument).is_file()
        ),
        None,
    )
    if launch_pdf is not None:
        QTimer.singleShot(
            0, lambda selected=launch_pdf: window._load_input(selected)
        )
    sys.exit(app.exec())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
