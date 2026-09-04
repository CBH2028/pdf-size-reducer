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
