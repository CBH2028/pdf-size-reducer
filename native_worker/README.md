# Native high-speed worker

`pdf_fast_worker.exe` is a small C++17 process that renders selected complete
Figure regions without their text layer and encodes them as opaque JPEGs. The
Python compressor retains ownership of PDF redaction, native-text preservation,
exact-size search, atomic output installation, and fallback behavior.

The split is deliberate: rendering and JPEG encoding are the expensive,
independent work and can run concurrently in native threads. PDF structure and
selection semantics remain in the tested Python engine.

Build on Windows after installing `requirements-dev.txt`:

```bat
native_worker\build.bat
```

The executable uses the MuPDF headers, import library, and DLL distributed with
the pinned PyMuPDF package. MuPDF is AGPL-3.0 or commercially licensed; binary
redistribution must comply with its license.

## Protocol

The worker accepts a UTF-8 tab-separated render manifest and writes newline-
delimited JSON progress records to stdout. `render-batch` is useful for a
single invocation:

```text
pdf_fast_worker render-batch --input input.pdf --manifest tasks.tsv --output-dir renders --threads 4
```

Manifest columns are `id`, zero-based page, `x0`, `y0`, `x1`, `y1`, DPI, JPEG
quality, output filename, and render-group ID. Paths are passed separately and
may contain spaces or non-ASCII characters.

The desktop application uses persistent server mode so the process, native
threads, and one MuPDF document per thread live for the whole exact-size search:

```text
pdf_fast_worker serve --input input.pdf --threads 8
```

Server commands are written to stdin as
`BATCH<TAB>manifest<TAB>output-directory`; `QUIT` closes the worker cleanly.
Protocol 2 also accepts `LADDER<TAB>manifest<TAB>output-directory`. All rows
with the same group ID must describe one Figure region. The worker rasterizes
that region once at the highest requested DPI, scales it once per lower DPI,
and emits every requested JPEG-quality variant from those shared pixmaps.
