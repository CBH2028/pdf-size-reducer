# Guarded native high-speed worker

`pdf_fast_worker.exe` is a Rust guard whose parsing and validation stay in safe
Rust; `unsafe` is limited to the Windows Job Object FFI. It checks the
build-time SHA-256 digests of the adjacent C++ backend and MuPDF DLL, applies
Windows process limits, and only then starts
`pdf_fast_worker_backend.exe`. The backend renders selected complete Figure
regions without their text layer and encodes them as opaque JPEGs. The Python
compressor retains ownership of PDF redaction, native-text preservation,
exact-size search, atomic output installation, and fallback behavior.

The split is deliberate: rendering and JPEG encoding are the expensive,
independent work and can run concurrently in native threads. PDF structure and
selection semantics remain in the tested Python engine.

Build on Windows after installing `requirements-dev.txt`, Visual Studio 2022
C++ Build Tools, and stable Rust through rustup:

```bat
native_worker\build.bat
```

The build emits the Rust guard as `bin\pdf_fast_worker.exe` and the renderer as
`bin\pdf_fast_worker_backend.exe`. The backend uses the MuPDF headers, import
library, and DLL distributed with the pinned PyMuPDF package. MuPDF is AGPL-3.0
or commercially licensed; binary redistribution must comply with its license.

## Security boundary

The guard has no third-party crate dependencies. It accepts only protocol 2,
confines manifests and output to `--workspace`, enforces bounded task and image
sizes, and runs itself plus the inherited backend in a Windows Job Object. The
default per-process memory ceiling is 1,536 MiB and the active-process ceiling
is two, preventing the backend from creating helper processes. Closing the
guard after an error, timeout, or cancellation terminates the backend as well.

This is resource and protocol containment, not a complete OS sandbox. MuPDF
still parses PDFs in C/C++ with the current user's permissions. See
[`SECURITY.md`](../SECURITY.md) for the full threat model and limitations.

## Protocol

The worker accepts a UTF-8 tab-separated render manifest and writes newline-
delimited JSON progress records to stdout. `render-batch` is useful for a
single invocation:

```text
pdf_fast_worker render-batch --input input.pdf --manifest private-workspace\renders\tasks.tsv --output-dir private-workspace\renders --threads 4 --workspace private-workspace
```

Manifest columns are `id`, zero-based page, `x0`, `y0`, `x1`, `y1`, DPI, JPEG
quality, output filename, and render-group ID. Paths are passed separately and
may contain spaces or non-ASCII characters.

The desktop application uses persistent server mode so the process, native
threads, and one MuPDF document per thread live for the whole exact-size search:

```text
pdf_fast_worker serve --input input.pdf --threads 8 --workspace private-workspace
```

Server commands are written to stdin as
`BATCH<TAB>manifest<TAB>output-directory`; `QUIT` closes the worker cleanly.
Protocol 2 also accepts `LADDER<TAB>manifest<TAB>output-directory`. All rows
with the same group ID must describe one Figure region. The worker rasterizes
that region once at the highest requested DPI, scales it once per lower DPI,
and emits every requested JPEG-quality variant from those shared pixmaps.
