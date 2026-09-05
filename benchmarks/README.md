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

## Workflow comparison (v3.9 and later)

`tools/benchmark_workflow.py` does not require the private Automatica corpus.
It compares two checkouts using one stress PDF, alternating baseline/current
order over three repetitions to reduce cache and ordering bias. Build each
checkout's own native worker first; do not point both versions at a shared
worker override.

```powershell
.\.venv\Scripts\python.exe tools\benchmark_workflow.py `
    --baseline "D:\path\v3.8-checkout" `
    --stress "dist\PDF_Size_Reducer_Stress_Demo_97.92MB.pdf" --repeats 3
```

The cases are asset scanning, guarded merging of the two halves of the input,
merging two generated 400-link documents, and compression of the first six
pages to 70% of their saved input size. Reports go under
`build/benchmarks/v3.9-workflow/` and include per-run times, medians, file hashes,
and environment details. The script checks identical asset records, merged
text and link counts, exact text preservation, target-size compliance, and
matching compressed 72-DPI page renders. These are regression gates, not a
substitute for full-resolution visual inspection or the PSNR/edge suite above.
