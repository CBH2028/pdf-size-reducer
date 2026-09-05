from __future__ import annotations

import os
from pathlib import Path
import threading

import pymupdf as fitz
import pytest

import compressor
import native_worker


def text_pdf(path: Path) -> None:
    with fitz.open() as document:
        document.new_page().insert_text((40, 40), "Preserve this text")
        document.save(path)


@pytest.mark.parametrize("kind", ["invalid", "encrypted"])
def test_already_small_input_is_validated_before_output_replacement(tmp_path, kind):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    if kind == "invalid":
        source.write_bytes(b"This is not a PDF")
    else:
        with fitz.open() as document:
            document.new_page()
            document.save(source, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    output.write_bytes(b"existing output")
    with pytest.raises(compressor.CompressionError):
        compressor.compress_pdf(source, output, source.stat().st_size + 100)
    assert output.read_bytes() == b"existing output"


def test_lossless_success_does_not_parse_drawings_or_renumber_selections(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "out.pdf"
    text_pdf(source)
    # Legal trailing whitespace supplies a simple lossless size reduction.
    with source.open("ab") as stream:
        stream.write(b"\n" * 50000)

    def no_scan(*args, **kwargs):
        pytest.fail("A lossless success should not scan drawing/image candidates")

    monkeypatch.setattr(compressor, "_collect_images", no_scan)
    monkeypatch.setattr(compressor, "_collect_vector_pages", no_scan)
    result = compressor.compress_pdf(source, output, 10000)
    assert result.method == "lossless"
    with fitz.open(output) as document:
        assert "Preserve this text" in document[0].get_text()


def test_unselected_vectors_are_not_parsed(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "out.pdf"
    text_pdf(source)

    def no_drawings(*args, **kwargs):
        pytest.fail("Explicitly unselected vector pages must not be parsed")

    monkeypatch.setattr(fitz.Page, "get_cdrawings", no_drawings)
    with pytest.raises(compressor.NoCompressibleImagesError, match="没有勾选"):
        compressor.compress_pdf(source, output, 10, selected_image_xrefs=set(), selected_vector_pages=set())


@pytest.mark.parametrize("native", [False, True])
def test_merge_retains_interactive_fields(tmp_path, monkeypatch, native):
    monkeypatch.setenv("PDF_SIZE_REDUCER_DISABLE_NATIVE", "0" if native else "1")
    native_worker.find_native_worker.cache_clear()
    first, second, output = tmp_path / "first.pdf", tmp_path / "form.pdf", tmp_path / "out.pdf"
    text_pdf(first)
    with fitz.open() as document:
        page = document.new_page()
        widget = fitz.Widget()
        widget.field_name = "applicant"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.field_value = "Alice"
        widget.rect = fitz.Rect(40, 50, 240, 80)
        page.add_widget(widget)
        document.save(second)
    try:
        result = compressor.merge_pdfs([first, second], output)
        assert not result.native_worker_used
        with fitz.open(output) as document:
            page = document[1]
            widget = next(page.widgets())
            assert widget.field_name == "applicant"
            assert widget.field_value == "Alice"
            widget.field_value = "Bob"
            widget.update()
            document.saveIncr()
        with fitz.open(output) as document:
            assert next(document[1].widgets()).field_value == "Bob"
        if native and native_worker.find_native_worker() is not None:
            workspace = tmp_path / "direct"
            workspace.mkdir()
            with pytest.raises(native_worker.NativeWorkerError, match="forms|form"):
                native_worker.merge_pdf_pages_native([first, second], workspace / "out.pdf", workspace)
    finally:
        native_worker.find_native_worker.cache_clear()


def test_native_merge_preserves_rotated_and_overlapping_links(tmp_path, monkeypatch):
    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_NATIVE", raising=False)
    native_worker.find_native_worker.cache_clear()
    if native_worker.find_native_worker() is None:
        pytest.skip("Build the native worker first")
    first, second, output = tmp_path / "first.pdf", tmp_path / "links.pdf", tmp_path / "out.pdf"
    text_pdf(first)
    with fitz.open() as document:
        document.new_page(width=300, height=400)
        document.new_page(width=500, height=600)
        document[0].set_rotation(90)
        document[1].set_rotation(270)
        rect = fitz.Rect(15, 20, 80, 45)
        for uri in ("https://example.com/first", "https://example.com/second"):
            document[0].insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": uri})
        document[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(20, 80, 90, 110), "page": 1, "to": fitz.Point(30, 50)})
        document.save(second)
    with fitz.open(second) as document:
        expected = document[0].get_links()
    result = compressor.merge_pdfs([first, second], output)
    assert result.native_worker_used
    with fitz.open(output) as document:
        actual = document[1].get_links()
        assert len(actual) == len(expected)
        for before, after in zip(expected, actual):
            assert after["from"] == before["from"]
            if before["kind"] == fitz.LINK_GOTO:
                assert after["page"] == before["page"] + 1
                assert after["to"] == before["to"]
            else:
                assert after["uri"] == before["uri"]


def test_worker_launch_failure_falls_back_and_progress_never_regresses(tmp_path, monkeypatch):
    first, second, output = tmp_path / "first.pdf", tmp_path / "second.pdf", tmp_path / "out.pdf"
    text_pdf(first)
    text_pdf(second)
    monkeypatch.setattr(compressor, "find_native_worker", lambda: Path("available.exe"))
    monkeypatch.setattr(native_worker, "find_native_worker", lambda: Path("available.exe"))

    def failed_launch(*args, **kwargs):
        raise OSError("worker could not be launched")

    monkeypatch.setattr(native_worker.subprocess, "Popen", failed_launch)
    result = compressor.merge_pdfs([first, second], output)
    assert not result.native_worker_used

    def failed_after_progress(*args, **kwargs):
        kwargs["progress_callback"](2, 2)
        raise native_worker.NativeWorkerError("late worker failure")

    monkeypatch.setattr(compressor, "merge_pdf_pages_native", failed_after_progress)
    progress = []
    compressor.merge_pdfs([first, second], output, progress_callback=lambda value, message: progress.append(value))
    assert progress == sorted(progress)
    assert progress[-1] == 100


def test_native_session_handshake_failure_closes_its_process(tmp_path, monkeypatch):
    text_pdf(tmp_path / "source.pdf")
    calls = []

    class Pipe:
        def close(self):
            pass

    class Process:
        stdin = Pipe()
        stdout = Pipe()
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append("terminate")
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(native_worker, "find_native_worker", lambda: Path("available.exe"))
    monkeypatch.setattr(native_worker, "_start_worker", lambda *args, **kwargs: Process())
    monkeypatch.setattr(native_worker, "_reader", lambda *args: None)

    def failed_hello(*args, **kwargs):
        raise native_worker.NativeWorkerError("broken hello")

    monkeypatch.setattr(native_worker.NativeWorkerSession, "_wait_for_response", failed_hello)
    with pytest.raises(native_worker.NativeWorkerError, match="broken hello"):
        native_worker.NativeWorkerSession(tmp_path / "source.pdf")
    assert calls == ["terminate"]


def _spawned_job(cancel_expected, result_queue, cancel_event):
    result_queue.put(("progress", 20, "Working"))
    if cancel_expected:
        if cancel_event.wait(5):
            result_queue.put(("cancelled",))
        else:
            result_queue.put(("failed", "Cancellation did not reach the child"))
    else:
        result_queue.put(("completed", os.getpid()))


@pytest.mark.parametrize("cancel", [False, True])
def test_background_pdf_process_is_isolated_and_receives_cancellation(cancel):
    import multiprocessing
    import qt_app

    records = []

    class Signal:
        def __init__(self, name):
            self.name = name

        def emit(self, *args):
            records.append((self.name, *args))
            if self.name == "progress" and cancel:
                worker.cancel_event.set()

    class Worker:
        cancel_event = threading.Event()
        progress = Signal("progress")
        completed = Signal("completed")
        failed = Signal("failed")
        cancelled = Signal("cancelled")

    before = {child.pid for child in multiprocessing.active_children()}
    worker = Worker()
    qt_app._run_pdf_process(worker, _spawned_job, (cancel,), "RegressionPDFJob")
    if cancel:
        assert records[-1] == ("cancelled",)
    else:
        assert records[-1][0] == "completed"
        assert records[-1][1] != os.getpid()
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_packaged_workflow_self_test_entry_point():
    import qt_app

    native_worker.find_native_worker.cache_clear()
    if native_worker.find_native_worker() is None:
        pytest.skip("Build the native worker first")
    assert qt_app._workflow_self_test() == 0
