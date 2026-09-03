# Current benchmark results

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
