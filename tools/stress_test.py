"""Generate a large synthetic paper PDF and measure Qt UI responsiveness."""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageOps
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from qt_app import MainWindow


def _synthetic_chart(page_index: int, width: int, height: int) -> bytes:
    """Return a high-entropy JPEG resembling a dense scientific heatmap."""
    noise = Image.effect_noise((width, height), 92 + page_index % 7)
    dark = (
        10 + page_index * 17 % 35,
        18 + page_index * 11 % 45,
        50 + page_index * 7 % 55,
    )
    light = (
        215 + page_index * 5 % 40,
        225 + page_index * 3 % 30,
        240 + page_index * 13 % 15,
    )
    image = ImageOps.colorize(noise, black=dark, white=light)
    draw = ImageDraw.Draw(image)
    grid_color = (255, 255, 255)
    for x in range(0, width, max(80, width // 16)):
        draw.line((x, 0, x, height), fill=grid_color, width=1)
    for y in range(0, height, max(70, height // 12)):
        draw.line((0, y, width, y), fill=grid_color, width=1)
    draw.rectangle((18, 18, 390, 76), fill=(255, 255, 255), outline=(45, 45, 60))
    draw.text((34, 35), f"Synthetic heatmap / page {page_index + 1}", fill=(20, 20, 30))
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=95,
        subsampling=0,
        optimize=False,
    )
    return buffer.getvalue()


def generate_stress_pdf(
    destination: Path,
    pages: int,
    image_width: int,
    image_height: int,
    vector_paths: int,
) -> float:
    """Create a multi-page PDF with unique bitmaps, paths, labels and captions."""
    started = time.perf_counter()
    document = fitz.open()
    try:
        for page_index in range(pages):
            page = document.new_page(width=900, height=650)
            image_bytes = _synthetic_chart(
                page_index, image_width, image_height
            )
            figure_rect = fitz.Rect(35, 42, 865, 535)
            page.insert_image(figure_rect, stream=image_bytes)
            for line_index in range(vector_paths):
                x0 = 45 + line_index * 811 / max(1, vector_paths - 1)
                phase = line_index * 0.31 + page_index * 0.17
                y0 = 285 + math.sin(phase) * 205
                y1 = 285 + math.cos(phase * 1.13) * 205
                color = (
                    (line_index * 17 % 255) / 255,
                    (line_index * 29 % 255) / 255,
                    (line_index * 43 % 255) / 255,
                )
                page.draw_line(
                    fitz.Point(x0, y0),
                    fitz.Point(865 - (x0 - 35), y1),
                    color=color,
                    width=0.45 + line_index % 3 * 0.2,
                    overlay=True,
                )
            page.insert_text(
                (55, 568),
                f"Fig. {page_index + 1}: Synthetic dense scientific chart for stress testing.",
                fontsize=12,
                fontname="helv",
            )
            page.insert_text(
                (55, 610),
                "Body text remains native, searchable, and outside the selected Figure.",
                fontsize=10,
                fontname="helv",
            )
        document.save(destination, garbage=4, deflate=True)
    finally:
        document.close()
    return time.perf_counter() - started


def _peak_working_set_mib() -> float | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    get_memory_info.restype = ctypes.c_int
    current_process = get_current_process()
    succeeded = get_memory_info(
        current_process,
        ctypes.byref(counters),
        counters.cb,
    )
    if not succeeded:
        return None
    return counters.PeakWorkingSetSize / 1024**2


def run_ui_stress_test(
    pdf_path: Path,
    timeout_seconds: int,
    cancel_after_ms: int = 0,
) -> dict[str, object]:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.resize(1280, 820)
    window.show()

    heartbeat_ticks = 0
    max_gap_seconds = 0.0
    max_gap_context = ""
    last_heartbeat = time.perf_counter()
    timed_out = False
    cancel_requested = False
    progress_values: list[int] = []
    started = time.perf_counter()

    heartbeat = QTimer()
    heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
    heartbeat.setInterval(16)

    def record_heartbeat() -> None:
        nonlocal heartbeat_ticks, max_gap_seconds, last_heartbeat, max_gap_context
        now = time.perf_counter()
        gap = now - last_heartbeat
        if gap > max_gap_seconds:
            max_gap_seconds = gap
            thumbnails_running = bool(
                window.thumbnail_thread
                and window.thumbnail_thread.isRunning()
            )
            max_gap_context = (
                f"status={window.status_label.text()}; "
                f"loading={window.assets_loading}; "
                f"cards={len(window.asset_cards)}; "
                f"thumbnails_running={thumbnails_running}"
            )
        last_heartbeat = now
        heartbeat_ticks += 1

    heartbeat.timeout.connect(record_heartbeat)
    heartbeat.start()

    window._load_input(pdf_path)
    if window.scan_worker:
        window.scan_worker.progress.connect(
            lambda _generation, value, _message: progress_values.append(value)
        )

    scan_completed_at: float | None = None
    watcher = QTimer()
    watcher.setInterval(40)

    def inspect_state() -> None:
        nonlocal scan_completed_at
        if not window.assets_loading and scan_completed_at is None:
            scan_completed_at = time.perf_counter()
        thumbnails_running = bool(
            window.thumbnail_thread and window.thumbnail_thread.isRunning()
        )
        if scan_completed_at is not None and not thumbnails_running:
            watcher.stop()
            heartbeat.stop()
            window.close()
            app.quit()

    watcher.timeout.connect(inspect_state)
    watcher.start()

    def handle_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        window.cancel_scan()
        if window.thumbnail_worker:
            window.thumbnail_worker.cancel()
        watcher.stop()
        heartbeat.stop()
        window.close()
        app.quit()

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.setInterval(timeout_seconds * 1000)
    timeout.timeout.connect(handle_timeout)
    timeout.start()

    cancel_timer = QTimer()
    cancel_timer.setSingleShot(True)
    if cancel_after_ms > 0:
        def request_cancel() -> None:
            nonlocal cancel_requested
            cancel_requested = True
            window.cancel_scan()

        cancel_timer.setInterval(cancel_after_ms)
        cancel_timer.timeout.connect(request_cancel)
        cancel_timer.start()

    app.exec()
    finished = time.perf_counter()
    scan_seconds = (
        None if scan_completed_at is None else scan_completed_at - started
    )
    gaps_ms = max_gap_seconds * 1000
    progress_monotonic = all(
        current <= following
        for current, following in zip(progress_values, progress_values[1:])
    )
    peak_working_set = _peak_working_set_mib()
    scan_succeeded = len(window.assets) > 0 and scan_completed_at is not None
    cancelled_cleanly = (
        cancel_requested
        and not timed_out
        and not window.assets_loading
        and not window.assets
    )
    return {
        "pdf": str(pdf_path),
        "pdf_size_mib": round(pdf_path.stat().st_size / 1024**2, 2),
        "pages": _page_count(pdf_path),
        "assets_detected": len(window.assets),
        "scan_seconds": None if scan_seconds is None else round(scan_seconds, 3),
        "scan_and_thumbnails_seconds": round(finished - started, 3),
        "heartbeat_ticks": heartbeat_ticks,
        "max_event_loop_gap_ms": round(gaps_ms, 2),
        "max_gap_context": max_gap_context,
        "ui_responsive": not timed_out and gaps_ms < 250,
        "scan_succeeded": scan_succeeded,
        "cancel_requested": cancel_requested,
        "cancelled_cleanly": cancelled_cleanly,
        "progress_monotonic": progress_monotonic,
        "progress_updates": len(progress_values),
        "timed_out": timed_out,
        "peak_working_set_mib": (
            None if peak_working_set is None else round(peak_working_set, 2)
        ),
    }


def _page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as document:
        return document.page_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="Use an existing PDF")
    parser.add_argument("--pages", type=int, default=48)
    parser.add_argument("--image-width", type=int, default=1600)
    parser.add_argument("--image-height", type=int, default=1000)
    parser.add_argument("--vector-paths", type=int, default=90)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--cancel-after-ms",
        type=int,
        default=0,
        help="Request a safe scan cancellation after this many milliseconds",
    )
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    generated = args.pdf is None
    generation_seconds: float | None = None
    if args.pdf:
        pdf_path = args.pdf.resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
    else:
        if args.keep:
            pdf_path = (
                args.output or PROJECT_ROOT / "stress-large-paper.pdf"
            ).resolve()
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="pdf-size-reducer-stress-"
            )
            pdf_path = Path(temporary_directory.name) / "large-paper.pdf"
        generation_seconds = generate_stress_pdf(
            pdf_path,
            pages=max(1, args.pages),
            image_width=max(640, args.image_width),
            image_height=max(400, args.image_height),
            vector_paths=max(0, args.vector_paths),
        )

    try:
        result = run_ui_stress_test(
            pdf_path,
            max(10, args.timeout),
            max(0, args.cancel_after_ms),
        )
        result["generated"] = generated
        result["generation_seconds"] = (
            None
            if generation_seconds is None
            else round(generation_seconds, 3)
        )
        result["temporary_pdf_removed"] = generated and not args.keep
        print(json.dumps(result, ensure_ascii=False, indent=2))
        expected_outcome = (
            result["cancelled_cleanly"]
            if args.cancel_after_ms > 0
            else result["scan_succeeded"]
        )
        return 0 if result["ui_responsive"] and expected_outcome else 1
    finally:
        if temporary_directory:
            temporary_directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
