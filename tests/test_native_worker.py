from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

import native_worker
import compressor
from compressor import compress_pdf, merge_pdfs
from native_worker import (
    NativeRenderRequest,
    NativeWorkerCancelled,
    NativeWorkerError,
    NativeWorkerSession,
    merge_pdf_pages_native,
)


def _make_native_fixture(path: Path) -> tuple[float, float, float, float]:
    document = fitz.open()
    page = document.new_page(width=320, height=240)
    figure = fitz.Rect(30, 30, 290, 180)
    page.draw_rect(figure, color=(0, 0.5, 0), width=2)
    page.insert_text(
        (50, 70),
        "NATIVE TEXT SHOULD STAY PDF",
        fontsize=18,
        fontname="helv",
    )
    page.draw_line(fitz.Point(45, 155), fitz.Point(275, 105), color=(0, 0, 1))
    document.save(path)
    document.close()
    return tuple(figure)


@pytest.fixture
def native_binary(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_NATIVE", raising=False)
    native_worker.find_native_worker.cache_clear()
    binary = native_worker.find_native_worker()
    if binary is None:
        pytest.skip("native worker has not been built")
    return binary


def test_native_worker_protocol(native_binary: Path) -> None:
    assert native_binary.name == "pdf_fast_worker.exe"
    assert native_binary.with_name("pdf_fast_worker_backend.exe").is_file()
    assert native_binary.with_name("mupdfcpp64.dll").is_file()
    completed = subprocess.run(
        [str(native_binary), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    response = json.loads(completed.stdout)
    assert response["security_guard"] == 1
    assert response["memory_limit_mib"] == 1536
    assert response["active_process_limit"] == 2
    assert response["capabilities"] == ["render", "ladder", "merge"]
    assert len(response["backend_sha256"]) == 64
    assert len(response["mupdf_sha256"]) == 64


def _make_merge_pdf(path: Path, labels: list[str]) -> None:
    document = fitz.open()
    for label in labels:
        page = document.new_page()
        page.insert_text((72, 72), label)
    document[0].add_text_annot((120, 120), f"Note {labels[0]}")
    document.save(path)
    document.close()


def test_guarded_native_merge_copies_pages_and_annotations(
    tmp_path: Path, native_binary: Path
) -> None:
    source_directory = tmp_path / "中文 source"
    source_directory.mkdir()
    first = source_directory / "第一份.pdf"
    second = source_directory / "第二份.pdf"
    output = tmp_path / "native-merged.pdf"
    _make_merge_pdf(first, ["FIRST"])
    _make_merge_pdf(second, ["SECOND-A", "SECOND-B"])
    progress: list[tuple[int, int]] = []

    result = merge_pdf_pages_native(
        [first, second],
        output,
        tmp_path,
        progress_callback=lambda completed, total: progress.append(
            (completed, total)
        ),
    )

    assert result.source_count == 2
    assert result.page_count == 3
    assert result.output_bytes == output.stat().st_size
    assert result.elapsed_seconds >= 0
    assert progress == [(1, 2), (2, 2)]
    with fitz.open(output) as document:
        assert "FIRST" in document[0].get_text()
        assert "SECOND-A" in document[1].get_text()
        assert "SECOND-B" in document[2].get_text()
        assert next(document[0].annots()).info["content"] == "Note FIRST"


def test_rust_guard_rejects_native_merge_output_escape(
    tmp_path: Path, native_binary: Path
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _make_merge_pdf(first, ["FIRST"])
    _make_merge_pdf(second, ["SECOND"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = workspace / "native-merge-inputs.tsv"
    manifest.write_text(
        f"0\t{first.resolve()}\n1\t{second.resolve()}\n",
        encoding="utf-8",
    )
    escaped = tmp_path / "escaped.pdf"

    completed = subprocess.run(
        [
            str(native_binary),
            "merge",
            "--manifest",
            str(manifest),
            "--output",
            str(escaped),
            "--workspace",
            str(workspace),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 64
    assert "private workspace" in completed.stderr
    assert not escaped.exists()


def test_merge_uses_native_accelerator_and_falls_back_safely(
    tmp_path: Path,
    native_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    accelerated = tmp_path / "accelerated.pdf"
    fallback = tmp_path / "fallback.pdf"
    mismatched = tmp_path / "mismatched.pdf"
    _make_merge_pdf(first, ["FIRST"])
    _make_merge_pdf(second, ["SECOND"])

    accelerated_result = merge_pdfs([first, second], accelerated)
    assert accelerated_result.native_worker_used is True
    assert accelerated_result.native_merge_seconds >= 0

    def unavailable(*_args, **_kwargs):
        raise NativeWorkerError("simulated native failure")

    monkeypatch.setattr(compressor, "merge_pdf_pages_native", unavailable)
    fallback_result = merge_pdfs([first, second], fallback)
    assert fallback_result.native_worker_used is False
    with fitz.open(fallback) as document:
        assert document.page_count == 2
        assert "FIRST" in document[0].get_text()
        assert "SECOND" in document[1].get_text()

    def wrong_page_count(_sources, candidate, _workspace, **_kwargs):
        Path(candidate).write_bytes(b"%PDF-bad-candidate")
        return native_worker.NativeMergeResult(
            Path(candidate), 2, 3, 18, 0.01
        )

    monkeypatch.setattr(
        compressor, "merge_pdf_pages_native", wrong_page_count
    )
    mismatched_result = merge_pdfs([first, second], mismatched)
    assert mismatched_result.native_worker_used is False
    with fitz.open(mismatched) as document:
        assert document.page_count == 2


def test_persistent_native_session_culls_text(
    tmp_path: Path, native_binary: Path
) -> None:
    unicode_directory = tmp_path / "中文 路径"
    unicode_directory.mkdir()
    source = unicode_directory / "原始 文件.pdf"
    rectangle = _make_native_fixture(source)
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_request = NativeRenderRequest(0, 0, rectangle, 144, 85)
    second_request = NativeRenderRequest(0, 0, rectangle, 150, 85)

    with NativeWorkerSession(source, threads=2, workspace=tmp_path) as session:
        process_id = session.process.pid
        first = session.render([first_request], first_directory)[0]
        second = session.render([second_request], second_directory)[0]
        assert session.process.pid == process_id

    first_image = Image.open(io.BytesIO(first)).convert("RGB")
    second_image = Image.open(io.BytesIO(second)).convert("RGB")
    assert first_image.size == (520, 300)
    assert second_image.width > first_image.width

    # The expensive JPEG layer excludes native text. The compressor keeps the
    # real PDF text above it, so glyphs stay searchable and are not duplicated.
    text_area = first_image.crop((35, 35, 505, 100)).convert("L")
    assert sum(text_area.histogram()[:80]) < 10


def test_native_ladder_reuses_one_master_render(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "ladder-fixture.pdf"
    rectangle = _make_native_fixture(source)
    requests = [
        NativeRenderRequest(0, 0, rectangle, 144, 80, 7),
        NativeRenderRequest(1, 0, rectangle, 144, 90, 7),
        NativeRenderRequest(2, 0, rectangle, 72, 75, 7),
    ]

    with NativeWorkerSession(source, threads=2) as session:
        rendered = session.render_ladder(requests, tmp_path / "ladder")
        response = session.last_response

    assert response["master_renders"] == 1
    assert response["variants"] == 3
    assert Image.open(io.BytesIO(rendered[0])).size == (520, 300)
    assert Image.open(io.BytesIO(rendered[1])).size == (520, 300)
    assert Image.open(io.BytesIO(rendered[2])).size == (260, 150)
    assert rendered[0] != rendered[1]


def test_native_session_rejects_unsafe_requests(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "unsafe-request.pdf"
    rectangle = _make_native_fixture(source)
    request = NativeRenderRequest(0, 0, rectangle, 1201, 85)

    with NativeWorkerSession(source, threads=1, workspace=tmp_path) as session:
        with pytest.raises(NativeWorkerError, match="DPI"):
            session.render([request], tmp_path / "unsafe-request")


def test_native_session_rejects_workspace_escape(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "workspace-source.pdf"
    rectangle = _make_native_fixture(source)
    workspace = tmp_path / "private"
    workspace.mkdir()
    request = NativeRenderRequest(0, 0, rectangle, 144, 85)

    with NativeWorkerSession(source, threads=1, workspace=workspace) as session:
        with pytest.raises(NativeWorkerError, match="escaped"):
            session.render([request], tmp_path / "outside")


def test_native_session_rejects_aggregate_pixel_dos(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "aggregate-limit.pdf"
    _make_native_fixture(source)
    requests = [
        NativeRenderRequest(item_id, 0, (0.0, 0.0, 720.0, 720.0), 720, 85)
        for item_id in range(40)
    ]

    with NativeWorkerSession(source, threads=1, workspace=tmp_path) as session:
        with pytest.raises(NativeWorkerError, match="2-gigapixel"):
            session.render(requests, tmp_path / "aggregate-output")


def test_rust_guard_rejects_manifest_path_traversal(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "guard-source.pdf"
    rectangle = _make_native_fixture(source)
    workspace = tmp_path / "workspace"
    output = workspace / "render"
    output.mkdir(parents=True)
    x0, y0, x1, y1 = rectangle
    manifest = output / "unsafe.tsv"
    manifest.write_text(
        f"0\t0\t{x0}\t{y0}\t{x1}\t{y1}\t144\t85\t../escape.jpg\t0\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            str(native_binary),
            "render-batch",
            "--input",
            str(source),
            "--manifest",
            str(manifest),
            "--output-dir",
            str(output),
            "--threads",
            "1",
            "--workspace",
            str(workspace),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 64
    assert "safe JPEG basenames" in completed.stderr
    assert not (workspace / "escape.jpg").exists()


def test_rust_guard_detects_backend_tampering(
    tmp_path: Path, native_binary: Path
) -> None:
    bundle = tmp_path / "tampered-worker"
    bundle.mkdir()
    for filename in (
        "pdf_fast_worker.exe",
        "pdf_fast_worker_backend.exe",
        "mupdfcpp64.dll",
    ):
        shutil.copy2(native_binary.with_name(filename), bundle / filename)
    with (bundle / "pdf_fast_worker_backend.exe").open("ab") as backend:
        backend.write(b"tampered")

    completed = subprocess.run(
        [str(bundle / "pdf_fast_worker.exe"), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 64
    assert "integrity verification failed" in completed.stderr


def test_rust_guard_detects_mupdf_tampering(
    tmp_path: Path, native_binary: Path
) -> None:
    bundle = tmp_path / "tampered-mupdf"
    bundle.mkdir()
    for filename in (
        "pdf_fast_worker.exe",
        "pdf_fast_worker_backend.exe",
        "mupdfcpp64.dll",
    ):
        shutil.copy2(native_binary.with_name(filename), bundle / filename)
    with (bundle / "mupdfcpp64.dll").open("ab") as runtime:
        runtime.write(b"tampered")

    completed = subprocess.run(
        [str(bundle / "pdf_fast_worker.exe"), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 64
    assert "MuPDF runtime integrity verification failed" in completed.stderr


def test_cpp_backend_rejects_unsafe_manifest_without_guard(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "backend-defense.pdf"
    rectangle = _make_native_fixture(source)
    output = tmp_path / "backend-output"
    output.mkdir()
    x0, y0, x1, y1 = rectangle
    manifest = output / "unsafe.tsv"
    manifest.write_text(
        f"0\t0\t{x0}\t{y0}\t{x1}\t{y1}\t1201\t85\tfigure.jpg\t0\n",
        encoding="utf-8",
    )
    backend = native_binary.with_name("pdf_fast_worker_backend.exe")

    completed = subprocess.run(
        [
            str(backend),
            "render-batch",
            "--input",
            str(source),
            "--manifest",
            str(manifest),
            "--output-dir",
            str(output),
            "--threads",
            "1",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    assert completed.returncode == 3
    assert "outside the safety range" in completed.stdout
    assert not (output / "figure.jpg").exists()


def test_compressor_uses_global_native_planner(
    tmp_path: Path,
    native_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "planner-source.pdf"
    output = tmp_path / "planner-output.pdf"
    rectangle = fitz.Rect(20, 20, 340, 200)
    bitmap = Image.new("RGB", (640, 320))
    bitmap.putdata(
        [
            (
                (x * 17 + y * 29) % 256,
                (x * 37 + y * 11) % 256,
                (x * 7 + y * 43) % 256,
            )
            for y in range(bitmap.height)
            for x in range(bitmap.width)
        ]
    )
    buffer = io.BytesIO()
    bitmap.save(buffer, format="PNG")
    document = fitz.open()
    page = document.new_page(width=360, height=240)
    page.insert_image(fitz.Rect(20, 20, 340, 180), stream=buffer.getvalue())
    page.draw_rect(rectangle, color=(0, 0, 1), width=2)
    page.insert_text((35, 55), "SEARCHABLE FIGURE TEXT", fontsize=12)
    document.save(source)
    document.close()

    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_NATIVE", raising=False)
    monkeypatch.delenv("PDF_SIZE_REDUCER_DISABLE_PLANNER", raising=False)
    result = compress_pdf(
        source,
        output,
        180_000,
        selected_image_xrefs=set(),
        selected_vector_pages=set(),
        selected_figure_regions={0: [tuple(rectangle)]},
    )

    assert result.planned_mode is True
    assert result.native_worker_used is True
    assert result.native_master_renders == 1
    assert result.planned_variants == 14
    assert result.candidate_attempts <= 2
    assert output.stat().st_size <= 180_000
    with fitz.open(output) as compressed:
        assert "SEARCHABLE FIGURE TEXT" in compressed[0].get_text()


def test_native_session_can_be_cancelled(
    tmp_path: Path, native_binary: Path
) -> None:
    source = tmp_path / "cancel-fixture.pdf"
    rectangle = _make_native_fixture(source)
    request = NativeRenderRequest(0, 0, rectangle, 600, 90)
    cancellation = threading.Event()
    cancellation.set()

    with NativeWorkerSession(source, threads=2) as session:
        with pytest.raises(NativeWorkerCancelled):
            session.render(
                [request],
                tmp_path / "cancelled",
                cancel_event=cancellation,
            )
        assert session.process.poll() is not None
