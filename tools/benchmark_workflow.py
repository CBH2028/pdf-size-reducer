"""Same-session, alternating before/after PDF workflow measurements."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def measure(args):
    sys.path.insert(0, str(args.root.resolve()))
    import pymupdf as fitz
    import compressor
    import native_worker

    native_worker.find_native_worker.cache_clear()
    if native_worker.find_native_worker() is None:
        raise RuntimeError("Build a guarded native worker in each compared checkout first")
    inputs = args.output.parent / "inputs"
    result = {"case": args.case, "label": args.label}
    if args.case == "scan":
        start = time.perf_counter()
        assets, pages = compressor.list_pdf_assets(args.stress)
        result.update(seconds=time.perf_counter() - start, pages=pages,
                      assets=[asdict(asset) for asset in assets])
    elif args.case in {"merge", "links"}:
        files = [inputs / ("part1.pdf" if args.case == "merge" else "links1.pdf"),
                 inputs / ("part2.pdf" if args.case == "merge" else "links2.pdf")]
        start = time.perf_counter()
        merged = compressor.merge_pdfs(files, args.output)
        result.update(seconds=time.perf_counter() - start, pages=merged.page_count,
                      bytes=merged.output_bytes, native=merged.native_worker_used)
        with fitz.open(args.output) as document:
            result["text"] = [page.get_text() for page in document]
            if args.case == "links":
                result["link_count"] = sum(len(page.get_links()) for page in document)
    else:
        source = inputs / "compression.pdf"
        assets, _ = compressor.list_pdf_assets(source)
        images = {a.xref for a in assets if a.kind == "image"}
        figures = {}
        for asset in assets:
            if asset.kind == "figure":
                figures.setdefault(asset.page_numbers[0], []).append(asset.rect)
        target = int(source.stat().st_size * 0.7)
        start = time.perf_counter()
        compressed = compressor.compress_pdf(
            source, args.output, target,
            selected_image_xrefs=images, selected_vector_pages=set(),
            selected_figure_regions=figures,
        )
        result.update(seconds=time.perf_counter() - start, bytes=compressed.output_bytes,
                      target=target, native=compressed.native_worker_used,
                      planner=compressed.planned_mode)
        with fitz.open(source) as before, fitz.open(args.output) as after:
            if before.page_count != after.page_count:
                raise RuntimeError("Page count changed")
            result["text_preserved"] = all(before[p].get_text() == after[p].get_text() for p in range(len(before)))
            result["render_hashes"] = [hashlib.sha256(page.get_pixmap(matrix=fitz.Matrix(1, 1)).samples).hexdigest() for page in after]
        if not result["text_preserved"] or compressed.output_bytes > target:
            raise RuntimeError("Compression quality/target gate failed")
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--current", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--stress", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("build/benchmarks/v3.9-workflow/results.json"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--case", choices=["scan", "merge", "links", "compression"])
    parser.add_argument("--label")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.case:
        measure(args)
        return
    if args.baseline is None or not 1 <= args.repeats <= 10:
        parser.error("Supply --baseline and use 1 to 10 repeats")
    import pymupdf as fitz

    args.report = args.report.resolve()
    work = args.report.parent
    inputs = work / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    with fitz.open(args.stress) as stress:
        middle = len(stress) // 2
        for name, first, last in (("part1.pdf", 0, middle - 1), ("part2.pdf", middle, len(stress) - 1),
                                  ("compression.pdf", 0, min(5, len(stress) - 1))):
            with fitz.open() as document:
                document.insert_pdf(stress, from_page=first, to_page=last)
                document.save(inputs / name, garbage=4, deflate=True)
    for index in (1, 2):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((30, 30), f"Links document {index}")
            for link in range(400):
                x, y = 10 + (link % 20) * 25, 60 + (link // 20) * 30
                page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(x, y, x + 20, y + 20),
                                  "uri": f"https://example.com/{index}/{link}"})
            document.save(inputs / f"links{index}.pdf")
    report = {"stress_sha256": digest(args.stress), "baseline": str(args.baseline.resolve()),
              "current": str(args.current.resolve()), "runs": [], "median_seconds": {},
              "environment": {"python": sys.version, "pymupdf": fitz.VersionBind,
                              "platform": platform.platform(), "logical_cpus": os.cpu_count()},
              "checkouts": {}}
    for label, root in (("baseline", args.baseline), ("current", args.current)):
        root = root.resolve()
        report["checkouts"][label] = {
            "commit": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
            "sha256": {name: digest(root / name) for name in (
                "compressor.py", "native_worker.py", "native_worker/bin/pdf_fast_worker.exe",
                "native_worker/bin/pdf_fast_worker_backend.exe", "native_worker/bin/mupdfcpp64.dll",
            )},
        }
    env = os.environ.copy()
    env.pop("PDF_SIZE_REDUCER_DISABLE_NATIVE", None)
    env.pop("PDF_SIZE_REDUCER_NATIVE_WORKER", None)
    for case in ("scan", "merge", "links", "compression"):
        rows = []
        for repeat in range(args.repeats):
            order = [("baseline", args.baseline), ("current", args.current)]
            if repeat % 2:
                order.reverse()
            for label, root in order:
                output = work / f"{case}-{label}-{repeat}.pdf"
                command = [sys.executable, str(Path(__file__).resolve()), "--root", str(root.resolve()),
                           "--case", case, "--label", label, "--stress", str(args.stress.resolve()), "--output", str(output)]
                run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
                if run.returncode:
                    raise RuntimeError(run.stderr or run.stdout)
                row = json.loads(run.stdout.splitlines()[-1])
                rows.append(row)
                print(f"{case} {label} {repeat + 1}: {row['seconds']:.3f}s", flush=True)
        comparable = "assets" if case == "scan" else "render_hashes" if case == "compression" else "text"
        if any(row[comparable] != rows[0][comparable] for row in rows):
            raise RuntimeError(f"Before/after content differed: {case}")
        if case == "links" and any(row["link_count"] != 800 for row in rows):
            raise RuntimeError("Merged links were lost")
        for row in rows:
            row.pop("text", None)
            row.pop("assets", None)
        report["runs"].extend(rows)
        report["median_seconds"][case] = {label: statistics.median(row["seconds"] for row in rows if row["label"] == label)
                                             for label in ("baseline", "current")}
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["median_seconds"], indent=2))


if __name__ == "__main__":
    main()
