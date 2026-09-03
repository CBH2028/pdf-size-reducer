"""Reproducible performance, targeting, and visual-quality benchmark suite."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf as fitz
from PIL import Image, ImageChops, ImageFilter

from compressor import PDFAsset, compress_pdf, list_pdf_assets


def current_working_set_mib() -> float | None:
    """Return this process's current resident memory on Windows."""
    if os.name != "nt":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    get_memory_info.restype = ctypes.c_int
    if not get_memory_info(
        get_current_process(), ctypes.byref(counters), counters.cb
    ):
        return None
    return counters.WorkingSetSize / 1024**2


class MemorySampler:
    """Sample resident memory during one benchmark operation."""

    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self.peak_mib: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.is_set():
            value = current_working_set_mib()
            if value is not None:
                self.peak_mib = max(self.peak_mib or value, value)
            self._stop.wait(self.interval_seconds)

    def __enter__(self) -> MemorySampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        value = current_working_set_mib()
        if value is not None:
            self.peak_mib = max(self.peak_mib or value, value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def describe_pdf(path: Path) -> dict[str, Any]:
    with fitz.open(path) as document:
        pages = document.page_count
    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "size_mib": round(path.stat().st_size / 1024**2, 3),
        "pages": pages,
        "sha256": sha256_file(path),
    }


def selected_assets(
    assets: list[PDFAsset],
) -> tuple[
    set[int],
    set[int],
    dict[int, list[tuple[float, float, float, float]]],
]:
    images = {
        asset.xref
        for asset in assets
        if asset.kind == "image" and asset.xref is not None
    }
    vectors = {
        page
        for asset in assets
        if asset.kind == "vector"
        for page in asset.page_numbers
    }
    figures: dict[int, list[tuple[float, float, float, float]]] = {}
    for asset in assets:
        if asset.kind == "figure" and asset.rect is not None:
            figures.setdefault(asset.page_numbers[0], []).append(asset.rect)
    return images, vectors, figures


def run_ui_measurement(pdf_path: Path, timeout: int) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "stress_test.py"),
        "--pdf",
        str(pdf_path),
        "--timeout",
        str(timeout),
    ]
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 30,
        check=False,
    )
    start = completed.stdout.find("{")
    if completed.returncode or start < 0:
        raise RuntimeError(
            "UI benchmark failed:\n"
            + (completed.stderr or completed.stdout or "No output")
        )
    return json.loads(completed.stdout[start:])


def run_compression_measurement(
    source: Path,
    destination: Path,
    target_bytes: int,
) -> tuple[dict[str, Any], list[PDFAsset]]:
    scan_started = time.perf_counter()
    assets, page_count = list_pdf_assets(source)
    scan_seconds = time.perf_counter() - scan_started
    images, vectors, figures = selected_assets(assets)

    last_bucket = -1

    def progress(value: int, message: str) -> None:
        nonlocal last_bucket
        bucket = value // 10
        if bucket > last_bucket:
            last_bucket = bucket
            print(f"  {value:3d}%  {message}", flush=True)

    compression_started = time.perf_counter()
    with MemorySampler() as memory:
        result = compress_pdf(
            source,
            destination,
            target_bytes,
            progress_callback=progress,
            selected_image_xrefs=images,
            selected_vector_pages=vectors,
            selected_figure_regions=figures,
        )
    compression_seconds = time.perf_counter() - compression_started
    return (
        {
            "page_count": page_count,
            "asset_count": len(assets),
            "figure_count": sum(a.kind == "figure" for a in assets),
            "image_count": sum(a.kind == "image" for a in assets),
            "scan_seconds": round(scan_seconds, 3),
            "compression_seconds": round(compression_seconds, 3),
            "peak_working_set_mib": (
                None if memory.peak_mib is None else round(memory.peak_mib, 2)
            ),
            "target_bytes": target_bytes,
            "output_bytes": result.output_bytes,
            "target_gap_bytes": target_bytes - result.output_bytes,
            "target_met": result.output_bytes <= target_bytes,
            "method": result.method,
            "image_scale": result.image_scale,
            "jpeg_quality": result.jpeg_quality,
            "vector_dpi": result.vector_dpi,
            "images_processed": result.images_processed,
            "vector_pages_processed": result.vector_pages_processed,
            "figures_processed": result.figures_processed,
            "candidate_attempts": result.candidate_attempts,
            "render_cache_hits": result.render_cache_hits,
            "render_cache_misses": result.render_cache_misses,
            "native_worker_used": result.native_worker_used,
            "native_render_batches": result.native_render_batches,
            "native_render_tasks": result.native_render_tasks,
            "native_render_seconds": round(result.native_render_seconds, 3),
            "output_sha256": sha256_file(destination),
        },
        assets,
    )


