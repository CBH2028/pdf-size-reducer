<div align="right">

**English** | [简体中文](README.zh-CN.md)

</div>

<div align="center">

# PDF Size Reducer

**Loss-minimizing PDF compression for everyday file-size limits**

When an assignment portal, application form, expense system, or submission site limits PDF size, there is no need to reopen Word, PowerPoint, or every source image. Choose the PDF, enter the required size, and decide which images may be reduced.

[![Release](https://img.shields.io/github/v/release/CBH2028/pdf-size-reducer?style=flat-square&color=5e5ce6)](https://github.com/CBH2028/pdf-size-reducer/releases/latest)
[![GitHub Stars](https://img.shields.io/github/stars/CBH2028/pdf-size-reducer?style=flat-square&logo=github&label=Stars&color=5e5ce6)](https://github.com/CBH2028/pdf-size-reducer/stargazers)
[![Tests](https://img.shields.io/badge/tests-51%20passed-34C759?style=flat-square)](#development-and-testing)
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
| Merge and compress | Combine up to 100 PDFs through the guarded native engine, then load the result into the accelerated visual compression workspace. |
| Exact size target | Enter MB or KB. The app searches for the clearest result at or below the target instead of padding a file with meaningless bytes. |
| Loss-minimizing workflow | Lossless structural optimization is tried first. Image or Figure data is reduced only when lossless work cannot reach the target. |
| Complete Figure discovery | Captions such as `Fig. X`, `Figure X`, and `图 X` are used to group bitmaps, vector paths, arrows, and labels into one selectable item. |
| Visual selection | Browse full-view thumbnails, type, and estimated storage in the main window. Click any card for a zoomable preview rendered at about 240 DPI. |
| Clarity first | High resolution and moderate JPEG compression are preferred to protect small characters and thin lines. |
| Safe exclusion | If a figure is too important or shows a black-background issue, uncheck it. Its original PDF content and clarity are retained. |
| Responsive interface | Figure scanning and thumbnail rendering run in isolated processes. Large documents do not lock the main window and scanning can be stopped safely. |
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

### Merge PDFs, then compress the result

1. Click **Merge multiple PDFs** (`合并多个 PDF`), or drag multiple PDFs into the window.
2. Add files and arrange their order by dragging, or with the **Move up / Move down** buttons. Remove any files you do not need.
3. Choose where to save the merged PDF. Leave **Load into compression workspace after merging** checked, then click **Start merge**.
4. After the merged document loads, review its Figures, set a target size in MB or KB, and click **Start smart compression**. The compressed result is saved separately from the merged PDF.

Uncheck the workspace option if you only need the merged file. With a matching protocol-3 worker, merging runs through the Rust guard and C++17/MuPDF backend; an unavailable or incompatible worker falls back automatically to Python/PyMuPDF. Both paths run in a separate process and support cancellation. The output is installed only after the completed PDF passes page-count and page-loading checks. Page text, geometry, ordinary page links, annotations, and bookmark destinations are retained. Password-protected inputs must be decrypted first.

Merging is a page-combination operation: document-level attachments, PDF portfolios, digital signatures, and named destinations are not guaranteed to carry over. The first PDF supplies the merged document's basic metadata.

## How it works

![How PDF Size Reducer works](docs/images/workflow-en.svg)

- Lossless structural optimization runs first. If it is enough, image quality is not changed.
- Standalone bitmaps are encoded into compact resolution and JPEG-quality ladders.
- The C++ worker rasterizes each selected complete Figure once at 720 DPI, derives its lower-DPI variants from that master, and leaves recognizable text in an upper PDF text layer.
- A global rate-distortion planner allocates the available bytes across all selected assets, then normally assembles only two complete candidate PDFs.
- If the target would require going below the 180 DPI clarity floor, the app reports the smallest safe result instead of silently producing unreadable content.

## Development and testing

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m py_compile compressor.py qt_app.py
# Requires stable Rust (rustup) and Visual Studio 2022 C++ Build Tools.
native_worker\build.bat
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

Build the single-file Windows executable with:

```powershell
.\build_exe.bat
```

The result is written to `dist\PDF_Size_Reducer.exe`.

## Project structure

```text
pdf-size-reducer/
├── qt_app.py              # Qt 6 desktop UI and background jobs
├── compressor.py          # Figure discovery, rendering, and targeting engine
├── native_worker.py       # Versioned bridge, cancellation, and safe fallback
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
