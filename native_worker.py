"""Python bridge for the optional C++ Figure-rendering worker."""

from __future__ import annotations

import json
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


PROTOCOL_VERSION = 2
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


def _worker_environment() -> dict[str, str]:
    """Expose the bundled PyMuPDF DLL directory to the child process."""
    environment = os.environ.copy()
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        dll_directory = str(Path(bundle_root) / "pymupdf")
        environment["PATH"] = dll_directory + os.pathsep + environment.get(
            "PATH", ""
        )
    return environment


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
                env=_worker_environment(),
            )
            if completed.returncode:
                continue
            response = json.loads(completed.stdout.splitlines()[-1])
            if response.get("protocol") == PROTOCOL_VERSION:
                return resolved
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            continue
    return None


def _reader(stream, messages: queue.Queue[object]) -> None:
    try:
        for line in stream:
            messages.put(line)
    finally:
        messages.put(None)


def _write_manifest(
    requests: list[NativeRenderRequest], directory: Path
) -> tuple[Path, dict[int, str]]:
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
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return manifest, filenames


class NativeWorkerSession:
    """A persistent C++ process with documents opened once per native thread."""

    def __init__(self, input_path: str | Path, threads: int | None = None) -> None:
        worker = find_native_worker()
        if worker is None:
            raise NativeWorkerError(
                "C++ worker is not installed or is incompatible."
            )
        self.input_path = Path(input_path).resolve()
        default_threads = min(8, max(2, (os.cpu_count() or 4) // 2))
        self.thread_count = max(1, min(12, threads or default_threads))
        command = [
            str(worker),
            "serve",
            "--input",
            str(self.input_path),
            "--threads",
            str(self.thread_count),
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
            env=_worker_environment(),
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
            line = str(message).strip()
            if not line:
                continue
            recent_lines.append(line)
            recent_lines = recent_lines[-4:]
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            current_type = str(response.get("type", ""))
            if current_type == "progress" and progress_callback:
                progress_callback(
                    int(response.get("completed", 0)),
                    int(response.get("total", 0)),
                )
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
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        self.last_response = response
        if not response.get("ok"):
            raise NativeWorkerError(
                "C++ worker failed: " + str(response.get("message", "no detail"))
            )

        rendered: dict[int, bytes] = {}
        for request in requests:
            output_path = directory / filenames[request.id]
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise NativeWorkerError(
                    f"C++ worker did not produce Figure {request.id}."
                )
            rendered[request.id] = output_path.read_bytes()
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
    with NativeWorkerSession(input_path, threads=threads) as session:
        return session.render(
            requests,
            work_directory,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