def render_pages(path: Path, dpi: int = 144) -> tuple[list[Image.Image], str]:
    images: list[Image.Image] = []
    text_parts: list[str] = []
    scale = dpi / 72.0
    with fitz.open(path) as document:
        for page in document:
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csGRAY,
                alpha=False,
                annots=False,
            )
            image = Image.frombytes(
                "L",
                (pixmap.width, pixmap.height),
                pixmap.samples,
                "raw",
                "L",
                pixmap.stride,
                1,
            )
            image.thumbnail((1100, 1550), Image.Resampling.LANCZOS)
            images.append(image.copy())
            text_parts.append(page.get_text("text"))
    return images, "\n".join(text_parts)


def image_metrics(
    reference_pages: list[Image.Image], candidate_pages: list[Image.Image]
) -> dict[str, Any]:
    squared_error = 0.0
    edge_squared_error = 0.0
    pixel_count = 0
    dark_increases: list[float] = []
    compared = min(len(reference_pages), len(candidate_pages))
    for reference, candidate in zip(reference_pages, candidate_pages):
        if candidate.size != reference.size:
            candidate = candidate.resize(reference.size, Image.Resampling.LANCZOS)
        difference = ImageChops.difference(reference, candidate)
        histogram = difference.histogram()
        squared_error += sum(count * value * value for value, count in enumerate(histogram))
        pixel_count += reference.width * reference.height

        reference_edges = reference.filter(ImageFilter.FIND_EDGES)
        candidate_edges = candidate.filter(ImageFilter.FIND_EDGES)
        edge_histogram = ImageChops.difference(
            reference_edges, candidate_edges
        ).histogram()
        edge_squared_error += sum(
            count * value * value
            for value, count in enumerate(edge_histogram)
        )

        reference_histogram = reference.histogram()
        candidate_histogram = candidate.histogram()
        total = max(1, reference.width * reference.height)
        dark_increases.append(
            (sum(candidate_histogram[:18]) - sum(reference_histogram[:18]))
            / total
        )

    mse = squared_error / max(1, pixel_count)
    edge_mse = edge_squared_error / max(1, pixel_count)
    psnr = 99.0 if mse == 0 else 20 * math.log10(255 / math.sqrt(mse))
    edge_similarity = max(0.0, 1.0 - math.sqrt(edge_mse) / 255)
    max_dark_increase = max(dark_increases, default=0.0)
    return {
        "pages_compared": compared,
        "psnr_db": round(psnr, 3),
        "edge_similarity": round(edge_similarity, 6),
        "max_dark_pixel_ratio_increase": round(max_dark_increase, 6),
        "black_background_regression": max_dark_increase > 0.20,
    }


def compare_documents(source: Path, candidate: Path) -> dict[str, Any]:
    source_pages, source_text = render_pages(source)
    candidate_pages, candidate_text = render_pages(candidate)
    metrics = image_metrics(source_pages, candidate_pages)
    normalize = lambda value: re.sub(r"\s+", "", value)
    source_normalized = normalize(source_text)
    candidate_normalized = normalize(candidate_text)
    metrics.update(
        {
            "source_pages": len(source_pages),
            "candidate_pages": len(candidate_pages),
            "page_count_preserved": len(source_pages) == len(candidate_pages),
            "native_text_exact": source_normalized == candidate_normalized,
            "native_text_length_ratio": round(
                len(candidate_normalized) / max(1, len(source_normalized)), 6
            ),
        }
    )
    return metrics


def speedup(before: Any, after: Any) -> float | None:
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    if before <= 0 or after <= 0:
        return None
    return round(before / after, 3)


