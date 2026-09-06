from __future__ import annotations

import ctypes
import multiprocessing
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pymupdf as fitz
import pytest

import compressor
import qt_app


def make_pdf(path):
    with fitz.open() as document:
        document.new_page().insert_text((30, 30), "Document revision A")
        document.save(path)


def change_pdf(path):
    with Path(path).open("ab") as output:
        output.write(b"\n% external save\n")


def test_compression_rejects_stale_scan_even_when_file_fits(tmp_path):
    source, destination = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    scanned = compressor.get_pdf_source_state(source)
    change_pdf(source)
    destination.write_bytes(b"previous output")
    with pytest.raises(compressor.CompressionError, match="预览后已发生变化"):
        compressor.compress_pdf(source, destination, 100_000, expected_source_state=scanned)
    assert destination.read_bytes() == b"previous output"


@pytest.mark.parametrize("mode", ["copied", "lossless"])
def test_source_edit_during_compression_keeps_previous_output(tmp_path, monkeypatch, mode):
    source, destination = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    destination.write_bytes(b"previous output")
    if mode == "copied":
        original = compressor.shutil.copy2

        def modify_after_copy(*args, **kwargs):
            original(*args, **kwargs)
            change_pdf(source)

        monkeypatch.setattr(compressor.shutil, "copy2", modify_after_copy)
    else:
        with source.open("ab") as output:
            output.write(b"\n" * 100_000)
        original = compressor._save_lossless

        def modify_after_save(*args, **kwargs):
            original(*args, **kwargs)
            change_pdf(source)

        monkeypatch.setattr(compressor, "_save_lossless", modify_after_save)
    with pytest.raises(compressor.CompressionError, match="处理期间发生变化"):
        compressor.compress_pdf(source, destination, 10_000)
    assert destination.read_bytes() == b"previous output"


def test_source_edit_during_merge_keeps_previous_output(tmp_path):
    source, second, destination = [tmp_path / name for name in ("first.pdf", "second.pdf", "result.pdf")]
    make_pdf(source)
    make_pdf(second)
    destination.write_bytes(b"previous output")

    def progress(value, _message):
        if value == 94:
            change_pdf(source)

    with pytest.raises(compressor.CompressionError, match="处理期间发生变化"):
        compressor.merge_pdfs([source, second], destination, progress_callback=progress)
    assert destination.read_bytes() == b"previous output"


def test_repeated_merge_sources_count_bytes_and_pages(tmp_path):
    source, destination = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    result = compressor.merge_pdfs([source, source], destination)
    assert result.page_count == 2
    assert result.input_bytes == source.stat().st_size * 2


def test_scan_rejects_file_changed_during_detection(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    make_pdf(source)

    def scan(*args, **kwargs):
        change_pdf(source)
        return [], 1

    monkeypatch.setattr(qt_app, "list_pdf_assets", scan)
    messages = queue.Queue()
    qt_app._asset_scan_process(str(source), messages, threading.Event())
    result = messages.get_nowait()
    assert result[0] == "failed"
    assert "扫描期间发生变化" in result[1]


@pytest.mark.parametrize("operation", ["compress", "merge"])
def test_desktop_cancel_at_final_progress_never_installs_output(tmp_path, operation):
    first, second, destination = [tmp_path / name for name in ("first.pdf", "second.pdf", "result.pdf")]
    make_pdf(first)
    make_pdf(second)
    destination.write_bytes(b"previous output")
    worker = (
        qt_app.MergeWorker([first, second], destination) if operation == "merge" else
        qt_app.CompressionWorker(first, destination, 100_000, set(), set(), {})
    )
    terminals = []
    worker.completed.connect(lambda result: terminals.append("completed"))
    worker.cancelled.connect(lambda: terminals.append("cancelled"))
    worker.failed.connect(lambda error: terminals.append(error))
    worker.progress.connect(lambda value, _text: worker.cancel() if value == 99 else None)
    worker.run()
    assert terminals == ["cancelled"]
    assert destination.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".pdf_desktop_job_*"))


def _hang_with_native_descendant(pid_path, result_queue, cancel_event):
    # A real grandchild proves cancellation stops more than the Python worker.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    Path(pid_path).write_text(str(child.pid), encoding="ascii")
    result_queue.put(("progress", 20, "ready"))
    time.sleep(60)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree regression")
