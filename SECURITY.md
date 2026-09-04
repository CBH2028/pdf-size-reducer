# Security policy and architecture

PDF files are complex, attacker-controlled inputs. PDF Size Reducer v3.6 adds
a Rust guard in front of the high-speed C++/MuPDF renderer. Request parsing,
validation, hashing, and protocol framing stay in safe Rust; `unsafe` is limited
to the small Windows Job Object FFI boundary. This reduces the exposed
native-code surface, but it does not turn MuPDF into memory-safe code or claim
to be a complete operating-system sandbox.

## Native worker boundary

Packaged builds contain two native executables:

- `pdf_fast_worker.exe` is the Rust security guard and the only executable the
  Python application launches.
- `pdf_fast_worker_backend.exe` is the C++17/MuPDF rendering backend. Its
  SHA-256 digest and that of `mupdfcpp64.dll` are compiled into the guard,
  which verifies both native components before every launch.

The Rust guard has no third-party crate dependencies. Its lockfile is committed,
release arithmetic retains overflow checks, panics abort, and Windows Control
Flow Guard is enabled. The C++ backend is built with `/GS`, `/sdl`, `/guard:cf`,
ASLR, high-entropy ASLR, and DEP/NX.

Before MuPDF receives a job, the guard:

- accepts only a strict, versioned command set and verifies all manifest fields;
- confines manifests and rendered files to a caller-provided private workspace;
- rejects path traversal, duplicate IDs or names, existing output files,
  non-finite coordinates, invalid DPI/quality values, and malformed responses;
- limits a PDF to 4 GiB, a manifest to 8 MiB, a batch to 4,096 tasks and two
  gigapixels, and one render request to 100 megapixels;
- places itself and the inherited backend in a Windows Job Object with a
  1,536 MiB per-process memory limit, a two-process ceiling, termination on
  abnormal guard exit, and unhandled-exception termination;
- receives only a minimal subprocess environment; Python applies a bounded
  render timeout and validates output size plus JPEG framing before use.

The same manifest constraints are repeated in Python and C++ as
defense-in-depth. If the guarded worker is absent or incompatible, compression
falls back to the tested Python/MuPDF path.

## Scope and limitations

- MuPDF still parses the document in native C/C++ code. The Job Object limits
  resource use and child processes; it does not restrict filesystem or network
  access under the current user's account.
- The integrity check detects replacement of either the C++ backend executable
  or its MuPDF DLL. It cannot protect against a local administrator—or another
  process with equivalent write access—replacing the guard and its bound
  components together or modifying the running application.
- The optional `PDF_SIZE_REDUCER_NATIVE_WORKER` environment variable is an
  explicit developer override. Pointing it at an untrusted executable defeats
  the packaged-worker trust assumption.
- Current portable builds are not Authenticode-signed. `SHA256SUMS.txt` detects
  a corrupted or mismatched download but is not a substitute for signed
  publisher provenance.
- Resource limits reduce denial-of-service risk but cannot guarantee that every
  malformed PDF is inexpensive to process.

All document processing remains local. The application does not upload PDFs and
contains no telemetry or analytics client.

## Reporting a vulnerability

Please avoid publishing exploit details in a regular issue. Use the repository's
[private security advisory form](https://github.com/CBH2028/pdf-size-reducer/security/advisories/new)
and include the affected version, a minimal reproducer, and the observed impact.
