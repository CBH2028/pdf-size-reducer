# Performance benchmark

The benchmark uses a fixed real paper, its PowerPoint-produced reference PDF,
and the 97.92 MiB synthetic stress document. File hashes in
`baseline-v3.3.1.json` prevent accidental comparisons against a different
corpus.

Run the complete suite on Windows:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_suite.py `
  --source "C:\path\to\Automatica.pdf" `
  --reference "C:\path\to\Automatica compressed.pdf" `
  --stress "dist\PDF_Size_Reducer_Stress_Demo_97.92MB.pdf" `
  --target-mib 3.12 `
  --label current
```

The suite records scan time, thumbnail completion, UI responsiveness, exact
target accuracy, compression attempts, planner activation, master
rasterizations, encoded variants, page and text preservation, PSNR, edge
similarity, black-background regression, and native C++ render time/task
counts. Reports are written to `build/benchmarks/` by default.

Before reporting a speedup, the suite verifies the source, PowerPoint
reference, and stress-file hashes against the saved baseline. This prevents a
different PDF from being presented as a valid before/after comparison.

See [the latest checked result](RESULTS.md).
