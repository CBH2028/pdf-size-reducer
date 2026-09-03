# Current benchmark results

Measured on 2026-09-02 with Python 3.12.10, PyMuPDF 1.28.2, Pillow
12.3.0, and a 20-logical-CPU Windows 11 machine. The fixed-corpus SHA-256
values matched [`baseline-v3.3.1.json`](baseline-v3.3.1.json).

| Metric | Original | C++ worker | Result |
|---|---:|---:|---:|
| Exact 3.12 MiB compression | 169.964 s | 33.074 s | **5.139× faster** |
| Source scan + 20 thumbnails | 8.782 s | 7.221 s | **1.216× faster** |
| 97.92 MiB scan + 48 thumbnails | 8.856 s | 7.547 s | **1.173× faster** |
| 97.92 MiB maximum UI pause | 41.16 ms | 32.47 ms | **21.1% lower** |
| 97.92 MiB main-process peak memory | 113.11 MiB | 113.16 MiB | stable |

For context, the Python caching/direct-pixel stage reached 89.800 seconds;
the native worker reduces that further by 2.715×. The current exact-size run
produced 3,271,401 bytes for a 3,271,557-byte target, a gap of only **156
bytes**. It evaluated 27 candidate PDFs, completed 209 native render tasks in
11 persistent batches, and spent 20.438 seconds inside the C++ render path.
The Python main-process peak working set was 196.98 MiB; the separately
isolated native worker is not included in that process-local measurement.

Quality gates all passed:

- all 15 pages were preserved;
- native PDF text was exactly preserved;
- PSNR against the source was 39.862 dB and edge similarity was 0.963437;
- the black-background detector found no regression;
- the current output scored materially closer to the source than the supplied
  PowerPoint-produced reference (21.047 dB PSNR, 0.853132 edge similarity).

Safe cancellation was also checked on the 97.92 MiB fixture. A cancellation
requested after 250 ms completed cleanly in 0.335 seconds, with no assets or
output left behind and a 30.11 ms maximum event-loop pause.

Times naturally vary with CPU load and storage caching. Run
[`tools/benchmark_suite.py`](../tools/benchmark_suite.py) against the same
fixture hashes for a comparable result.