def markdown_report(report: dict[str, Any]) -> str:
    compression = report["compression"]
    ui = report["ui_source"]
    comparison = report.get("comparison", {})
    baseline = report.get("baseline_comparison", {})
    quality = report["quality"]["output_vs_source"]
    lines = [
        f"# PDF Size Reducer benchmark — {report['label']}",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "## Fixed corpus",
        "",
        "| File | Size | Pages | SHA-256 |",
        "|---|---:|---:|---|",
    ]
    for fixture in report["fixtures"].values():
        if fixture is None:
            continue
        lines.append(
            f"| {fixture['name']} | {fixture['size_mib']:.3f} MiB | "
            f"{fixture['pages']} | `{fixture['sha256']}` |"
        )
    if report.get("baseline_file"):
        lines.extend(
            [
                "",
                "Baseline fixture hashes match: "
                f"**{report['baseline_fixture_match']}**",
            ]
        )
    lines.extend(
        [
            "",
            "## Results",
            "",
            "| Metric | Current | Baseline | Change |",
            "|---|---:|---:|---:|",
            f"| Scan | {ui['scan_seconds']:.3f} s | "
            f"{comparison.get('baseline_scan_seconds', '—')} | "
            f"{baseline.get('scan_speedup', '—')}× |",
            f"| Scan + thumbnails | {ui['scan_and_thumbnails_seconds']:.3f} s | "
            f"{comparison.get('baseline_ui_total_seconds', '—')} | "
            f"{baseline.get('ui_total_speedup', '—')}× |",
            f"| Compression to target | {compression['compression_seconds']:.3f} s | "
            f"{comparison.get('baseline_compression_seconds', '—')} | "
            f"{baseline.get('compression_speedup', '—')}× |",
            f"| Compression main-process peak memory | {compression['peak_working_set_mib']} MiB | — | — |",
            f"| Output size | {compression['output_bytes']:,} B | — | "
            f"{compression['target_gap_bytes']:,} B below target |",
            f"| Candidate renders | {compression['candidate_attempts']} | — | — |",
            f"| Render-cache hits | {compression['render_cache_hits']} | 0 | — |",
            f"| C++ render time | {compression['native_render_seconds']:.3f} s | — | "
            f"{compression['native_render_tasks']} tasks |",
            f"| Max UI pause | {ui['max_event_loop_gap_ms']:.2f} ms | "
            f"{comparison.get('baseline_max_gap_ms', '—')} | — |",
            "",
            "## Quality gates",
            "",
            f"- Target met: **{compression['target_met']}**",
            f"- Page count preserved: **{quality['page_count_preserved']}**",
            f"- Native text exact: **{quality['native_text_exact']}**",
            f"- PSNR against source: **{quality['psnr_db']:.3f} dB**",
            f"- Edge similarity: **{quality['edge_similarity']:.6f}**",
            f"- Black-background regression: **{quality['black_background_regression']}**",
        ]
    )
    if "reference_vs_source" in report["quality"]:
        reference = report["quality"]["reference_vs_source"]
        lines.extend(
            [
                f"- PPT reference PSNR: **{reference['psnr_db']:.3f} dB**",
                f"- PPT reference edge similarity: **{reference['edge_similarity']:.6f}**",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--stress", type=Path)
    parser.add_argument("--target-mib", type=float, default=3.12)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--label", default="current")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "baseline-v3.3.1.json",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "build" / "benchmarks"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().resolve()
    reference = args.reference.expanduser().resolve() if args.reference else None
    stress = args.stress.expanduser().resolve() if args.stress else None
    for path in (source, reference, stress):
        if path is not None and not path.is_file():
            raise FileNotFoundError(path)

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{args.label}-output.pdf"
    target_bytes = round(args.target_mib * 1024**2)
    fixtures = {
        "source": describe_pdf(source),
        "reference": describe_pdf(reference) if reference else None,
        "stress": describe_pdf(stress) if stress else None,
    }

    print("Running UI scan and thumbnail benchmark…", flush=True)
    ui_source = run_ui_measurement(source, args.timeout)
    print("Running exact-size compression benchmark…", flush=True)
    compression, _assets = run_compression_measurement(
        source, output_pdf, target_bytes
    )
    print("Running visual and text quality gates…", flush=True)
    quality: dict[str, Any] = {
        "output_vs_source": compare_documents(source, output_pdf)
    }
    if reference is not None:
        quality["reference_vs_source"] = compare_documents(source, reference)

    ui_stress = None
    if stress is not None:
        print("Running large-PDF UI stress benchmark…", flush=True)
        ui_stress = run_ui_measurement(stress, args.timeout)

    baseline_data = None
    comparison: dict[str, Any] = {}
    baseline_comparison: dict[str, Any] = {}
    baseline_fixture_match: bool | None = None
    if args.baseline.is_file():
        baseline_data = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline_fixtures = baseline_data.get("fixtures", {})
        fixture_key_map = {
            "source": "source",
            "reference": "ppt_reference",
            "stress": "stress",
        }
        baseline_fixture_match = all(
            current is None
            or current["sha256"]
            == baseline_fixtures.get(fixture_key_map[key], {}).get("sha256")
            for key, current in fixtures.items()
        )
        if baseline_fixture_match:
            old_ui = baseline_data["results"]["automatica_ui"]
            old_compression = baseline_data["results"]["automatica_compression"]
            comparison = {
                "baseline_scan_seconds": old_ui["scan_seconds"],
                "baseline_ui_total_seconds": old_ui["scan_and_thumbnails_seconds"],
                "baseline_compression_seconds": old_compression["compression_seconds"],
                "baseline_max_gap_ms": old_ui["max_event_loop_gap_ms"],
            }
            baseline_comparison = {
                "scan_speedup": speedup(
                    old_ui["scan_seconds"], ui_source["scan_seconds"]
                ),
                "ui_total_speedup": speedup(
                    old_ui["scan_and_thumbnails_seconds"],
                    ui_source["scan_and_thumbnails_seconds"],
                ),
                "compression_speedup": speedup(
                    old_compression["compression_seconds"],
                    compression["compression_seconds"],
                ),
            }

    import PIL

    report: dict[str, Any] = {
        "schema_version": 1,
        "label": args.label,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "pymupdf": fitz.__version__,
            "pillow": PIL.__version__,
            "logical_cpus": os.cpu_count(),
        },
        "fixtures": fixtures,
        "ui_source": ui_source,
        "compression": compression,
        "quality": quality,
        "ui_stress": ui_stress,
        "comparison": comparison,
        "baseline_comparison": baseline_comparison,
        "baseline_fixture_match": baseline_fixture_match,
        "baseline_file": args.baseline.name if baseline_data else None,
    }
    json_path = output_dir / f"{args.label}.json"
    markdown_path = output_dir / f"{args.label}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_path.write_text(
        markdown_report(report), encoding="utf-8", newline="\n"
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
