<div align="right">

**English** | [简体中文](README.zh-CN.md)

</div>

<div align="center">

# PDF Size Reducer

**Visual PDF composition, high-resolution graphics export, and loss-minimizing compression**

When an assignment portal, application form, expense system, or submission site limits PDF size, there is no need to reopen Word, PowerPoint, or every source image. Choose the PDF, enter the required size, and decide which images may be reduced.

[![Release](https://img.shields.io/github/v/release/CBH2028/pdf-size-reducer?style=flat-square&color=5e5ce6)](https://github.com/CBH2028/pdf-size-reducer/releases/latest)
[![GitHub Stars](https://img.shields.io/github/stars/CBH2028/pdf-size-reducer?style=flat-square&logo=github&label=Stars&color=5e5ce6)](https://github.com/CBH2028/pdf-size-reducer/stargazers)
[![Tests](https://img.shields.io/badge/tests-237%20passed-34C759?style=flat-square)](#development-and-testing)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square&logo=windows11&logoColor=white)](https://github.com/CBH2028/pdf-size-reducer/releases/latest)
[![License](https://img.shields.io/github/license/CBH2028/pdf-size-reducer?style=flat-square)](LICENSE)

[Download for Windows](https://github.com/CBH2028/pdf-size-reducer/releases/latest) · [Changelog](CHANGELOG.md) · [Report an issue](https://github.com/CBH2028/pdf-size-reducer/issues)

</div>

![PDF Size Reducer loading a large document](docs/images/app-loading.png)

## Operation demo

<div align="center">

[![PDF Size Reducer operation demo](docs/media/operation-demo.gif)](https://github.com/CBH2028/pdf-size-reducer/releases/download/v3.3.0/PDF_Size_Reducer_Operation_Demo.mp4)

**A real 97.92 MiB / 48-page PDF: background scan → full-Figure preview → zoom → selective exclusion → exact target → completed output**

[Download HD MP4](https://github.com/CBH2028/pdf-size-reducer/releases/download/v3.3.0/PDF_Size_Reducer_Operation_Demo.mp4) · [Download demo PDF](https://github.com/CBH2028/pdf-size-reducer/releases/download/v3.2.0/PDF_Size_Reducer_Stress_Demo_97.92MB.pdf) · [Demo and stress-test notes](docs/DEMO.md)

</div>

## Why this tool exists

The PDF is often already finished when a website says it is too large. The usual choices are repetitive manual export work or whole-page rasterization that also blurs body text, equations, links, and captions.

PDF Size Reducer automates the careful route. It discovers complete figures, shows their previews and estimated storage, lets you choose only the images that may change, and searches for the clearest result that stays within the requested limit.

> **It does not rasterize every page.** Body text, equations, links, annotations, and unselected figures remain untouched. Text that can be recognized inside a selected figure is kept as a sharp, searchable, copyable PDF text layer.

Typical situations include:

- uploading homework, forms, portfolios, receipts, or certificates to a size-limited portal;
- submitting a report or paper without manually rebuilding its figures;
- reducing only the largest images while preserving important screenshots or diagrams;
- getting close to an exact MB or KB limit with the least practical visual loss.

## Important: possible black backgrounds in PowerPoint vector figures

Vector artwork exported from PowerPoint may contain complex transparency, masks, gradients, or grouped drawing objects. When the app reduces such a Figure, a small number of PDFs may render its background as solid black. This depends on the PDF drawing structure and viewer compatibility, so it cannot currently be ruled out for every PowerPoint-generated figure.

If this happens, **uncheck that Figure in the preview list and run the task again**. An unchecked Figure is preserved exactly as it appears in the source PDF; the app will reduce only the remaining selected images. Always give the output PDF a quick visual check before submitting it.

## Highlights

| Feature | What it does |
| --- | --- |
| Simple drag-and-drop composition | Result pages on the left, material pages on the right. Drag to insert or reorder, then save. No tree, page-range fields or insertion buttons in the default view. |
| Advanced composition when needed | One toggle reveals the three-pane tree, nested groups, page ranges and extra controls. Both modes share the same plan and undo history; group names become bookmarks. |
| Whole-document merge in Advanced | Open the optional file queue from Advanced composition to merge entire PDFs, reorder multiple files, sort naturally, undo and check totals. |
| Compose and compress | Assemble selected original pages without rasterizing them, then optionally continue to the accelerated compression workspace. Compatible whole-document plans retain the guarded native merge path. |
| Export selected graphics | Check exactly the Figure/image cards you need, then choose any combination of SVG, 600-DPI PNG and one-graphic-per-page PDF. Unchecked graphics are not exported. |
| Exact size target | Enter MB or KB. The app searches for the clearest result at or below the target instead of padding a file with meaningless bytes. |
| Loss-minimizing workflow | Lossless structural optimization is tried first. Image or Figure data is reduced only when lossless work cannot reach the target. |
| Complete Figure discovery | Captions such as `Fig. X`, `Figure X`, and `图 X` are used to group bitmaps, vector paths, arrows, and labels into one selectable item. |
| Visual selection | Browse full-view thumbnails, type, and estimated storage in the main window. Click any card for a zoomable preview rendered at about 240 DPI. |
| Clarity first | High resolution and moderate JPEG compression are preferred to protect small characters and thin lines. |
| Safe exclusion | If a figure is too important or shows a black-background issue, uncheck it. Its original PDF content and clarity are retained. |
| Responsive interface | Scanning, thumbnails, high-resolution previews, merging, and compression run in separate processes. Closing a preview cancels its render; stuck desktop writers can be stopped after a cancellation grace period. |
| Immediate animated startup | The Windows bootloader shows activity during one-file extraction, then hands off to a pulsing, rotating Qt startup animation until the main workspace is interactive. |
| Native high-speed planner | A C++17/MuPDF worker builds each Figure's complete quality ladder from one master rasterization; a global byte-budget planner then selects the clearest combination. |
| Hardened native boundary | A Rust guard performs memory-safe request parsing, verifies the native backend and DLL, confines jobs to a private workspace, and applies Windows process and memory limits. |
| Commercial-grade desktop UI | A glass-like header, structured workflow cards, animated status, a gradient primary action, progressive thumbnails, hover feedback, smooth scrolling, and high-DPI support. |

## Quick start

### Option 1: portable Windows build

Open [Releases](https://github.com/CBH2028/pdf-size-reducer/releases/latest), download `PDF_Size_Reducer.exe`, and run it. Python is not required.
v3.6 and later releases also provide `SHA256SUMS.txt` for download-integrity checks.

### Option 2: run from source

```powershell
git clone https://github.com/CBH2028/pdf-size-reducer.git
cd pdf-size-reducer
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe qt_app.py
```

On Windows, `start_pdf_tool.bat` can also create the isolated environment, install dependencies, and start the app.

## How to use it

1. Click **Choose File**, or drag a PDF into the window.
2. Let the app discover figures page by page. The window remains responsive while it works.
3. Review each full-view thumbnail and estimated storage; click a thumbnail to zoom in.
4. Keep the images or Figures that may be reduced checked. Uncheck anything that must remain unchanged.
5. Enter the required target size, confirm the output location, and start compression.
6. Review the generated PDF. If a PowerPoint vector figure has a black background, uncheck it and run the task again.

The source PDF is never overwritten. If the selected output path already exists, the app asks before replacing it.

If another application saves or replaces the source after it was scanned, reload
the PDF before compressing it. Source-state checks prevent ordinary file edits
from silently applying old Figure selections to new content. Desktop tasks
prepare output in temporary files; the application installs the final PDF only
after successful completion and a final cancellation check.

### Export selected graphics

1. Load a PDF, wait for Figure discovery to finish, and check exactly the Figure/image cards you want. The export action is disabled when no card is selected.
2. Click **Export selected high-resolution graphics…** (`导出已选高清图…`). Tick SVG, PNG, PDF, or any combination of the three, then choose a parent folder. At least one format is required.
3. The app creates a new `<PDF name>_高清图` directory and never overwrites an existing export directory. Only the selected payload formats are written.
4. Open the generated `manifest.json` when you need provenance. It lists requested formats, source pages, dimensions, output filenames and whether each item retains native vector content or embeds an original bitmap.

Complete Figures are exported without flattening their PDF paths in SVG/PDF; SVG text is emitted as outlines and PDF text remains native. Figure PNGs render at up to 600 DPI. If a Figure mixes vector drawing with photographs, the vector portions remain vector in SVG/PDF and the original raster portions remain embedded. A standalone bitmap cannot be made into a truthful vector without tracing and altering it, so every selected container preserves its exact source pixels. The number of exported items always equals the number of checked cards; unchecked graphics remain out of the export directory.

### Compose pages by dragging

v3.13.1 fixes drops that did nothing in v3.13.0. Launch the updated executable; an already-open window continues running the old version.

1. Click **Compose PDF** (`组合 PDF · 拖动页面即可`). The currently loaded PDF fills the left result pane. In an empty composer, drop the main PDF onto the left; additional dropped PDFs become materials.
2. Drop more PDFs onto the **right**, or click **Add PDF** (`添加 PDF`). Pick a material document from the dropdown and drag its pages to a gap on the **left**. The colored marker shows the insertion position. Adding material does not concatenate whole documents.
3. Drag result pages to reorder them. Ctrl/Shift selects several pages; the hover **×** or Delete omits pages from the result only. Ctrl+Z undoes a change; Ctrl+Shift+Z/Ctrl+Y redoes it. Double-click a page to zoom in.
4. Click **Save PDF** (`保存 PDF`) and choose a new filename. Cancelling keeps the workspace intact. By default the saved PDF opens in the compression workspace; choose a target there if needed.

![Simple drag-and-drop PDF composition](docs/images/pdf-composer-simple.png)

**Advanced composition** (`高级组合`) contains the tree, nested groups/bookmarks, typed page ranges, insertion buttons, source rechecking, save-only choice and whole-document merge. Switching modes preserves the exact plan, groups and undo history. [Advanced workspace screenshot](docs/images/pdf-composer.png).

The material library has no fixed file-count cap. Simple-mode thumbnails scroll continuously and load on demand; advanced browsers retain 24-page batches. Resource limits remain: 20,000 output pages, 12 tree levels, 4 GiB per PDF and 16 GiB across used sources. See the [composition guide](docs/COMPOSING.md) for controls and preservation limits.

### Whole-document merge (Advanced)

1. Open **Compose PDF → Advanced composition → Whole-document merge** (`组合 PDF → 高级组合 → 整份合并…`). The material library preloads the queue. Cancelling returns to the unchanged composition plan; completing this queue saves the queued documents instead of the page plan.
2. Drop more PDFs directly into the merge list. Check each file's page count, size and folder, along with the totals. Double-click an entry to inspect it in your default PDF reader.
3. Drag to reorder, or use **Ctrl / Shift** to select several entries and move them together with **Move up / Move down** or **Alt+Up / Alt+Down**. **Sort by filename** puts `1, 2, 10` in natural order. Remove/clear only affects the list; **Undo list action** restores your previous queue.
4. Edit the suggested output name or choose a different folder. The default name avoids existing files. If you decline an overwrite, the queue stays open so you can choose another name.
5. Choose **Continue to compression** (`继续压缩：先预览，再设置目标大小`), then click **Merge and continue**. After the merged document loads, review its Figures, set an MB/KB target, and start compression. The compressed result is saved separately from the merged PDF.

Choose **Merge only** (`只合并，保存 PDF`) if you do not need compression. Duplicates and invalid paths are reported. Background checks flag unreadable/password-protected inputs; use **Recheck files** if a source changes. This optional queue is limited to 100 PDFs, 4 GiB per file and 16 GiB in total; check the queue when a larger material library is preloaded.

![Merge queue with natural sorting and page totals](docs/images/pdf-merge.png)

With a matching protocol-3 worker, merging runs through the Rust guard and C++17/MuPDF backend; an unavailable or incompatible worker falls back automatically to Python/PyMuPDF. Both paths run in a separate process and support cancellation. The output is installed only after the completed PDF passes page-count and page-loading checks. Page text, geometry, ordinary page links, annotations, and bookmark destinations are retained. Password-protected inputs must be decrypted first.

Inputs with editable AcroForm fields automatically use the form-aware compatibility merger to preserve field values and editability. Native merges also retain ordinary links on rotated pages, including overlapping links with different targets.

Merging is a page-combination operation: document-level attachments, PDF portfolios, digital signatures, and named destinations are not guaranteed to carry over. The first PDF supplies the merged document's basic metadata.

## How it works

![How PDF Size Reducer works](docs/images/workflow-en.svg)

- Lossless structural optimization runs first. If it is enough, image quality is not changed.
- Standalone bitmaps are encoded into compact resolution and JPEG-quality ladders.
- The C++ worker rasterizes each selected complete Figure once at 720 DPI, derives its lower-DPI variants from that master, and leaves recognizable text in an upper PDF text layer.
- A global rate-distortion planner allocates the available bytes across all selected assets, then normally assembles only two complete candidate PDFs.
- If the target would require going below the 180 DPI clarity floor, the app reports the smallest safe result instead of silently producing unreadable content.

Text editing, automatic text-region editing and text insertion remain removed. Page selection, insertion and omission now belong to the new-document composition workflow; they do not edit the original PDFs. Figure discovery and selection remain available for compression.

## Development and testing

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m py_compile compressor.py graphics_export.py qt_app.py merge_ui.py pdf_composer.py composer_ui.py qt_dispatch.py tools\generate_startup_splash.py
# Requires stable Rust (rustup) and Visual Studio 2022 C++ Build Tools.
native_worker\build.bat
.\.venv\Scripts\python.exe qt_app.py --graphics-export-self-test
.\.venv\Scripts\python.exe qt_app.py --startup-self-test
```

Run the reproducible large-file UI stress test:

```powershell
.\.venv\Scripts\python.exe .\tools\stress_test.py `
    --pages 48 --image-width 1600 --image-height 1000 `
    --vector-paths 90 --timeout 300
```

The command creates a temporary synthetic research-style PDF of about 98 MiB, monitors the Qt event loop, scanning, thumbnails, and memory, and removes the file afterward. Use `--pdf "D:\path\large.pdf"` to test an existing file or `--cancel-after-ms 500` to verify safe cancellation.

Run the full fixed-corpus performance and quality benchmark with
[`tools/benchmark_suite.py`](tools/benchmark_suite.py). It verifies fixture
hashes before comparing results and records exact-size accuracy, render-cache
hits, memory, PSNR, edge similarity, native-text preservation, and black-background
regressions. See the [benchmark guide](benchmarks/README.md) and
[latest measured results](benchmarks/RESULTS.md).

For controlled before/after checks, `tools/benchmark_workflow.py` alternates two
checkouts and verifies matching asset lists, merged text/links, and compressed
page renders. Both checkouts need their corresponding built native worker:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_workflow.py `
    --baseline "D:\path\v3.8-checkout" --stress "D:\path\stress.pdf" --repeats 3
```

Build the single-file Windows executable with:

```powershell
.\build_exe.bat
```

The result is written to `dist\PDF_Size_Reducer.exe`.

Use `PDF_Size_Reducer.exe --workflow-self-test` for a headless packaged smoke
test of guarded merging, lossless compression, preview, native Figure planning,
and cancellation before final output installation (exit code 0 means success).
This complements `--native-worker-self-test`, which only checks worker discovery.
Use `--merge-ui-self-test` to check the actual merge queue, background page inspection,
multi-selection ordering, undo, and preflight-checked guarded output in a packaged build.
Use `--composer-self-test` to check the advanced three-column workspace and nested
tree insertion, or `--simple-composer-self-test` for the default two-pane drag/drop
workspace. The simple check sends drag events through Qt's viewport dispatch and
checks insertion and reordering. Both cover real thumbnails, zoom preview,
undo/redo, selected-page export and bookmark order.

## Project structure

```text
pdf-size-reducer/
├── qt_app.py              # Qt 6 desktop UI and background jobs
├── merge_ui.py            # Merge queue, metadata preflight, ordering and save choices
├── composer_ui.py         # Shared composition jobs, validation and advanced tree UI
├── simple_composer_ui.py  # Default two-pane drag-and-drop page workspace
├── pdf_composer.py        # Composition tree and non-rasterizing selected-page writer
├── qt_dispatch.py         # Explicit GUI-thread dispatch for asynchronous callbacks
├── compressor.py          # Figure discovery, rendering, and targeting engine
├── native_worker.py       # Versioned bridge, cancellation, and safe fallback
├── process_jobs.py        # Lifetime management for desktop process trees
├── native_worker/         # Rust guard plus C++17/MuPDF high-speed backend
├── SECURITY.md            # Native-worker threat model and reporting policy
├── tests/                 # Compression and content-preservation regressions
├── benchmarks/            # Fixed baseline, benchmark guide, and checked results
├── tools/benchmark_suite.py # Performance, targeting, and quality benchmark
├── tools/stress_test.py   # Large-PDF generator and UI responsiveness test
├── tools/record_demo.py   # Reproducible, application-only demo recorder
├── docs/media/            # README animation and HD demo video
├── start_pdf_tool.bat     # One-click source launcher
├── build_exe.bat          # PyInstaller build script
└── CHANGELOG.md           # Full change history
```

MuPDF, Pillow, and Qt already perform low-level work in native code, while Python retains PDF selection, safety, and fallback control. Protocol 3 of the C++17 worker supports guarded page merging and builds an entire Figure quality ladder from one high-resolution master; a global rate-distortion planner distributes the byte budget and avoids repeatedly rewriting the whole PDF. On the fixed `Automatica.pdf → 3.12 MiB` benchmark this reduced compression from 169.964 seconds in v3.3.1 and 33.074 seconds in v3.4.0 to **9.501 seconds**—**17.889× faster** than v3.3.1 and **3.481× faster** than v3.4.0. The output was 809 bytes below target, preserved native text exactly, scored 39.854 dB PSNR, and passed the black-background gate. See [the measured benchmark](benchmarks/RESULTS.md).

The native worker is optional. Source runs without a built worker use the tested Python/MuPDF fallback; packaged Windows builds include it automatically. Python launches a zero-third-party-dependency Rust guard, which verifies and contains the C++ backend before it renders or merges a document. Native merging strengthens the execution boundary but is not advertised as faster than PyMuPDF's already-native `insert_pdf` path; the major measured speedup remains the compression planner used after merging. See the honest scope and limitations in the [security policy](SECURITY.md). `build_exe.bat` also produces a standalone folder in `dist\PDF_Fast_Worker` and a shareable `dist\PDF_Fast_Worker_Windows_x64.zip` bundle.

## Privacy

All PDFs are processed locally. The app does not upload documents and contains no telemetry, account system, or network analytics.

## Star history

<div align="center">

[![GitHub Star History](docs/images/star-history.svg)](https://github.com/CBH2028/pdf-size-reducer/stargazers)

</div>

This repository-hosted chart is generated from GitHub's official Stargazers API, avoiding third-party chart outages. Click it for the live stargazer list; maintainers can refresh it with `python tools/update_star_history.py`.

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before getting started. Important behavior changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

Released under the [MIT License](LICENSE).
