"""Record a deterministic product demo from the real Qt interface.

The recorder drives the same code paths as a user: it loads a PDF, waits for
background Figure discovery and thumbnail rendering, opens a high-resolution
preview, changes the selection and target, and starts an output task.  Frames
are captured from Qt widgets rather than from the desktop, so notifications or
other personal windows can never appear in the recording.
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qt_app import APP_NAME, APP_STYLE, APP_VERSION, BACKGROUND, MainWindow


class DemoRecorder(QObject):
    """Small state machine that records a real end-to-end interaction."""

    def __init__(
        self,
        app: QApplication,
        window: MainWindow,
        pdf_path: Path,
        frame_dir: Path,
        fps: int,
        width: int,
        height: int,
        timeout_seconds: int,
        show_caption: bool,
    ) -> None:
        super().__init__()
        self.app = app
        self.window = window
        self.pdf_path = pdf_path
        self.frame_dir = frame_dir
        self.fps = fps
        self.width = width
        self.height = height
        self.timeout_seconds = timeout_seconds
        self.show_caption = show_caption
        self.frame_number = 0
        self.started = time.monotonic()
        self.state_started = self.started
        self.state = "welcome"
        self.caption = "选择需要定容的 PDF"
        self.preview_dialog = None
        self.zoom_steps = 0
        self.output_path = frame_dir.parent / "demo-output.pdf"
        self.failure: str | None = None

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(round(1000 / fps))
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        for old_frame in self.frame_dir.glob("frame_*.png"):
            old_frame.unlink()
        if self.output_path.exists():
            self.output_path.unlink()
        self.timer.start()

    def _elapsed_in_state(self) -> float:
        return time.monotonic() - self.state_started

    def _advance(self, state: str, caption: str) -> None:
        self.state = state
        self.caption = caption
        self.state_started = time.monotonic()

    def _thumbnail_work_finished(self) -> bool:
        thread = self.window.thumbnail_thread
        cards_ready = bool(self.window.asset_cards) and all(
            card._source_pixmap is not None
            for card in self.window.asset_cards.values()
        )
        return cards_ready and (thread is None or not thread.isRunning())

    def _capture(self) -> None:
        canvas = QPixmap(self.width, self.height)
        canvas.fill(QColor(BACKGROUND))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        main_pixmap = self.window.grab()
        painter.drawPixmap(QRect(0, 0, self.width, self.height), main_pixmap)

        dialog = self.preview_dialog
        if dialog is not None and dialog.isVisible():
            painter.fillRect(
                QRect(0, 0, self.width, self.height), QColor(15, 15, 20, 72)
            )
            dialog_pixmap = dialog.grab()
            margin_x, margin_y = 110, 70
            available_width = self.width - margin_x * 2
            available_height = self.height - margin_y * 2
            scaled = dialog_pixmap.scaled(
                available_width,
                available_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            left = (self.width - scaled.width()) // 2
            top = (self.height - scaled.height()) // 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 42))
            painter.drawRoundedRect(
                QRect(left + 8, top + 12, scaled.width(), scaled.height()),
                22,
                22,
            )
            painter.drawPixmap(left, top, scaled)

        if self.show_caption:
            font = QFont("Microsoft YaHei UI", 12)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(self.caption)
            pill = QRect(28, self.height - 70, text_width + 38, 42)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(29, 29, 31, 224))
            painter.drawRoundedRect(pill, 21, 21)
            painter.setPen(QColor("white"))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self.caption)
        painter.end()

        target = self.frame_dir / f"frame_{self.frame_number:06d}.png"
        if not canvas.save(str(target), "PNG"):
            raise RuntimeError(f"Could not save frame: {target}")
        self.frame_number += 1

    def _tick(self) -> None:
        try:
            self._capture()
            if time.monotonic() - self.started > self.timeout_seconds:
                raise TimeoutError(
                    f"Demo did not finish within {self.timeout_seconds} seconds"
                )
            self._drive()
        except Exception as exc:
            self.failure = str(exc)
            self._finish()

    def _drive(self) -> None:
        elapsed = self._elapsed_in_state()

        if self.state == "welcome" and elapsed >= 1.4:
            self.window._load_input(self.pdf_path)
            self.window.output_info.setText(
                r"D:\Demo\PDF_Size_Reducer_Stress_Demo_压缩至58.75MB.pdf"
            )
            self._advance(
                "loading",
                "后台读取 97.92 MB PDF · 界面始终响应",
            )
            return

        if self.state == "loading":
            if not self.window.assets_loading and self._thumbnail_work_finished():
                self._advance(
                    "overview",
                    "48 个完整 Figure 已生成全景缩略图",
                )
            return

        if self.state == "overview" and elapsed >= 2.0:
            self._advance("scroll", "在主界面快速浏览所有 Figure")
            return

        if self.state == "scroll":
            scroll = self.window.preview_scroll.verticalScrollBar()
            progress = min(1.0, elapsed / 3.0)
            if progress <= 0.55:
                value = int(scroll.maximum() * (progress / 0.55) * 0.62)
            else:
                value = int(
                    scroll.maximum()
                    * 0.62
                    * (1.0 - (progress - 0.55) / 0.45)
                )
            scroll.setValue(max(0, value))
            if progress >= 1.0:
                scroll.setValue(0)
                first_asset = self.window.assets[0]
                self.window._open_preview(first_asset)
                self.preview_dialog = self.window.preview_dialogs[-1]
                self._advance("preview_wait", "点击缩略图，打开高清全景预览")
            return

        if self.state == "preview_wait":
            if self.preview_dialog and self.preview_dialog.pixmap_item is not None:
                self._advance("preview", "完整 Figure 全景 · 文字与细线清晰可辨")
            return

        if self.state == "preview":
            if elapsed >= 1.3 and self.zoom_steps == 0:
                self.preview_dialog._scale(1.18)
                self.zoom_steps = 1
                self.caption = "滚轮或按钮放大，检查图中细节"
            elif elapsed >= 2.1 and self.zoom_steps == 1:
                self.preview_dialog._scale(1.18)
                self.zoom_steps = 2
            elif elapsed >= 3.4:
                self.preview_dialog.close()
                self.preview_dialog = None
                first_cards = list(self.window.asset_cards.values())[:2]
                for card in first_cards:
                    card.checkbox.setChecked(False)
                self.window.target_edit.setText("100")
                index = self.window.unit_combo.findText("MB")
                if index >= 0:
                    self.window.unit_combo.setCurrentIndex(index)
                self.window.output_path = self.output_path
                self.window.output_custom = True
                self.window.output_info.setText(
                    r"D:\Demo\PDF_Size_Reducer_Stress_Demo_压缩至100MB.pdf"
                )
                self._advance(
                    "configure",
                    "取消无需处理的 Figure，并精确输入目标大小",
                )
            return

        if self.state == "configure" and elapsed >= 2.2:
            self.window.start_compression()
            self._advance("compress", "开始定容 · 原生文字层保持不变")
            return

        if self.state == "compress":
            thread = self.window.compression_thread
            if self.window.last_output and (thread is None or not thread.isRunning()):
                self._advance("complete", "处理完成 · 输出文件已安全生成")
            return

        if self.state == "complete" and elapsed >= 2.5:
            self._finish()

    def _finish(self) -> None:
        self.timer.stop()
        if self.output_path.exists():
            self.output_path.unlink()
        self.window.close()
        print(f"frames={self.frame_number}")
        print(f"duration={self.frame_number / self.fps:.3f}")
        print(f"frame_dir={self.frame_dir}")
        if self.failure:
            print(f"error={self.failure}", file=sys.stderr)
            self.app.exit(1)
        else:
            self.app.quit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF used by the recorded demo")
    parser.add_argument("frame_dir", type=Path, help="PNG frame output directory")
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument(
        "--no-caption",
        action="store_true",
        help="Capture clean application frames without the demo caption pill",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_path = args.pdf.resolve()
    frame_dir = args.frame_dir.resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise SystemExit(f"PDF does not exist: {pdf_path}")
    if args.fps < 1 or args.fps > 30:
        raise SystemExit("--fps must be between 1 and 30")

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.resize(args.width, args.height)
    window.show()

    recorder = DemoRecorder(
        app,
        window,
        pdf_path,
        frame_dir,
        args.fps,
        args.width,
        args.height,
        args.timeout,
        not args.no_caption,
    )
    QTimer.singleShot(250, recorder.start)
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