def test_cancel_stops_unresponsive_worker_and_descendant(tmp_path):
    records = []
    pid_path = tmp_path / "descendant.pid"

    class Signal:
        def __init__(self, kind):
            self.kind = kind

        def emit(self, *args):
            records.append((self.kind, *args))
            if self.kind == "progress":
                worker.cancel_event.set()

    class Worker:
        cancel_event = threading.Event()
        progress = Signal("progress")
        cancelled = Signal("cancelled")
        completed = Signal("completed")
        failed = Signal("failed")

    worker = Worker()
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    qt_app._run_pdf_process(worker, _hang_with_native_descendant, (pid_path,), "StuckPDFTest", cancel_after=0.2)
    assert time.monotonic() - started < 8
    assert records[-1] == ("cancelled",)
    assert {child.pid for child in multiprocessing.active_children()} == before
    pid = int(pid_path.read_text(encoding="ascii"))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 3000) == 0
        finally:
            kernel.CloseHandle(handle)


def test_preview_returns_image_from_isolated_worker(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    asset = compressor.PDFAsset("figure:0", "figure", (0,), rect=(10, 10, 300, 100))
    worker = qt_app.PreviewRenderWorker(source, asset, compressor.get_pdf_source_state(source))
    completed, errors = [], []
    worker.completed.connect(completed.append)
    worker.failed.connect(errors.append)
    worker.run()
    assert not errors
    assert len(completed) == 1 and completed[0].startswith(b"\x89PNG")


@pytest.mark.parametrize("kind", ["scan", "thumbnails"])
def test_read_worker_start_failure_reports_terminal_status(tmp_path, monkeypatch, kind):
    from multiprocessing.process import BaseProcess

    def cannot_start(self):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(BaseProcess, "start", cannot_start)
    source = tmp_path / "source.pdf"
    make_pdf(source)
    records = []
    if kind == "scan":
        worker = qt_app.AssetScanWorker(source, 1)
        worker.failed.connect(lambda generation, path, error: records.append(error))
    else:
        worker = qt_app.ThumbnailWorker(source, [compressor.PDFAsset("figure:0", "figure", (0,), rect=(10, 10, 200, 100))], 1)
        worker.ready.connect(lambda generation, index, error: records.append(str(error)))
        worker.done.connect(lambda generation: records.append("done"))
    worker.run()
    assert "simulated spawn failure" in records[0]
    if kind == "thumbnails":
        assert records[-1] == "done"
    assert worker._process_cancel_event is None


def _slow_preview(path, asset, destination, source_state, result_queue, cancel_event):
    result_queue.put(("progress", 20, "ready"))
    time.sleep(60)


def test_closing_preview_stops_render_and_reuses_existing_window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QThread

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.pdf"
    make_pdf(source)
    asset = compressor.PDFAsset("figure:0", "figure", (0,), rect=(10, 10, 200, 100))
    monkeypatch.setattr(qt_app, "_preview_process", _slow_preview)
    window = qt_app.MainWindow()
    window.input_path = source
    window.input_source_state = compressor.get_pdf_source_state(source)
    try:
        window._open_preview(asset)
        dialog = window.preview_dialogs[0]
        ready = []
        dialog.worker.progress.connect(lambda *args: ready.append(True))
        window._open_preview(asset)
        assert window.preview_dialogs == [dialog]
        deadline = time.monotonic() + 10
        while not ready:
            app.processEvents()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        started = time.monotonic()
        dialog.close()
        while any(thread.isRunning() for thread in window.findChildren(QThread)):
            app.processEvents()
            assert time.monotonic() - started < 5
            time.sleep(0.01)
        assert not window.preview_dialogs
    finally:
        window.close()
        app.processEvents()


def _incomplete_writer(source, destination, behavior, result_queue, cancel_event):
    # Simulate a native writer stuck/crashed after writing some temporary data.
    Path(destination).write_bytes(b"%PDF-partial")
    nested = Path(destination).parent / ".pdf_size_reducer_partial"
    nested.mkdir()
    (nested / "candidate.pdf").write_bytes(b"unfinished")
    if behavior == "crash":
        os._exit(7)
    result_queue.put(("progress", 20, "ready"))
    time.sleep(60)


@pytest.mark.parametrize("behavior", ["cancel", "crash"])
def test_interrupted_desktop_writer_discards_partial_workspace(tmp_path, behavior):
    source, destination = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    destination.write_bytes(b"previous output")
    worker = qt_app.CompressionWorker(source, destination, 100_000, set(), set(), {})
    terminals = []
    worker.completed.connect(lambda result: terminals.append("completed"))
    worker.failed.connect(lambda error: terminals.append("failed"))
    worker.cancelled.connect(lambda: terminals.append("cancelled"))
    worker.progress.connect(lambda *args: worker.cancel())
    qt_app._run_staged_pdf_process(
        worker, _incomplete_writer, (source, destination, behavior), "InterruptedWriterTest",
    )
    assert terminals == ["cancelled" if behavior == "cancel" else "failed"]
    assert destination.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".pdf_desktop_job_*"))
