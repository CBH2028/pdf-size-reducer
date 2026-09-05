"""Python bridge for the optional C++ Figure-rendering worker."""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable


PROTOCOL_VERSION = 3
SECURITY_GUARD_VERSION = 1
MAX_NATIVE_TASKS = 4096
MAX_NATIVE_BATCH_PIXELS = 2_000_000_000
MAX_NATIVE_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_LINE_BYTES = 64 * 1024
DEFAULT_BATCH_TIMEOUT_SECONDS = 900
MAX_NATIVE_MERGE_SOURCES = 100
MAX_NATIVE_MERGE_OUTPUT_BYTES = 16 * 1024**3
ProgressCallback = Callable[[int, int], None]


class NativeWorkerError(RuntimeError):
    """The native process failed or returned an invalid response."""


class NativeWorkerCancelled(NativeWorkerError):
    """The caller stopped an active native render batch."""


@dataclass(frozen=True)
class NativeRenderRequest:
    id: int
    page_number: int
    rectangle: tuple[float, float, float, float]
    dpi: int
    jpeg_quality: int
    group_id: int = 0


@dataclass(frozen=True)
class NativeMergeResult:
    output_path: Path
    source_count: int
    page_count: int
    output_bytes: int
    elapsed_seconds: float


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    configured = os.environ.get("PDF_SIZE_REDUCER_NATIVE_WORKER", "").strip()
    if configured:
        paths.append(Path(configured).expanduser())
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        paths.append(Path(bundle_root) / "native_worker" / "pdf_fast_worker.exe")
    paths.append(
        Path(__file__).resolve().parent
        / "native_worker"
        / "bin"
        / "pdf_fast_worker.exe"
    )
    return paths


def _worker_environment(worker_directory: Path) -> dict[str, str]:
    """Build a minimal environment for the guarded native subprocess."""
    environment = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    path_parts = [str(worker_directory)]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        path_parts.append(str(Path(bundle_root) / "pymupdf"))
    system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    if system_root:
        path_parts.append(str(Path(system_root) / "System32"))
    environment["PATH"] = os.pathsep.join(path_parts)
    memory_limit = os.environ.get(
        "PDF_SIZE_REDUCER_WORKER_MEMORY_MIB", ""
    ).strip()
    if memory_limit:
        environment["PDF_SIZE_REDUCER_WORKER_MEMORY_MIB"] = memory_limit
    return environment


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NativeWorkerError(f"C++ worker returned an invalid {label}.")
    return value


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _validate_requests(requests: list[NativeRenderRequest]) -> None:
    if len(requests) > MAX_NATIVE_TASKS:
        raise NativeWorkerError(
            f"A native batch cannot exceed {MAX_NATIVE_TASKS} tasks."
        )
    identifiers: set[int] = set()
    total_pixels = 0.0
    for request in requests:
        integer_fields = (
            (request.id, "task id", 0, 1_000_000),
            (request.page_number, "page number", 0, 1_000_000),
            (request.group_id, "group id", 0, 1_000_000),
            (request.dpi, "DPI", 24, 1200),
            (request.jpeg_quality, "JPEG quality", 35, 100),
        )
        for value, label, minimum, maximum in integer_fields:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise NativeWorkerError(
                    f"Native {label} is outside the safety range."
                )
        if request.id in identifiers:
            raise NativeWorkerError("Native task ids must be unique.")
        identifiers.add(request.id)
        if (
            not isinstance(request.rectangle, tuple)
            or len(request.rectangle) != 4
        ):
            raise NativeWorkerError(
                "Native Figure coordinates are outside the safety range."
            )
        if not all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and abs(value) <= 1_000_000
            for value in request.rectangle
        ):
            raise NativeWorkerError(
                "Native Figure coordinates are outside the safety range."
            )
        x0, y0, x1, y1 = request.rectangle
        if x1 <= x0 or y1 <= y0:
            raise NativeWorkerError("Native Figure rectangle has no area.")
        width = (x1 - x0) * request.dpi / 72
        height = (y1 - y0) * request.dpi / 72
        task_pixels = width * height
        if task_pixels > 100_000_000:
            raise NativeWorkerError(
                "Native Figure render exceeds the 100-megapixel safety limit."
            )
        total_pixels += task_pixels
        if total_pixels > MAX_NATIVE_BATCH_PIXELS:
            raise NativeWorkerError(
                "Native batch exceeds the 2-gigapixel safety limit."
            )


