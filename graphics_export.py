"""Lossless, local export of PDF Figures and embedded images.

Detected Figure regions are copied through an intermediate PDF page before
being serialized as SVG, PDF, or a high-resolution PNG. Existing PDF paths
stay vector data in SVG/PDF. A bitmap cannot truthfully be converted to vector
without tracing and changing its appearance, so standalone bitmaps retain
their original pixels in every selected container format.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from compressor import (
    CompressionCancelled,
    CompressionError,
    PDFAsset,
    PDFSourceState,
    get_pdf_source_state,
)

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with PyMuPDF 1.24.
    import fitz  # type: ignore[no-redef]


ProgressCallback = Callable[[int, str], None]
MAX_EXPORT_ASSETS = 20_000
MAX_SVG_BYTES = 512 * 1024**2
MAX_TOTAL_PAYLOAD_BYTES = 8 * 1024**3
MANIFEST_NAME = "manifest.json"
SUPPORTED_FORMATS = frozenset({"svg", "png", "pdf"})
DEFAULT_FORMATS = frozenset({"svg"})
FIGURE_PNG_DPI = 600
MAX_FIGURE_PNG_PIXELS = 64_000_000


@dataclass(frozen=True)
class GraphicsExportResult:
    """Summary of one completed graphics export."""

    source_path: Path
    output_directory: Path
    manifest_path: Path
    item_count: int
    figure_count: int
    image_count: int
    svg_count: int
    png_count: int
    pdf_count: int
    formats: tuple[str, ...]
    total_bytes: int


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CompressionCancelled("高清图导出已取消。")


def _notify(callback: ProgressCallback | None, value: int, message: str) -> None:
    if callback is not None:
        callback(max(0, min(100, int(value))), message)


def _safe_fragment(value: str, fallback: str, limit: int = 64) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = "".join(character for character in value if ord(character) >= 32)
    value = re.sub(r'[<>:"/\\|?*]+', "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    value = value[:limit].rstrip(" ._") or fallback
    if value.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        value = f"_{value}"
    return value


def suggest_graphics_export_directory(source: str | Path, parent: str | Path) -> Path:
    """Return a non-existing, human-readable export directory."""
    source_path = Path(source)
    parent_path = Path(parent).expanduser().resolve()
    stem = _safe_fragment(source_path.stem, "PDF", limit=80)
    base = f"{stem}_高清图"
    candidate = parent_path / base
    suffix = 2
    while candidate.exists():
        candidate = parent_path / f"{base} ({suffix})"
        suffix += 1
    return candidate


def _page_label(page_numbers: Iterable[int]) -> str:
    numbers = sorted(set(int(number) + 1 for number in page_numbers))
    if not numbers:
        return "unknown"
    ranges: list[str] = []
    first = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(first) if first == previous else f"{first}-{previous}")
        first = previous = number
    ranges.append(str(first) if first == previous else f"{first}-{previous}")
    return "_".join(ranges)


def _asset_stem(asset: PDFAsset, index: int) -> str:
    pages = _page_label(asset.page_numbers)
    if asset.kind == "figure":
        title = _safe_fragment(asset.title, "figure")
        return f"{index:04d}_page_{pages}_figure_{title}"
    dimensions = (
        f"_{max(0, asset.width)}x{max(0, asset.height)}"
        if asset.width and asset.height else ""
    )
    return f"{index:04d}_page_{pages}_image{dimensions}"


def _validated_figure_rect(document: fitz.Document, asset: PDFAsset) -> fitz.Rect:
    if len(asset.page_numbers) != 1 or asset.rect is None:
        raise CompressionError(f"Figure {asset.key!r} 缺少有效页面或区域。")
    page_number = asset.page_numbers[0]
    if not 0 <= page_number < document.page_count:
        raise CompressionError(f"Figure {asset.key!r} 的页码超出范围。")
    try:
        rectangle = fitz.Rect(asset.rect) & document[page_number].rect
    except (RuntimeError, TypeError, ValueError) as exc:
        raise CompressionError(f"Figure {asset.key!r} 的区域无效。") from exc
    if rectangle.is_empty or rectangle.is_infinite or rectangle.width < 1 or rectangle.height < 1:
        raise CompressionError(f"Figure {asset.key!r} 的区域为空或过小。")
    return rectangle


def _figure_contains_raster(page: fitz.Page, rectangle: fitz.Rect) -> bool:
    try:
        image_info = page.get_image_info(xrefs=True)
    except (RuntimeError, ValueError):
        return False
    for info in image_info:
        try:
            bounds = fitz.Rect(info["bbox"])
        except (KeyError, TypeError, ValueError):
            continue
        overlap = bounds & rectangle
        if not overlap.is_empty and overlap.width > 0 and overlap.height > 0:
            return True
    return False


def _figure_outputs(
    document: fitz.Document,
    asset: PDFAsset,
    formats: frozenset[str],
) -> tuple[dict[str, bytes], dict[str, object]]:
    rectangle = _validated_figure_rect(document, asset)
    page_number = asset.page_numbers[0]
    page = document[page_number]
    mixed = _figure_contains_raster(page, rectangle)
    outputs: dict[str, bytes] = {}
    png_dpi: int | None = None
    with fitz.open() as clipped:
        target_page = clipped.new_page(width=rectangle.width, height=rectangle.height)
        target_page.show_pdf_page(
            target_page.rect,
            document,
            pno=page_number,
            clip=rectangle,
            keep_proportion=False,
        )
        if "svg" in formats:
            svg = target_page.get_svg_image(text_as_path=1).encode("utf-8")
            if not svg.startswith(b"<svg") or b"</svg>" not in svg[-64:]:
                raise CompressionError(f"Figure {asset.key!r} 未生成有效 SVG。")
            if len(svg) > MAX_SVG_BYTES:
                raise CompressionError(f"Figure {asset.key!r} 的 SVG 超过 512 MB 安全上限。")
            outputs["svg"] = svg
        if "pdf" in formats:
            outputs["pdf"] = clipped.tobytes(garbage=4, deflate=True)
        if "png" in formats:
            # Keep the default genuinely high-resolution, while bounding a
            # malicious/full-page region before PyMuPDF allocates its pixmap.
            scale = FIGURE_PNG_DPI / 72
            requested_pixels = rectangle.width * rectangle.height * scale * scale
            if requested_pixels > MAX_FIGURE_PNG_PIXELS:
                scale *= (MAX_FIGURE_PNG_PIXELS / requested_pixels) ** 0.5
            png_dpi = max(72, min(FIGURE_PNG_DPI, round(scale * 72)))
            pixmap = target_page.get_pixmap(
                matrix=fitz.Matrix(png_dpi / 72, png_dpi / 72),
                alpha=True,
            )
            try:
                outputs["png"] = pixmap.tobytes("png")
            finally:
                pixmap = None
    mode = "mixed-vector-raster-preserved" if mixed else "native-vector-preserved"
    details: dict[str, object] = {
        "preservation_mode": mode,
        "vector_content_preserved": True,
        "raster_content_preserved": mixed,
        "width_points": round(rectangle.width, 4),
        "height_points": round(rectangle.height, 4),
    }
    if png_dpi is not None:
        details["png_render_dpi"] = png_dpi
    return outputs, details


def _image_pixmap(document: fitz.Document, asset: PDFAsset) -> fitz.Pixmap:
    if asset.xref is None or asset.xref <= 0 or asset.xref >= document.xref_length():
        raise CompressionError(f"图片 {asset.key!r} 的对象编号无效。")
    try:
        extracted = document.extract_image(asset.xref)
        image_bytes = extracted.get("image")
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise CompressionError(f"图片 {asset.key!r} 没有可导出的像素数据。")
        base = fitz.Pixmap(image_bytes)
        smask = asset.smask or int(extracted.get("smask", 0) or 0)
        if smask > 0:
            mask_data = document.extract_image(smask).get("image")
            if isinstance(mask_data, bytes) and mask_data:
                mask = fitz.Pixmap(mask_data)
                try:
                    if mask.width == base.width and mask.height == base.height:
                        combined = fitz.Pixmap(base, mask)
                        base = combined
                finally:
                    mask = None
        if base.colorspace is None:
            raise CompressionError(f"图片 {asset.key!r} 只有蒙版，没有颜色数据。")
        if base.n - int(bool(base.alpha)) > 3:
            converted = fitz.Pixmap(fitz.csRGB, base)
            base = converted
        return base
    except CompressionError:
        raise
    except (RuntimeError, ValueError) as exc:
        raise CompressionError(f"无法读取图片 {asset.key!r}：{exc}") from exc


def _raster_svg(png: bytes, width: int, height: int, title: str) -> bytes:
    encoded = base64.b64encode(png).decode("ascii")
    escaped_title = html.escape(title or "Embedded PDF image", quote=True)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f"  <title>{escaped_title}</title>\n"
        "  <desc>Original PDF bitmap embedded losslessly; no vector tracing was applied.</desc>\n"
        f'  <image width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>\n'
        "</svg>\n"
    )
    return svg.encode("utf-8")


def _raster_pdf(png: bytes, width: int, height: int) -> bytes:
    """Put original pixels on one PDF page without resampling them."""
    # A 300-DPI physical page size is practical for documents and printing;
    # insert_image still stores the source pixmap at its exact pixel size.
    page_width = max(1.0, width * 72 / 300)
    page_height = max(1.0, height * 72 / 300)
    with fitz.open() as output:
        page = output.new_page(width=page_width, height=page_height)
        page.insert_image(page.rect, stream=png, keep_proportion=False)
        return output.tobytes(garbage=4, deflate=True)


def _normalize_formats(formats: Iterable[str] | None) -> frozenset[str]:
    selected = DEFAULT_FORMATS if formats is None else frozenset(
        str(value).strip().lower() for value in formats
    )
    if not selected:
        raise CompressionError("请至少选择一种导出格式。")
    unsupported = selected - SUPPORTED_FORMATS
    if unsupported:
        raise CompressionError(f"不支持的导出格式：{', '.join(sorted(unsupported))}。")
    return selected


def _manifest_item(asset: PDFAsset, index: int) -> dict[str, object]:
    return {
        "index": index,
        "source_asset_key": asset.key,
        "type": asset.kind,
        "source_pages": [number + 1 for number in asset.page_numbers],
        "title": asset.title or None,
    }


def export_pdf_graphics(
    source: str | Path,
    output_directory: str | Path,
    assets: Iterable[PDFAsset],
    *,
    formats: Iterable[str] | None = None,
    expected_source_state: PDFSourceState | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> GraphicsExportResult:
    """Export detected assets into a new directory without touching the PDF.

    ``output_directory`` must not already exist.  If any item fails, the fresh
    directory is removed so callers never mistake a partial export for success.
    """
    source_path = Path(source).expanduser().resolve()
    destination = Path(output_directory).expanduser().resolve()
    items = list(assets)
    selected_formats = _normalize_formats(formats)
    if not source_path.is_file() or source_path.suffix.lower() != ".pdf":
        raise CompressionError("请选择一个存在的 PDF 文件。")
    if not items:
        raise CompressionError("没有识别到可导出的 Figure 或独立图片。")
    if len(items) > MAX_EXPORT_ASSETS:
        raise CompressionError(f"一次最多导出 {MAX_EXPORT_ASSETS} 个图形。")
    if destination.exists():
        raise CompressionError("导出文件夹已经存在；为安全起见不会覆盖。")
    if destination == source_path or source_path in destination.parents:
        raise CompressionError("导出文件夹不能创建在源 PDF 路径内部。")
    before = get_pdf_source_state(source_path)
    if expected_source_state is not None and before != expected_source_state:
        raise CompressionError("源 PDF 已更改，请重新加载后再导出。")
    _check_cancel(cancel_event)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    completed = False
    manifest_items: list[dict[str, object]] = []
    payload_bytes = 0
    svg_count = 0
    png_count = 0
    pdf_count = 0
    figure_count = 0
    image_count = 0
    _notify(progress_callback, 1, "正在准备高清图导出…")
    try:
        destination.mkdir()
        created = True
        with fitz.open(source_path) as document:
            if document.needs_pass:
                raise CompressionError("此 PDF 受密码保护，请先解密后再导出。")
            if not document.is_pdf or document.page_count == 0:
                raise CompressionError("文件中没有可处理的 PDF 页面。")
            for index, asset in enumerate(items, start=1):
                _check_cancel(cancel_event)
                stem = _asset_stem(asset, index)
                record = _manifest_item(asset, index)
                if asset.kind == "figure":
                    outputs, details = _figure_outputs(
                        document, asset, selected_formats
                    )
                    figure_count += 1
                    record.update(details)
                    files: dict[str, str] = {}
                    for extension, data in outputs.items():
                        output_path = destination / f"{stem}.{extension}"
                        output_path.write_bytes(data)
                        files[extension] = output_path.name
                        payload_bytes += len(data)
                        svg_count += extension == "svg"
                        png_count += extension == "png"
                        pdf_count += extension == "pdf"
                    record["files"] = files
                elif asset.kind == "image":
                    pixmap = _image_pixmap(document, asset)
                    try:
                        png = pixmap.tobytes("png")
                        width, height = pixmap.width, pixmap.height
                    finally:
                        pixmap = None
                    outputs = {}
                    if "png" in selected_formats:
                        outputs["png"] = png
                    if "svg" in selected_formats:
                        svg = _raster_svg(png, width, height, asset.title)
                        if len(svg) > MAX_SVG_BYTES:
                            raise CompressionError(f"图片 {asset.key!r} 的 SVG 超过 512 MB 安全上限。")
                        outputs["svg"] = svg
                    if "pdf" in selected_formats:
                        outputs["pdf"] = _raster_pdf(png, width, height)
                    files = {}
                    for extension, data in outputs.items():
                        output_path = destination / f"{stem}.{extension}"
                        output_path.write_bytes(data)
                        files[extension] = output_path.name
                        payload_bytes += len(data)
                        svg_count += extension == "svg"
                        png_count += extension == "png"
                        pdf_count += extension == "pdf"
                    image_count += 1
                    record.update(
                        {
                            "preservation_mode": "original-raster-preserved",
                            "vector_content_preserved": False,
                            "raster_content_preserved": True,
                            "pixel_width": width,
                            "pixel_height": height,
                            "files": files,
                            "note": "The PDF object is a bitmap. Selected container formats retain its original pixels; no vector tracing is applied.",
                        }
                    )
                else:
                    raise CompressionError(f"不支持的图形类型：{asset.kind!r}。")
                if payload_bytes > MAX_TOTAL_PAYLOAD_BYTES:
                    raise CompressionError("导出内容超过 8 GB 安全上限。")
                manifest_items.append(record)
                _notify(
                    progress_callback,
                    3 + round(94 * index / len(items)),
                    f"正在导出高清图 {index} / {len(items)}…",
                )

        _check_cancel(cancel_event)
        if get_pdf_source_state(source_path) != before:
            raise CompressionError("源 PDF 在导出期间发生变化，未保留导出结果。")
        manifest = {
            "schema_version": 1,
            "generator": "PDF Size Reducer",
            "source_file": source_path.name,
            "item_count": len(manifest_items),
            "figure_count": figure_count,
            "standalone_image_count": image_count,
            "svg_count": svg_count,
            "png_count": png_count,
            "pdf_count": pdf_count,
            "requested_formats": sorted(selected_formats),
            "payload_bytes": payload_bytes,
            "vector_policy": (
                "Existing PDF vectors are preserved in Figure SVG/PDF files, and SVG text is emitted as outlines. "
                "Bitmap objects retain their original pixel dimensions and are not presented as true vectors."
            ),
            "items": manifest_items,
        }
        manifest_path = destination / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _check_cancel(cancel_event)
        if get_pdf_source_state(source_path) != before:
            raise CompressionError("源 PDF 在导出期间发生变化，未保留导出结果。")
        total_bytes = sum(
            path.stat().st_size for path in destination.iterdir() if path.is_file()
        )
        completed = True
        _notify(progress_callback, 100, f"高清图导出完成，共 {len(items)} 项")
        return GraphicsExportResult(
            source_path=source_path,
            output_directory=destination,
            manifest_path=manifest_path,
            item_count=len(items),
            figure_count=figure_count,
            image_count=image_count,
            svg_count=svg_count,
            png_count=png_count,
            pdf_count=pdf_count,
            formats=tuple(sorted(selected_formats)),
            total_bytes=total_bytes,
        )
    except CompressionCancelled:
        raise
    except CompressionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise CompressionError(f"高清图导出失败：{exc}") from exc
    finally:
        if created and not completed:
            shutil.rmtree(destination, ignore_errors=True)


def install_graphics_export(
    candidate: str | Path,
    destination: str | Path,
    source: str | Path,
    expected_source_state: PDFSourceState,
    cancel_event: threading.Event | None = None,
) -> None:
    """Atomically install a verified, staged export directory."""
    candidate_path = Path(candidate).resolve()
    destination_path = Path(destination).resolve()
    source_path = Path(source).resolve()
    _check_cancel(cancel_event)
    if destination_path.exists():
        raise CompressionError("导出文件夹已被其他程序创建，未覆盖现有内容。")
    if not candidate_path.is_dir() or candidate_path.is_symlink():
        raise CompressionError("后台任务未生成有效的导出文件夹。")
    manifest_path = candidate_path / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise CompressionError("后台任务未生成有效的导出清单。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompressionError("后台任务生成的导出清单无效。") from exc
    if not isinstance(manifest, dict) or manifest.get("item_count") != len(manifest.get("items", [])):
        raise CompressionError("后台任务生成的导出清单不完整。")
    for entry in manifest.get("items", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("files"), dict):
            raise CompressionError("后台任务生成的导出清单不完整。")
        for filename in entry["files"].values():
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise CompressionError("导出清单包含不安全的文件名。")
            path = candidate_path / filename
            if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
                raise CompressionError("后台任务生成的导出文件不完整。")
    if get_pdf_source_state(source_path) != expected_source_state:
        raise CompressionError("源 PDF 在导出期间发生变化，未写入最终文件夹。")
    _check_cancel(cancel_event)
    try:
        os.replace(candidate_path, destination_path)
    except OSError as exc:
        raise CompressionError(f"无法安全写入导出文件夹：{exc}") from exc
