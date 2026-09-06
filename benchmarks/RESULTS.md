# Current benchmark results

## v3.9.1 task-lifecycle verification

Measured on 2026-09-06 using the existing 97.92 MiB / 48-page stress PDF.
The desktop loaded all 48 assets and their thumbnails in 5.905 seconds; the
maximum event-loop gap was 34.02 ms and main-process peak working set was
114.59 MiB. A separate run requesting cancellation after 500 ms finished
cleanly after 0.678 seconds. These UI measurements include process startup and
interface transitions and are not directly comparable to the engine-only scan
times below.

All 80 Python tests passed, including cancellation immediately before final
installation, cleanup after a partially written/cancelled/crashed task, actual
Windows child/grandchild termination, native planner preservation, source-file
changes, preview closure/reuse, and scanner/thumbnail startup failures.

## v3.9 workflow regression and scan optimization

Measured on 2026-09-05 with Python 3.12.10, PyMuPDF 1.28.2, and the same
20-logical-CPU Windows 11 machine. Baseline: v3.8.0 commit `30a8953` with its
own native binaries; current: v3.9.0 with the rebuilt guarded worker. Three
alternating baseline/current repetitions were performed for each case.

| Case | v3.8 median | v3.9 median | Interpretation |
|---|---:|---:|---|
| 97.92 MiB / 48-page asset scan | 5.471 s | **3.421 s** | **1.60× faster; about 37.5% less time** |
| Guarded merge of two 24-page halves | 1.494 s | 1.496 s | Essentially unchanged |
| Merge two 400-link documents | 0.575 s | 0.581 s | Essentially unchanged |
| Six-page compression to 70% of input size | 39.562 s | 39.542 s | Essentially unchanged |

The stress input SHA-256 is
`c0481f39fe607c42e66e30e36266fe8440cbbe518fce38a48670ce9b1e851214`.
All asset records matched; merged page text and all 800 test links were retained.
Every compressed output was 7,765,099 bytes against a 7,767,964-byte target,
preserved native text, and had identical 72-DPI page-render hashes across both
versions. This compression case completed through the ordinary bitmap path
without using the native Figure planner, so it is not a new native-planner
speed measurement. The historical Automatica corpus was not available for
this run; the 17.889× result below is historical, not a fresh v3.9 claim.

See [per-run data](workflow-v3.9.0.json) and
[`tools/benchmark_workflow.py`](../tools/benchmark_workflow.py). These are
fixture-specific, warm-session measurements, not guarantees for every PDF.
The 72-DPI comparison is a regression check, not a full-resolution quality
assessment. Additional tests cover editable forms, rotated/overlapping links,
invalid small inputs, lossless early exit, subprocess failure cleanup, and
cooperative cancellation. All 63 Python tests and 5 Rust tests passed.

## Historical v3.3.1–v3.5 compression planner benchmark

Measured on 2026-09-03 with Python 3.12.10, PyMuPDF 1.28.2, Pillow
12.3.0, and a 20-logical-CPU Windows 11 machine. The fixed-corpus SHA-256
values matched [`baseline-v3.3.1.json`](baseline-v3.3.1.json).

| Metric | v3.3.1 | v3.4.0 | v3.5.0 planner |
|---|---:|---:|---:|
| Exact 3.12 MiB compression | 169.964 s | 33.074 s | **9.501 s** |
| Speedup vs v3.3.1 | 1.000× | 5.139× | **17.889×** |
| Speedup vs v3.4.0 | — | 1.000× | **3.481×** |
| Source scan + 20 thumbnails | 8.782 s | 7.221 s | 7.215 s |
| 97.92 MiB scan + 48 thumbnails | 8.856 s | 7.547 s | 7.538 s |
| 97.92 MiB maximum UI pause | 41.16 ms | 32.47 ms | 29.33 ms |
| 97.92 MiB main-process peak memory | 113.11 MiB | 113.16 MiB | 113.00 MiB |

The planner produced 3,270,748 bytes for a 3,271,557-byte target, a gap of
only **809 bytes**. It assembled two complete candidate PDFs. One native batch
performed 19 master rasterizations and emitted 266 encoded variants in 5.663
seconds; lower-DPI and same-DPI JPEG choices reused those masters rather than
rasterizing the PDF again. The Python main-process peak working set was 196.82
MiB; the separately isolated native worker is not included in that
process-local measurement.

Quality gates all passed:

- all 15 pages were preserved;
- native PDF text was exactly preserved;
- PSNR against the source was 39.854 dB and edge similarity was 0.962284;
- the black-background detector found no regression.

The 26-test regression suite also covers safe cancellation, planner budget
limits, quality-floor fairness, master-raster reuse, DPI dimensions, and
automatic fallback to the previous search engine.

Times naturally vary with CPU load and storage caching. Run
[`tools/benchmark_suite.py`](../tools/benchmark_suite.py) against the same
fixture hashes for a comparable result.

## v3.6 security-guard regression

Measured on 2026-09-04 using the same machine and source hash. For a controlled
A/B comparison, commit `277faa1` supplied the v3.5 Python path while both sides
used the same freshly hardened C++ backend; the v3.6 side additionally used the
Rust integrity, protocol, workspace, and Job Object guard.

| Metric | v3.5 path, no Rust guard | v3.6 guarded path | Change |
|---|---:|---:|---:|
| Exact 3.12 MiB compression | 16.790 s | 16.879 s | +0.53% |
| Native ladder stage | 5.646 s | 6.010 s | +6.45% |
| Scan + thumbnails | 6.929 s | 6.983 s | +0.78% |
| Main-process peak memory | 199.32 MiB | 199.03 MiB | -0.29 MiB |
| Output size | 3,270,748 B | 3,270,748 B | identical |

The guarded output remained 809 bytes below target, with identical 39.854 dB
PSNR, 0.962284 edge similarity, exact native-text preservation, and no detected
black-background regression. The 0.089-second end-to-end difference is small
enough to be indistinguishable from ordinary run-to-run system noise; the
security layer does not create a perceptible overall slowdown. The historical
9.501-second v3.5 release run above was recorded under a faster scan/cache state,
so the controlled same-session A/B is the appropriate security-overhead check.