@lru_cache(maxsize=1)
def find_native_worker() -> Path | None:
    """Return a protocol-compatible worker, if one is installed."""
    if os.environ.get("PDF_SIZE_REDUCER_DISABLE_NATIVE", "").strip() == "1":
        return None
    for candidate in _candidate_paths():
        try:
            resolved = candidate.resolve()
            if not resolved.is_file():
                continue
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            completed = subprocess.run(
                [str(resolved), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=flags,
                env=_worker_environment(resolved.parent),
            )
            if completed.returncode:
                continue
            response_lines = completed.stdout.splitlines()
            if not response_lines:
                continue
            response = json.loads(response_lines[-1])
            if not isinstance(response, dict):
                continue
            backend_digest = response.get("backend_sha256")
            mupdf_digest = response.get("mupdf_sha256")
            if (
                response.get("protocol") == PROTOCOL_VERSION
                and response.get("security_guard") == SECURITY_GUARD_VERSION
                and response.get("capabilities")
                == ["render", "ladder", "merge"]
                and _is_sha256_digest(backend_digest)
                and _is_sha256_digest(mupdf_digest)
            ):
                return resolved
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            continue
    return None


def _reader(stream, messages: queue.Queue[object]) -> None:
    try:
        while True:
            line = stream.readline(MAX_RESPONSE_LINE_BYTES + 1)
            if not line:
                break
            if len(line.encode("utf-8", errors="replace")) > MAX_RESPONSE_LINE_BYTES:
                messages.put(
                    NativeWorkerError(
                        "C++ worker response exceeded the 64 KiB safety limit."
                    )
                )
                return
            messages.put(line)
    finally:
        messages.put(None)


def _write_manifest(
    requests: list[NativeRenderRequest], directory: Path
) -> tuple[Path, dict[int, str]]:
    _validate_requests(requests)
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "native-render-tasks.tsv"
    filenames = {request.id: f"figure-{request.id:04d}.jpg" for request in requests}
    rows = []
    for request in requests:
        x0, y0, x1, y1 = request.rectangle
        rows.append(
            "\t".join(
                (
                    str(request.id),
                    str(request.page_number),
                    format(x0, ".6f"),
                    format(y0, ".6f"),
                    format(x1, ".6f"),
                    format(y1, ".6f"),
                    str(request.dpi),
                    str(request.jpeg_quality),
                    filenames[request.id],
                    str(request.group_id),
                )
            )
        )
    try:
        with manifest.open("x", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(rows) + "\n")
    except FileExistsError as exc:
        raise NativeWorkerError("Native render manifest already exists.") from exc
    return manifest, filenames


class NativeWorkerSession:
    """A persistent C++ process with documents opened once per native thread."""

    def __init__(
        self,
        input_path: str | Path,
        threads: int | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        worker = find_native_worker()
        if worker is None:
            raise NativeWorkerError(
                "C++ worker is not installed or is incompatible."
            )
        self.input_path = Path(input_path).resolve()
        self.workspace = Path(workspace or self.input_path.parent).resolve()
        if not self.workspace.is_dir():
            raise NativeWorkerError("Native worker workspace does not exist.")
        default_threads = min(8, max(2, (os.cpu_count() or 4) // 2))
        self.thread_count = max(1, min(12, threads or default_threads))
        try:
            configured_timeout = int(
                os.environ.get("PDF_SIZE_REDUCER_WORKER_TIMEOUT_SECONDS", "")
                or DEFAULT_BATCH_TIMEOUT_SECONDS
            )
        except ValueError:
            configured_timeout = DEFAULT_BATCH_TIMEOUT_SECONDS
        self.batch_timeout_seconds = max(30, min(1800, configured_timeout))
        command = [
            str(worker),
            "serve",
            "--input",
            str(self.input_path),
            "--threads",
            str(self.thread_count),
            "--workspace",
            str(self.workspace),
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
            env=_worker_environment(worker.parent),
            cwd=worker.parent,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.messages: queue.Queue[object] = queue.Queue()
        self.last_response: dict[str, object] = {}
        self.reader = threading.Thread(
            target=_reader,
            args=(self.process.stdout, self.messages),
            daemon=True,
        )
        self.reader.start()
        hello = self._wait_for_response("hello", timeout=8)
        if hello.get("protocol") != PROTOCOL_VERSION:
            self.close(force=True)
            raise NativeWorkerError("C++ worker protocol mismatch.")

    def _wait_for_response(
        self,
        response_type: str,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, object]:
        deadline = None if timeout is None else time.monotonic() + timeout
        recent_lines: list[str] = []
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self.close(force=True)
                raise NativeWorkerCancelled(
                    "C++ Figure rendering was cancelled."
                )
            if deadline is not None and time.monotonic() >= deadline:
                self.close(force=True)
                raise NativeWorkerError("C++ worker response timed out.")
            try:
                message = self.messages.get(timeout=0.05)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise NativeWorkerError(
                        f"C++ worker exited with code {self.process.returncode}."
                    )
                continue
            if message is None:
                raise NativeWorkerError(
                    "C++ worker closed its output stream unexpectedly."
                )
            if isinstance(message, NativeWorkerError):
                self.close(force=True)
                raise message
            line = str(message).strip()
            if not line:
                continue
            recent_lines.append(line)
            recent_lines = recent_lines[-4:]
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(response, dict):
                self.close(force=True)
                raise NativeWorkerError(
                    "C++ worker returned a non-object protocol response."
                )
            current_type = str(response.get("type", ""))
            if current_type == "progress" and progress_callback:
                try:
                    completed = _strict_int(
                        response.get("completed"), "progress count"
                    )
                    total = _strict_int(
                        response.get("total"), "progress total"
                    )
                except NativeWorkerError:
                    self.close(force=True)
                    raise
                if completed < 0 or total < 1 or completed > total:
                    self.close(force=True)
                    raise NativeWorkerError(
                        "C++ worker returned invalid progress bounds."
                    )
                progress_callback(completed, total)
            if current_type == response_type:
                return response

    def _render_command(
        self,
        requests: list[NativeRenderRequest],
        work_directory: str | Path,
        *,
        command: str,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[int, bytes]:
        if not requests:
            return {}
        directory = Path(work_directory).resolve()
        if not directory.is_relative_to(self.workspace):
            raise NativeWorkerError(
                "Native render directory escaped the private workspace."
            )
        manifest, filenames = _write_manifest(requests, directory)
        for path in (manifest, directory):
            if "\t" in str(path) or "\n" in str(path):
                raise NativeWorkerError(
                    "Native worker paths cannot contain tabs or newlines."
                )
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(
                f"{command}\t{manifest}\t{directory}\n"
            )
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise NativeWorkerError("C++ worker input pipe is closed.") from exc

        response = self._wait_for_response(
            "result",
            timeout=self.batch_timeout_seconds,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        self.last_response = response
        if response.get("ok") is not True:
            self.close(force=True)
            raise NativeWorkerError(
                "C++ worker failed: " + str(response.get("message", "no detail"))
            )
        try:
            completed = _strict_int(
                response.get("completed"), "completion count"
            )
            variants = _strict_int(response.get("variants"), "variant count")
            masters = _strict_int(response.get("master_renders"), "master count")
        except NativeWorkerError:
            self.close(force=True)
            raise
        expected_masters = (
            len({request.group_id for request in requests})
            if command == "LADDER"
            else len(requests)
        )
        if (
            completed != len(requests)
            or variants != len(requests)
            or masters != expected_masters
        ):
            self.close(force=True)
            raise NativeWorkerError(
                "C++ worker completion metadata did not match the request."
            )

        rendered: dict[int, bytes] = {}
        for request in requests:
            output_path = directory / filenames[request.id]
            if output_path.is_symlink() or not output_path.is_file():
                self.close(force=True)
                raise NativeWorkerError(
                    f"C++ worker did not produce Figure {request.id}."
                )
            size = output_path.stat().st_size
            if size < 4 or size > MAX_NATIVE_OUTPUT_BYTES:
                self.close(force=True)
                raise NativeWorkerError(
                    f"C++ worker produced an unsafe Figure size for {request.id}."
                )
            payload = output_path.read_bytes()
            if (
                len(payload) != size
                or not payload.startswith(b"\xff\xd8")
                or not payload.endswith(b"\xff\xd9")
            ):
                self.close(force=True)
                raise NativeWorkerError(
                    f"C++ worker produced an invalid JPEG for Figure {request.id}."
                )
            rendered[request.id] = payload
        return rendered

    def render(
        self,
        requests: list[NativeRenderRequest],
        work_directory: str | Path,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[int, bytes]:
        """Render independent requests at their requested DPI and quality."""
        return self._render_command(
            requests,
            work_directory,
            command="BATCH",
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def render_ladder(
        self,
        requests: list[NativeRenderRequest],
        work_directory: str | Path,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[int, bytes]:
        """Render one master per group and encode every requested variant."""
        return self._render_command(
            requests,
            work_directory,
            command="LADDER",
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    def close(self, *, force: bool = False) -> None:
        if not hasattr(self, "process") or self.process.poll() is not None:
            return
        if not force and self.process.stdin is not None:
            try:
                self.process.stdin.write("QUIT\n")
                self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                force = True
        if force and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(timeout=1)

    def __enter__(self) -> NativeWorkerSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def render_figure_batch(
    input_path: str | Path,
    requests: list[NativeRenderRequest],
    work_directory: str | Path,
    *,
    threads: int | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[int, bytes]:
    """Render one batch with a short-lived native session."""
    if not requests:
        return {}
    work_directory = Path(work_directory).resolve()
    with NativeWorkerSession(
        input_path,
        threads=threads,
        workspace=work_directory,
    ) as session:
        return session.render(
            requests,
            work_directory,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )


def _write_merge_manifest(
    input_paths: list[Path], workspace: Path
) -> Path:
    if not 2 <= len(input_paths) <= MAX_NATIVE_MERGE_SOURCES:
        raise NativeWorkerError("Native merge requires 2 to 100 PDF files.")
    rows = []
    for index, path in enumerate(input_paths):
        resolved = path.resolve()
        if not resolved.is_file() or resolved.suffix.lower() != ".pdf":
            raise NativeWorkerError(f"Invalid native merge input: {path.name}")
        value = str(resolved)
        if "\t" in value or "\n" in value or "\r" in value:
            raise NativeWorkerError(
                "Native merge paths cannot contain tabs or newlines."
            )
        rows.append(f"{index}\t{value}")
    manifest = workspace / "native-merge-inputs.tsv"
    try:
        with manifest.open("x", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(rows) + "\n")
    except FileExistsError as exc:
        raise NativeWorkerError("Native merge manifest already exists.") from exc
    return manifest


def merge_pdf_pages_native(
    input_paths: list[str | Path] | tuple[str | Path, ...],
    output_path: str | Path,
    workspace: str | Path,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> NativeMergeResult:
    """Merge PDF pages through the guarded native MuPDF backend."""
    worker = find_native_worker()
    if worker is None:
        raise NativeWorkerError(
            "Native merge worker is not installed or is incompatible."
        )
    sources = [Path(path).resolve() for path in input_paths]
    work_directory = Path(workspace).resolve()
    destination = Path(output_path).resolve()
    if not work_directory.is_dir():
        raise NativeWorkerError("Native merge workspace does not exist.")
    if destination.parent != work_directory or destination.suffix.lower() != ".pdf":
        raise NativeWorkerError(
            "Native merge output must stay directly inside its private workspace."
        )
    if destination.exists() or destination.is_symlink():
        raise NativeWorkerError("Native merge output already exists.")
    manifest = _write_merge_manifest(sources, work_directory)
    command = [
        str(worker),
        "merge",
        "--manifest",
        str(manifest),
        "--output",
        str(destination),
        "--workspace",
        str(work_directory),
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
        env=_worker_environment(worker.parent),
        cwd=worker.parent,
    )
    assert process.stdout is not None
    messages: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(
        target=_reader,
        args=(process.stdout, messages),
        daemon=True,
    )
    reader.start()
    try:
        configured_timeout = int(
            os.environ.get("PDF_SIZE_REDUCER_WORKER_TIMEOUT_SECONDS", "")
            or DEFAULT_BATCH_TIMEOUT_SECONDS
        )
    except ValueError:
        configured_timeout = DEFAULT_BATCH_TIMEOUT_SECONDS
    deadline = time.monotonic() + max(30, min(1800, configured_timeout))
    recent_lines: list[str] = []
    response: dict[str, object] | None = None
    try:
        while response is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                raise NativeWorkerCancelled("Native PDF merge was cancelled.")
            if time.monotonic() >= deadline:
                process.terminate()
                raise NativeWorkerError("Native PDF merge timed out.")
            try:
                message = messages.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None:
                    raise NativeWorkerError(
                        f"Native PDF merge exited with code {process.returncode}."
                    )
                continue
            if message is None:
                if process.poll() is not None:
                    detail = recent_lines[-1] if recent_lines else "no detail"
                    raise NativeWorkerError(
                        f"Native PDF merge exited with code {process.returncode}: "
                        f"{detail}"
                    )
                continue
            if isinstance(message, NativeWorkerError):
                raise message
            line = str(message).strip()
            if not line:
                continue
            recent_lines.append(line)
            recent_lines = recent_lines[-4:]
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                raise NativeWorkerError(
                    "Native PDF merge returned an invalid protocol response."
                )
            if record.get("type") == "progress":
                completed = _strict_int(record.get("completed"), "merge progress")
                total = _strict_int(record.get("total"), "merge total")
                if completed < 0 or total != len(sources) or completed > total:
                    raise NativeWorkerError(
                        "Native PDF merge returned invalid progress bounds."
                    )
                if progress_callback:
                    progress_callback(completed, total)
            elif record.get("type") == "result":
                response = record
        if response.get("ok") is not True or response.get("operation") != "merge":
            raise NativeWorkerError(
                "Native PDF merge failed: "
                + str(response.get("message", "no detail"))
            )
        source_count = _strict_int(response.get("completed"), "merge source count")
        page_count = _strict_int(response.get("pages"), "merge page count")
        output_bytes = _strict_int(response.get("bytes"), "merge output size")
        elapsed_ms = _strict_int(response.get("elapsed_ms"), "merge elapsed time")
        if source_count != len(sources) or page_count < source_count:
            raise NativeWorkerError(
                "Native PDF merge completion metadata did not match the request."
            )
        process.wait(timeout=5)
        if process.returncode != 0:
            raise NativeWorkerError(
                f"Native PDF merge exited with code {process.returncode}."
            )
        if destination.is_symlink() or not destination.is_file():
            raise NativeWorkerError("Native PDF merge did not produce an output file.")
        actual_size = destination.stat().st_size
        if (
            actual_size != output_bytes
            or actual_size < 5
            or actual_size > MAX_NATIVE_MERGE_OUTPUT_BYTES
        ):
            raise NativeWorkerError("Native PDF merge produced an unsafe output size.")
        with destination.open("rb") as output:
            if output.read(5) != b"%PDF-":
                raise NativeWorkerError("Native PDF merge output is not a PDF.")
        return NativeMergeResult(
            destination,
            source_count,
            page_count,
            output_bytes,
            elapsed_ms / 1000,
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        reader.join(timeout=1)
