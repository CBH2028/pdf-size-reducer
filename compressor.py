"""PDF compression engine with a hard output-size target.

The compressor first tries a lossless structural rewrite. It can then
recompress standalone bitmaps or rasterize explicitly selected, captioned
paper Figures as complete composite regions. Pages are never rasterized as a
whole, and text outside selected Figure regions remains native PDF text.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw

from native_worker import (
    NativeRenderRequest,
    NativeWorkerCancelled,
    NativeWorkerError,
    NativeWorkerSession,
    find_native_worker,
)

try:
    import pymupdf as fitz
except ImportError:  # Compatibility with PyMuPDF 1.24.
    import fitz  # type: ignore[no-redef]


ProgressCallback = Callable[[int, str], None]
QUALITY_STEPS = 400


class CompressionError(RuntimeError):
    """Base error raised by the compression engine."""


class CompressionCancelled(CompressionError):
    """Raised when the caller requests cancellation."""


class PasswordProtectedPDF(CompressionError):
    """Raised for a PDF that cannot be opened without a password."""


class NoCompressibleImagesError(CompressionError):
    """Raised when reducing page quality would be the only remaining option."""


class TargetTooSmallError(CompressionError):
    """Raised when even the lowest-quality PDF is larger than the target."""

    def __init__(self, target_bytes: int, minimum_bytes: int) -> None:
        self.target_bytes = target_bytes
        self.minimum_bytes = minimum_bytes
        super().__init__(
            "在保持页面文字和图内小字符可读（Figure 不低于 180 DPI）的前提下，"
            f"当前文件最小约为 {format_bytes(minimum_bytes)}。"
        )


class PlannerUnavailable(RuntimeError):
    """The one-shot planner could not safely handle this document."""


@dataclass(frozen=True)
class CompressionResult:
    input_path: Path
    output_path: Path
    original_bytes: int
    output_bytes: int
    target_bytes: int
    method: str
    image_scale: float | None = None
    jpeg_quality: int | None = None
    images_processed: int = 0
    vector_pages_processed: int = 0
    figures_processed: int = 0
    vector_dpi: int | None = None
    candidate_attempts: int = 0
    render_cache_hits: int = 0
    render_cache_misses: int = 0
    native_worker_used: bool = False
    native_render_batches: int = 0
    native_render_tasks: int = 0
    native_render_seconds: float = 0.0
    planned_mode: bool = False
    native_master_renders: int = 0
    planned_variants: int = 0

    @property
    def saved_ratio(self) -> float:
        if not self.original_bytes:
            return 0.0
        return max(0.0, 1.0 - self.output_bytes / self.original_bytes)


@dataclass(frozen=True)
class PDFAsset:
    """One user-selectable paper figure, bitmap, or page vector layer."""

    key: str
    kind: str
    page_numbers: tuple[int, ...]
    xref: int | None = None
    smask: int = 0
    width: int = 0
    height: int = 0
    rect: tuple[float, float, float, float] | None = None
    title: str = ""
    storage_bytes: int = 0
    storage_is_estimate: bool = False

    @property
    def display_name(self) -> str:
        pages = format_page_ranges(set(self.page_numbers))
        if self.kind == "image":
            return f"第 {pages} 页 · 位图 {self.width}×{self.height}"
        if self.kind == "figure":
            return f"第 {pages} 页 · {self.title or '论文 Figure'}"
        return f"第 {pages} 页 · 矢量绘图层"

    @property
    def storage_label(self) -> str:
        if self.storage_bytes <= 0:
            return "当前占用未知"
        qualifier = "约 " if self.storage_is_estimate else ""
        return f"当前占用 {qualifier}{format_bytes(self.storage_bytes)}"


class RenderCache:
    """Bounded LRU cache for encoded Figure and vector-layer renders."""

    def __init__(self, max_bytes: int = 256 * 1024**2) -> None:
        self.max_bytes = max(0, max_bytes)
        self.current_bytes = 0
        self.hits = 0
        self.misses = 0
        self._items: OrderedDict[
            tuple[object, ...],
            tuple[bytes, tuple[float, float, float, float]],
        ] = OrderedDict()

    def get(
        self, key: tuple[object, ...]
    ) -> tuple[bytes, fitz.Rect] | None:
        cached = self._items.pop(key, None)
        if cached is None:
            self.misses += 1
            return None
        self.hits += 1
        self._items[key] = cached
        image_bytes, rectangle = cached
        return image_bytes, fitz.Rect(rectangle)

    def put(
        self,
        key: tuple[object, ...],
        value: tuple[bytes, fitz.Rect],
    ) -> None:
        image_bytes, rectangle = value
        size = len(image_bytes)
        if not self.max_bytes or size > self.max_bytes:
            return
        previous = self._items.pop(key, None)
        if previous is not None:
            self.current_bytes -= len(previous[0])
        stored = (image_bytes, tuple(rectangle))
        self._items[key] = stored
        self.current_bytes += size
        while self.current_bytes > self.max_bytes and self._items:
            _old_key, old_value = self._items.popitem(last=False)
            self.current_bytes -= len(old_value[0])


@dataclass(frozen=True)
class PlannedVariant:
    """One already-encoded quality choice for a planned asset."""

    score: int
    image_scale: float
    jpeg_quality: int
    dpi: int
    payload: bytes


@dataclass(frozen=True)
class PlannedAsset:
    """A bitmap or Figure with an ordered rate-distortion ladder."""

    key: tuple[object, ...]
    kind: str
    page_number: int
    variants: tuple[PlannedVariant, ...]
    xref: int | None = None
    rectangle: tuple[float, float, float, float] | None = None
    visual_weight: float = 1.0


@dataclass(frozen=True)
class PlannedStageResult:
    path: Path
    size: int
    selection: tuple[int, ...]
    attempts: int
    images_processed: int
    figures_processed: int


def format_bytes(size: int) -> str:
    """Return a compact human-readable byte count."""
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024**2:.2f} MB"


def _notify(callback: ProgressCallback | None, value: int, message: str) -> None:
    if callback:
        callback(max(0, min(100, value)), message)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise CompressionCancelled("压缩已取消。")


def _quality_profile(score: float) -> tuple[float, int]:
    """Map a 0..100 score to image scale/JPEG quality."""
    score = max(0, min(100, score))
    # For charts and diagrams, pixel resolution protects small glyphs much
    # better than spending the same bytes on JPEG quality. The high end reaches
    # near-lossless JPEG so targets between an optimized original and the old
    # quality-82 ceiling no longer have a large unreachable size gap.
    image_scale = 0.25 + 0.75 * score / 100
    jpeg_quality = round(45 + (98 - 45) * score / 100)
    return image_scale, jpeg_quality


def _vector_dpi(score: float) -> int:
    """Map quality to a readable 180..720 DPI Figure render."""
    score = max(0, min(100, score))
    return round(180 + (720 - 180) * score / 100)


def _planner_profiles(step: int = 40) -> tuple[tuple[int, float, int, int], ...]:
    """Return the shared quality ladder used by the one-shot planner."""
    step = max(1, min(QUALITY_STEPS, step))
    scores = list(range(0, QUALITY_STEPS + 1, step))
    if scores[-1] != QUALITY_STEPS:
        scores.append(QUALITY_STEPS)
    profiles = []
    for score in scores:
        quality_score = score * 100 / QUALITY_STEPS
        image_scale, jpeg_quality = _quality_profile(quality_score)
        profiles.append(
            (score, image_scale, jpeg_quality, _vector_dpi(quality_score))
        )
        if 200 <= score <= 280 and jpeg_quality < 100:
            # A same-resolution +1 JPEG step gives the global allocator a
            # cheap fine-grained option without another MuPDF render/scale.
            profiles.append(
                (
                    min(QUALITY_STEPS - 1, score + step // 4),
                    image_scale,
                    jpeg_quality + 1,
                    _vector_dpi(quality_score),
                )
            )
    return tuple(profiles)


def _save_lossless(source: fitz.Document, destination: Path) -> None:
    """Rewrite and garbage-collect a document without rasterizing pages."""
    options = {
        "garbage": 4,
        "deflate": True,
        "deflate_images": True,
        "deflate_fonts": True,
        "clean": True,
    }
    try:
        source.save(destination, use_objstms=1, **options)
    except TypeError:
        # Compatibility with older PyMuPDF releases.
        source.save(destination, **options)


def _collect_images(
    source: fitz.Document, allowed_pages: set[int] | None = None
) -> dict[int, tuple[int, int]]:
    """Map eligible image xrefs to their first page and optional soft mask.

    Image replacement is global for a PDF xref. If an image is shared by an
    allowed and an explicitly excluded page, it is left unchanged so the
    exclusion guarantee is not violated.
    """
    images: dict[int, tuple[int, int]] = {}
    occurrences: dict[int, set[int]] = {}
    for page_number in range(source.page_count):
        for image in source[page_number].get_images(full=True):
            xref = int(image[0])
            smask = int(image[1])
            if xref > 0:
                occurrences.setdefault(xref, set()).add(page_number)
                if xref not in images:
                    images[xref] = (page_number, smask)
    if allowed_pages is not None:
        images = {
            xref: location
            for xref, location in images.items()
            if occurrences[xref] and occurrences[xref].issubset(allowed_pages)
        }
    return images


def _collect_vector_pages(source: fitz.Document) -> list[int]:
    """Return pages containing PDF line-art/path drawing operators."""
    pages: list[int] = []
    for page_number in range(source.page_count):
        try:
            if source[page_number].get_drawings():
                pages.append(page_number)
        except (RuntimeError, ValueError):
            pass
    return pages


_FIGURE_CAPTION = re.compile(
    r"^\s*((?:fig(?:ure)?\.?\s*\d+(?:[a-z])?|图\s*\d+(?:[a-z])?))"
    r"\s*(?:[:：.．]|[-–—])\s*",
    re.IGNORECASE,
)


def _rect_union(rectangles: list[fitz.Rect]) -> fitz.Rect:
    result = fitz.Rect(rectangles[0])
    for rectangle in rectangles[1:]:
        result.include_rect(rectangle)
    return result


def _rectangles_near(first: fitz.Rect, second: fitz.Rect, gap: float = 12) -> bool:
    """Return whether two graphic bounds touch after a small expansion."""
    return not (
        first.x1 + gap < second.x0
        or second.x1 + gap < first.x0
        or first.y1 + gap < second.y0
        or second.y1 + gap < first.y0
    )


def _graphic_components(rectangles: list[fitz.Rect]) -> list[fitz.Rect]:
    """Merge nearby graphic bounds in linear time using a coarse grid.

    Some scientific plots contain tens of thousands of individual vector
    paths. Pairwise rectangle merging made asset discovery appear frozen on
    those PDFs. A small occupancy grid keeps the work proportional to the page
    area plus the number of paths.
    """
    if not rectangles:
        return []

    cell_size = 4.0
    expansion = 6.0
    min_x = min(rectangle.x0 for rectangle in rectangles) - expansion
    min_y = min(rectangle.y0 for rectangle in rectangles) - expansion
    max_x = max(rectangle.x1 for rectangle in rectangles) + expansion
    max_y = max(rectangle.y1 for rectangle in rectangles) + expansion
    width = max(1, int((max_x - min_x) / cell_size) + 2)
    height = max(1, int((max_y - min_y) / cell_size) + 2)

    mask = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for rectangle in rectangles:
        draw.rectangle(
            (
                int((rectangle.x0 - expansion - min_x) / cell_size),
                int((rectangle.y0 - expansion - min_y) / cell_size),
                int((rectangle.x1 + expansion - min_x) / cell_size),
                int((rectangle.y1 + expansion - min_y) / cell_size),
            ),
            fill=1,
        )

    pixels = mask.load()
    labels = [0] * (width * height)
    component_number = 0
    for y in range(height):
        for x in range(width):
            index = y * width + x
            if not pixels[x, y] or labels[index]:
                continue
            component_number += 1
            labels[index] = component_number
            pending: deque[tuple[int, int]] = deque([(x, y)])
            while pending:
                current_x, current_y = pending.popleft()
                for next_x, next_y in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    next_index = next_y * width + next_x
                    if pixels[next_x, next_y] and not labels[next_index]:
                        labels[next_index] = component_number
                        pending.append((next_x, next_y))

    grouped: dict[int, list[fitz.Rect]] = {}
    for rectangle in rectangles:
        center_x = min(
            width - 1,
            max(0, int((rectangle.x0 + rectangle.x1) / 2 - min_x) // 4),
        )
        center_y = min(
            height - 1,
            max(0, int((rectangle.y0 + rectangle.y1) / 2 - min_y) // 4),
        )
        label = labels[center_y * width + center_x]
        if label:
            grouped.setdefault(label, []).append(rectangle)
    return [_rect_union(component) for component in grouped.values()]


def _caption_lane(page: fitz.Page, caption_rect: fitz.Rect) -> str:
    """Classify a caption as left-column, right-column, or full-width."""
    midpoint = (page.rect.x0 + page.rect.x1) / 2
    if caption_rect.width <= page.rect.width * 0.47:
        if caption_rect.x1 <= midpoint + 10:
            return "left"
        if caption_rect.x0 >= midpoint - 10:
            return "right"
    return "full"


def _page_caption_lines(page: fitz.Page) -> list[tuple[str, fitz.Rect]]:
    """Return strict Figure-caption labels and their first-line bounds."""
    captions: list[tuple[str, fitz.Rect]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            match = _FIGURE_CAPTION.match(text)
            if match:
                captions.append((match.group(1).strip(), fitz.Rect(line["bbox"])))
    captions.sort(key=lambda item: (item[1].y0, item[1].x0))
    return captions


def _figure_rect_for_caption(
    page: fitz.Page,
    caption_rect: fitz.Rect,
    lower_limit: float,
    lane: str = "full",
    graphic_rectangles: list[fitz.Rect] | None = None,
) -> fitz.Rect | None:
    """Infer the complete graphic immediately above one Figure caption."""
    if graphic_rectangles is None:
        graphic_rectangles = []
        try:
            graphic_rectangles.extend(
                fitz.Rect(item["rect"]) for item in page.get_drawings()
            )
        except (RuntimeError, ValueError):
            pass
        try:
            graphic_rectangles.extend(
                fitz.Rect(info["bbox"]) for info in page.get_image_info(xrefs=True)
            )
        except (RuntimeError, ValueError):
            pass

    midpoint = (page.rect.x0 + page.rect.x1) / 2
    lane_rect = (
        fitz.Rect(page.rect.x0, page.rect.y0, midpoint + 8, page.rect.y1)
        if lane == "left"
        else fitz.Rect(midpoint - 8, page.rect.y0, page.rect.x1, page.rect.y1)
        if lane == "right"
        else page.rect
    )
    candidates: list[fitz.Rect] = []
    for raw_rectangle in graphic_rectangles:
        rectangle = raw_rectangle & page.rect
        horizontal_overlap = max(
            0.0,
            min(rectangle.x1, lane_rect.x1) - max(rectangle.x0, lane_rect.x0),
        )
        if (
            rectangle.y0 >= lower_limit - 2
            and rectangle.y1 <= caption_rect.y0 + 2
            and rectangle.y1 > lower_limit
            and rectangle.x1 > rectangle.x0
            and rectangle.y1 >= rectangle.y0
            and horizontal_overlap >= min(4.0, max(0.1, rectangle.width * 0.2))
        ):
            candidates.append(rectangle)
    if not candidates:
        return None

    meaningful = [
        component
        for component in _graphic_components(candidates)
        if component.width >= 18 and component.height >= 10
    ]
    if not meaningful:
        return None

    # The component ending closest to the caption is the captioned Figure.
    # Area is the tie-breaker for multi-part drawings with aligned bottoms.
    selected = max(
        meaningful,
        key=lambda rectangle: (rectangle.y1, rectangle.width * rectangle.height),
    )

    # Merge adjacent panels in the same Figure, including vertically stacked
    # subplots such as (a)/(b). Repeat because each merge may make another
    # neighboring panel eligible.
    remaining = [component for component in meaningful if component != selected]
    changed = True
    while changed:
        changed = False
        for component in list(remaining):
            vertical_overlap = max(
                0.0,
                min(selected.y1, component.y1) - max(selected.y0, component.y0),
            )
            horizontal_overlap = max(
                0.0,
                min(selected.x1, component.x1) - max(selected.x0, component.x0),
            )
            shorter_height = max(1.0, min(selected.height, component.height))
            shorter_width = max(1.0, min(selected.width, component.width))
            horizontal_gap = max(
                0.0,
                max(selected.x0, component.x0) - min(selected.x1, component.x1),
            )
            vertical_gap = max(
                0.0,
                max(selected.y0, component.y0) - min(selected.y1, component.y1),
            )
            side_by_side = (
                vertical_overlap / shorter_height >= 0.45
                and horizontal_gap <= 42
            )
            vertically_stacked = (
                horizontal_overlap / shorter_width >= 0.45
                and vertical_gap <= 50
            )
            if side_by_side or vertically_stacked:
                selected.include_rect(component)
                remaining.remove(component)
                changed = True

    padding = 3.0
    result = fitz.Rect(
        max(page.rect.x0, selected.x0 - padding),
        max(page.rect.y0, selected.y0 - padding),
        min(page.rect.x1, selected.x1 + padding),
        min(caption_rect.y0 - 1, selected.y1 + padding),
    )
    return result if result.width >= 18 and result.height >= 10 else None


def _xref_stream_storage_bytes(document: fitz.Document, xref: int) -> int:
    """Return the encoded stream bytes stored in one PDF object."""
    if xref <= 0:
        return 0
    try:
        stream = document.xref_stream_raw(xref)
    except (RuntimeError, ValueError):
        return 0
    return len(stream) if stream else 0


def _image_storage_bytes(
    document: fitz.Document, xref: int, smask: int = 0
) -> int:
    """Return the actual encoded bytes of an image and its soft mask."""
    xrefs = {value for value in (xref, smask) if value > 0}
    return sum(_xref_stream_storage_bytes(document, value) for value in xrefs)


def _drawing_storage_weight(drawing: dict[str, object]) -> int:
    """Estimate the relative PDF instruction cost of one vector path."""
    items = drawing.get("items", ())
    if not isinstance(items, (list, tuple)):
        return 1
    weight = 0
    for item in items:
        if isinstance(item, (list, tuple)):
            # Curves and rectangles carry more coordinates than simple lines.
            weight += max(12, 12 * (len(item) - 1))
        else:
            weight += 12
    return max(1, weight)


def _detect_figure_assets(
    document: fitz.Document,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[PDFAsset]:
    """Detect complete paper Figures, using captions as semantic anchors."""
    assets: list[PDFAsset] = []
    for page_number in range(document.page_count):
        _check_cancel(cancel_event)
        page = document[page_number]
        captions = _page_caption_lines(page)
        if not captions:
            _notify(
                progress_callback,
                8 + round(48 * (page_number + 1) / max(1, document.page_count)),
                f"正在阅读第 {page_number + 1} / {document.page_count} 页，寻找完整 Figure…",
            )
            continue
        graphic_rectangles: list[fitz.Rect] = []
        drawings: list[dict[str, object]] = []
        try:
            drawings = page.get_drawings()
            graphic_rectangles.extend(fitz.Rect(item["rect"]) for item in drawings)
        except (RuntimeError, ValueError):
            pass
        image_infos: list[dict[str, object]] = []
        try:
            image_infos = page.get_image_info(xrefs=True)
            graphic_rectangles.extend(fitz.Rect(info["bbox"]) for info in image_infos)
        except (RuntimeError, ValueError):
            pass

        page_stream_storage = sum(
            _xref_stream_storage_bytes(document, int(xref))
            for xref in (page.get_contents() or ())
        )
        try:
            # Scientific plots exported by LaTeX / MATLAB are frequently
            # stored as Form XObjects. Their streams can be far larger than
            # the page's tiny placement stream, so include them in the Figure
            # allocation or the displayed size would be severely understated.
            form_xrefs = {
                int(item[0]) for item in page.get_xobjects() if int(item[0]) > 0
            }
        except (RuntimeError, ValueError):
            form_xrefs = set()
        content_storage = page_stream_storage + sum(
            _xref_stream_storage_bytes(document, xref) for xref in form_xrefs
        )
        weighted_drawings = [
            (fitz.Rect(drawing["rect"]), _drawing_storage_weight(drawing))
            for drawing in drawings
        ]
        total_complexity = sum(weight for _rect, weight in weighted_drawings)
        try:
            # Text shares the page content stream but is kept native during
            # Figure compression, so include it in the allocation denominator.
            total_complexity += len(page.get_text("text"))
        except (RuntimeError, ValueError):
            pass
        image_smasks = {
            int(image[0]): int(image[1]) for image in page.get_images(full=True)
        }
        lane_bottom = {"left": page.rect.y0, "right": page.rect.y0}
        for caption_index, (label, caption_rect) in enumerate(
            captions, start=1
        ):
            lane = _caption_lane(page, caption_rect)
            lower_limit = (
                max(lane_bottom.values())
                if lane == "full"
                else lane_bottom[lane]
            )
            rectangle = _figure_rect_for_caption(
                page,
                caption_rect,
                lower_limit + 2,
                lane,
                graphic_rectangles,
            )
            if lane == "full":
                lane_bottom["left"] = caption_rect.y1
                lane_bottom["right"] = caption_rect.y1
            else:
                lane_bottom[lane] = caption_rect.y1
            if rectangle is None:
                continue

            inside_vector_weight = sum(
                weight
                for bounds, weight in weighted_drawings
                if _rect_center_is_inside(bounds, rectangle)
            )
            vector_storage = round(
                content_storage * inside_vector_weight / max(1, total_complexity)
            )
            inside_image_xrefs = {
                int(info.get("xref", 0))
                for info in image_infos
                if int(info.get("xref", 0)) > 0
                and _rect_center_is_inside(fitz.Rect(info["bbox"]), rectangle)
            }
            image_storage = sum(
                _image_storage_bytes(
                    document, xref, image_smasks.get(xref, 0)
                )
                for xref in inside_image_xrefs
            )
            assets.append(
                PDFAsset(
                    key=f"figure:{page_number}:{caption_index}",
                    kind="figure",
                    page_numbers=(page_number,),
                    rect=tuple(rectangle),
                    title=label,
                    storage_bytes=vector_storage + image_storage,
                    storage_is_estimate=True,
                )
            )
        _notify(
            progress_callback,
            8 + round(48 * (page_number + 1) / max(1, document.page_count)),
            f"正在阅读第 {page_number + 1} / {document.page_count} 页，寻找完整 Figure…",
        )
    return assets


def _rect_center_is_inside(inner: fitz.Rect, outer: fitz.Rect) -> bool:
    center = (inner.tl + inner.br) / 2
    return center in outer


def list_pdf_assets(
    input_path: str | Path,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[list[PDFAsset], int]:
    """List complete paper Figures plus any ungrouped graphic objects."""
    _check_cancel(cancel_event)
    _notify(progress_callback, 2, "正在验证 PDF 并读取目录…")
    with fitz.open(Path(input_path)) as document:
        _notify(
            progress_callback,
            6,
            f"已打开 PDF，共 {document.page_count} 页",
        )
        figure_assets = _detect_figure_assets(
            document, progress_callback, cancel_event
        )
        figures_by_page: dict[int, list[fitz.Rect]] = {}
        for asset in figure_assets:
            if asset.rect is not None:
                figures_by_page.setdefault(asset.page_numbers[0], []).append(
                    fitz.Rect(asset.rect)
                )

        # An image xref is replaced globally. Hide it as a standalone choice if
        # any occurrence belongs to a detected Figure, preventing double work
        # and ensuring that deselecting the Figure really preserves it.
        figure_image_xrefs: set[int] = set()
        for page_number, figure_rectangles in figures_by_page.items():
            _check_cancel(cancel_event)
            for info in document[page_number].get_image_info(xrefs=True):
                xref = int(info.get("xref", 0))
                image_rect = fitz.Rect(info["bbox"])
                if xref > 0 and any(
                    _rect_center_is_inside(image_rect, figure_rect)
                    for figure_rect in figure_rectangles
                ):
                    figure_image_xrefs.add(xref)

        _notify(progress_callback, 60, "正在整理 Figure 内的图像与矢量对象…")

        image_data: dict[int, dict[str, object]] = {}
        for page_number in range(document.page_count):
            _check_cancel(cancel_event)
            for image in document[page_number].get_images(full=True):
                xref = int(image[0])
                if xref <= 0 or xref in figure_image_xrefs:
                    continue
                entry = image_data.setdefault(
                    xref,
                    {
                        "pages": set(),
                        "smask": int(image[1]),
                        "width": int(image[2]),
                        "height": int(image[3]),
                    },
                )
                pages = entry["pages"]
                assert isinstance(pages, set)
                pages.add(page_number)

            _notify(
                progress_callback,
                60
                + round(24 * (page_number + 1) / max(1, document.page_count)),
                f"正在建立第 {page_number + 1} / {document.page_count} 页的图像索引…",
            )

        assets = list(figure_assets)
        image_items = list(image_data.items())
        for item_index, (xref, data) in enumerate(image_items):
            _check_cancel(cancel_event)
            assets.append(
                PDFAsset(
                    key=f"image:{xref}",
                    kind="image",
                    page_numbers=tuple(
                        sorted(data["pages"])  # type: ignore[arg-type]
                    ),
                    xref=xref,
                    smask=int(data["smask"]),
                    width=int(data["width"]),
                    height=int(data["height"]),
                    storage_bytes=_image_storage_bytes(
                        document, xref, int(data["smask"])
                    ),
                )
            )
            _notify(
                progress_callback,
                85 + round(11 * (item_index + 1) / max(1, len(image_items))),
                f"正在估算图像空间占用（{item_index + 1} / {len(image_items)}）…",
            )
        kind_order = {"figure": 0, "image": 1, "vector": 2}
        assets.sort(
            key=lambda asset: (
                asset.page_numbers[0],
                kind_order.get(asset.kind, 9),
                asset.key,
            )
        )
        _notify(
            progress_callback,
            100,
            f"分析完成，找到 {len(assets)} 个可处理图形",
        )
        return assets, document.page_count


_RESULTS_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*)?\s*"
    r"(?:results?(?:\s+and\s+discussion)?|experimental\s+results?|"
    r"numerical\s+results?|experiments?\s+and\s+results?|"
    r"结果(?:与讨论)?|实验结果|数值结果)\s*$",
    re.IGNORECASE,
)
_RESULTS_END_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*)?\s*"
    r"(?:conclusions?|references?|acknowledg(?:e)?ments?|"
    r"结论|总结|参考文献|致谢)\s*$",
    re.IGNORECASE,
)


def _detect_results_pages(source: fitz.Document) -> set[int]:
    """Detect the page range belonging to a Results section.

    Detection intentionally requires a standalone section-heading line. Words
    such as "results" inside an abstract or paragraph therefore do not match.
    The returned page indexes are zero-based.
    """
    start_page: int | None = None
    end_page = source.page_count
    for page_number in range(source.page_count):
        lines = [line.strip() for line in source[page_number].get_text().splitlines()]
        if start_page is None:
            if any(_RESULTS_HEADING.fullmatch(line) for line in lines if line):
                start_page = page_number
        elif any(_RESULTS_END_HEADING.fullmatch(line) for line in lines if line):
            end_page = page_number
            break
    if start_page is None:
        return set()
    return set(range(start_page, end_page))


def detect_results_pages(input_path: str | Path) -> tuple[set[int], int]:
    """Return automatically detected result pages and total page count."""
    with fitz.open(Path(input_path)) as document:
        return _detect_results_pages(document), document.page_count


def parse_page_ranges(value: str, page_count: int) -> set[int]:
    """Parse user-facing one-based ranges like ``5-9, 12`` to page indexes."""
    value = value.strip().replace("，", ",").replace("—", "-").replace("–", "-")
    if not value:
        return set()
    pages: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            pieces = [piece.strip() for piece in part.split("-", 1)]
            if not all(piece.isdigit() for piece in pieces):
                raise ValueError("页码范围格式应类似：5-9, 12")
            first, last = map(int, pieces)
        else:
            if not part.isdigit():
                raise ValueError("页码范围格式应类似：5-9, 12")
            first = last = int(part)
        if first < 1 or last < first or last > page_count:
            raise ValueError(f"页码必须在 1 到 {page_count} 之间。")
        pages.update(range(first - 1, last))
    return pages


def format_page_ranges(pages: set[int]) -> str:
    """Format zero-based page indexes as compact one-based ranges."""
    if not pages:
        return ""
    ordered = sorted(pages)
    ranges: list[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(
            str(start + 1)
            if start == previous
            else f"{start + 1}-{previous + 1}"
        )
        start = previous = page
    ranges.append(
        str(start + 1) if start == previous else f"{start + 1}-{previous + 1}"
    )
    return ", ".join(ranges)


def _image_pixmap(
    document: fitz.Document, xref: int, smask: int
) -> fitz.Pixmap:
    """Load an image and combine its soft mask so transparency is preserved."""
    base = fitz.Pixmap(document, xref)
    # Some producers expose an image that already contains alpha *and* report
    # a separate /SMask. MuPDF rejects applying a second mask with
    # "color pixmap must not have an alpha channel". In that case the pixmap
    # is already complete and the extra mask must be ignored.
    if smask > 0 and not base.alpha:
        # If this mask is malformed, let the per-image boundary skip this image
        # entirely. Recompressing only the color channels would lose transparency.
        mask = fitz.Pixmap(document, smask)
        combined = fitz.Pixmap(base, mask)
        mask = None
        base = combined

    if base.colorspace and base.colorspace.n not in (1, 3):
        base = fitz.Pixmap(fitz.csRGB, base)
    return base


def _pixmap_to_image(
    pixmap: fitz.Pixmap, *, opaque: bool = True
) -> Image.Image:
    """Decode a MuPDF pixmap directly from its native sample buffer.

    Older render paths encoded the pixmap as PNG and immediately asked Pillow
    to decode that PNG again. Direct sample decoding removes both operations
    while producing the same RGB / white-composited pixels.
    """
    if pixmap.colorspace and pixmap.colorspace.n not in (1, 3):
        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)

    if pixmap.alpha:
        mode = "LA" if pixmap.n == 2 else "RGBA"
        # Lowercase ``a`` is Pillow's premultiplied-alpha mode. Converting it
        # to straight alpha is implemented in native code and preserves the
        # transparent edge colours that MuPDF's PNG encoder exposed before.
        source_mode = "La" if pixmap.n == 2 else "RGBa"
    else:
        mode = "L" if pixmap.n == 1 else "RGB"
        source_mode = mode
    image = Image.frombytes(
        source_mode,
        (pixmap.width, pixmap.height),
        pixmap.samples,
        "raw",
        source_mode,
        pixmap.stride,
        1,
    )
    if source_mode != mode:
        image = image.convert(mode)
    if pixmap.alpha and opaque:
        foreground = image.convert("RGBA")
        white = Image.new("RGBA", foreground.size, "white")
        return Image.alpha_composite(white, foreground).convert("RGB")
    if opaque and image.mode != "RGB":
        return image.convert("RGB")
    return image


def _render_vector_layer(
    document: fitz.Document,
    page_number: int,
    dpi: int,
    jpeg_quality: int = 85,
) -> tuple[bytes, fitz.Rect] | None:
    """Render only one page's vector paths to a cropped opaque JPEG.

    JPEG has no alpha channel and cannot create a PDF soft mask. This avoids the
    black backgrounds some readers show for transparent or ICC-based PNGs. The
    layer is inserted behind untouched text and bitmap objects, so its white
    backing cannot cover them.
    """
    layer_document = fitz.open()
    try:
        layer_document.insert_pdf(
            document,
            from_page=page_number,
            to_page=page_number,
            links=False,
            annots=False,
        )
        layer_page = layer_document[0]
        layer_page.add_redact_annot(layer_page.rect, fill=None, cross_out=False)
        layer_page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_REMOVE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )

        scale = dpi / 72.0
        pixmap = layer_page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        )
        image = _pixmap_to_image(pixmap)
        white_background = Image.new("RGB", image.size, "white")
        content_box = ImageChops.difference(image, white_background).getbbox()
        if content_box is None:
            return None

        # Retain antialiased edge pixels around the detected drawing bounds.
        padding = 2
        content_box = (
            max(0, content_box[0] - padding),
            max(0, content_box[1] - padding),
            min(image.width, content_box[2] + padding),
            min(image.height, content_box[3] + padding),
        )

        cropped = image.crop(content_box)
        buffer = BytesIO()
        cropped.save(
            buffer,
            format="JPEG",
            quality=max(35, min(100, jpeg_quality)),
            subsampling=2,
            optimize=True,
        )
        page_rect = document[page_number].rect
        target_rect = fitz.Rect(
            page_rect.x0 + content_box[0] / scale,
            page_rect.y0 + content_box[1] / scale,
            page_rect.x0 + content_box[2] / scale,
            page_rect.y0 + content_box[3] / scale,
        )
        return buffer.getvalue(), target_rect
    finally:
        layer_document.close()


def _render_figure_region(
    document: fitz.Document,
    page_number: int,
    rectangle: tuple[float, float, float, float] | fitz.Rect,
    dpi: int,
    jpeg_quality: int = 85,
) -> tuple[bytes, fitz.Rect]:
    """Render one complete Figure region to an opaque white-backed JPEG."""
    page = document[page_number]
    clip = fitz.Rect(rectangle) & page.rect
    if clip.is_empty or clip.width < 1 or clip.height < 1:
        raise CompressionError("论文 Figure 的识别区域无效。")
    scale = max(24, dpi) / 72.0
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        clip=clip,
        colorspace=fitz.csRGB,
        alpha=False,
        annots=False,
    )
    image = _pixmap_to_image(pixmap)
    buffer = BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=max(35, min(100, jpeg_quality)),
        # PowerPoint's PDF export uses high-resolution 4:2:0 JPEGs. This spends
        # far more of the file budget on glyph pixels while keeping color data
        # compact, which is markedly clearer for scientific plots.
        subsampling=2,
        optimize=True,
    )
    return buffer.getvalue(), clip


def render_asset_image_from_document(
    document: fitz.Document,
    asset: PDFAsset,
    dpi: int = 220,
) -> bytes:
    """Render one selectable asset using an already-open document."""
    if asset.kind == "image":
        if asset.xref is None:
            raise CompressionError("图片对象缺少 xref。")
        pixmap = _image_pixmap(document, asset.xref, asset.smask)
        preview = _pixmap_to_image(pixmap)
    elif asset.kind == "figure":
        if asset.rect is None:
            raise CompressionError("论文 Figure 缺少识别区域。")
        page = document[asset.page_numbers[0]]
        clip = fitz.Rect(asset.rect) & page.rect
        if clip.is_empty:
            raise CompressionError("论文 Figure 的识别区域无效。")
        scale = max(72, min(300, dpi)) / 72.0
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            clip=clip,
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        )
        preview = _pixmap_to_image(pixmap)
    else:
        rendered = _render_vector_layer(
            document, asset.page_numbers[0], dpi=dpi, jpeg_quality=90
        )
        if rendered is None:
            raise CompressionError("此矢量绘图层没有可预览内容。")
        preview = Image.open(BytesIO(rendered[0])).convert("RGB")

    buffer = BytesIO()
    preview.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_asset_image(
    input_path: str | Path,
    asset: PDFAsset,
    dpi: int = 220,
) -> bytes:
    """Render one complete selectable asset as a full-resolution PNG."""
    with fitz.open(Path(input_path)) as document:
        return render_asset_image_from_document(document, asset, dpi)


def render_asset_thumbnail_from_document(
    document: fitz.Document,
    asset: PDFAsset,
    size: tuple[int, int] = (210, 145),
) -> bytes:
    """Render a thumbnail while reusing an already-open PDF document.

    The main-grid preview is deliberately decoded directly from MuPDF pixels.
    Routing it through ``render_asset_image`` used to encode a large PNG only
    to decode and shrink it immediately, causing visible event-loop stalls on
    PDFs with dozens of Figures.
    """
    if asset.kind == "image":
        if asset.xref is None:
            raise CompressionError("图片对象缺少 xref。")
        pixmap = _image_pixmap(document, asset.xref, asset.smask)
        preview = _pixmap_to_image(pixmap)
    elif asset.kind == "figure":
        if asset.rect is None:
            raise CompressionError("论文 Figure 缺少识别区域。")
        page = document[asset.page_numbers[0]]
        clip = fitz.Rect(asset.rect) & page.rect
        if clip.is_empty:
            raise CompressionError("论文 Figure 的识别区域无效。")
        fit_scale = min(size[0] / clip.width, size[1] / clip.height)
        # One native PDF pixel per point is already around 2.5x the card
        # resolution for a typical paper Figure and gives clean downsampling.
        scale = max(1.0, min(1.7, fit_scale * 1.35))
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            clip=clip,
            colorspace=fitz.csRGB,
            alpha=False,
            annots=False,
        )
        preview = _pixmap_to_image(pixmap)
    else:
        rendered = _render_vector_layer(
            document, asset.page_numbers[0], dpi=90, jpeg_quality=88
        )
        if rendered is None:
            raise CompressionError("此矢量绘图层没有可预览内容。")
        preview = Image.open(BytesIO(rendered[0])).convert("RGB")

    preview.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "white")
    x = (size[0] - preview.width) // 2
    y = (size[1] - preview.height) // 2
    canvas.paste(preview, (x, y))
    buffer = BytesIO()
    canvas.save(
        buffer,
        format="JPEG",
        quality=88,
        subsampling=1,
        optimize=False,
    )
    return buffer.getvalue()


def render_asset_thumbnail(
    input_path: str | Path,
    asset: PDFAsset,
    size: tuple[int, int] = (210, 145),
) -> bytes:
    """Render a fast, aspect-fitted overview for one selectable asset."""
    with fitz.open(Path(input_path)) as document:
        return render_asset_thumbnail_from_document(document, asset, size)


def iter_asset_thumbnails(
    input_path: str | Path,
    indexed_assets: list[tuple[int, PDFAsset]],
    size: tuple[int, int] = (210, 145),
    cancel_event: threading.Event | None = None,
):
    """Yield thumbnail results while one worker keeps one PDF open.

    The function intentionally returns errors per asset so one malformed image
    cannot stop the remaining cards in that worker's partition.
    """
    with fitz.open(Path(input_path)) as document:
        for index, asset in indexed_assets:
            if cancel_event is not None and cancel_event.is_set():
                return
            try:
                data = render_asset_thumbnail_from_document(
                    document, asset, size
                )
            except Exception as exc:
                yield index, None, str(exc)
            else:
                yield index, data, None


def _replace_vector_layer(
    document: fitz.Document,
    page_number: int,
    dpi: int,
    jpeg_quality: int = 85,
    render_cache: RenderCache | None = None,
) -> bool:
    """Replace one page's vector paths while keeping text and images intact."""
    cache_key = ("vector", page_number, dpi, jpeg_quality)
    rendered = render_cache.get(cache_key) if render_cache else None
    if rendered is None:
        rendered = _render_vector_layer(
            document, page_number, dpi, jpeg_quality
        )
        if rendered is not None and render_cache is not None:
            render_cache.put(cache_key, rendered)
    if rendered is None:
        return False
    image_bytes, target_rect = rendered

    page = document[page_number]
    page.add_redact_annot(page.rect, fill=None, cross_out=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
        text=fitz.PDF_REDACT_TEXT_NONE,
    )
    page.insert_image(target_rect, stream=image_bytes, overlay=False)
    return True


def _replace_figure_regions(
    document: fitz.Document,
    page_number: int,
    rectangles: list[tuple[float, float, float, float]],
    dpi: int,
    jpeg_quality: int = 85,
    dpi_overrides: list[int] | None = None,
    render_cache: RenderCache | None = None,
    cache_variant: int = 0,
) -> int:
    """Compress complete Figures while preserving all native Figure text.

    The visual line art and bitmap layer is rendered to an opaque JPEG, but
    text is removed from that render and retained in the real PDF content. The
    JPEG is inserted behind it. Small axis labels and legends therefore stay
    perfectly sharp and searchable, while the no-alpha white backing continues
    to prevent black-background rendering bugs.
    """
    if not rectangles:
        return 0

    render_dpis = (
        dpi_overrides
        if dpi_overrides is not None
        and len(dpi_overrides) == len(rectangles)
        else [dpi] * len(rectangles)
    )
    rendered: list[tuple[bytes, fitz.Rect] | None] = [None] * len(rectangles)
    missing: list[tuple[int, tuple[object, ...]]] = []
    for index, (rectangle, render_dpi) in enumerate(
        zip(rectangles, render_dpis)
    ):
        rectangle_key = tuple(round(float(value), 3) for value in rectangle)
        cache_key = (
            "figure",
            page_number,
            rectangle_key,
            render_dpi,
            jpeg_quality,
            cache_variant,
        )
        cached = render_cache.get(cache_key) if render_cache else None
        if cached is None:
            missing.append((index, cache_key))
        else:
            rendered[index] = cached

    if missing:
        # Build a temporary copy containing Figure graphics but no text only
        # when at least one encoded region is absent from the cross-candidate
        # cache. Native text remains sharp and searchable in the output PDF.
        graphics_document = fitz.open()
        try:
            graphics_document.insert_pdf(
                document,
                from_page=page_number,
                to_page=page_number,
                links=False,
                annots=False,
            )
            graphics_page = graphics_document[0]
            for rectangle in rectangles:
                graphics_page.add_redact_annot(
                    fitz.Rect(rectangle), fill=None, cross_out=False
                )
            graphics_page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
            for index, cache_key in missing:
                item = _render_figure_region(
                    graphics_document,
                    0,
                    rectangles[index],
                    render_dpis[index],
                    jpeg_quality,
                )
                rendered[index] = item
                if render_cache is not None:
                    render_cache.put(cache_key, item)
        finally:
            graphics_document.close()

    return _replace_figure_payloads(
        document,
        page_number,
        [item for item in rendered if item is not None],
    )


def _replace_figure_payloads(
    document: fitz.Document,
    page_number: int,
    payloads: list[tuple[bytes, fitz.Rect]],
) -> int:
    """Install pre-rendered Figure payloads with one redaction pass."""
    if not payloads:
        return 0
    page = document[page_number]
    for _image_bytes, rectangle in payloads:
        page.add_redact_annot(rectangle, fill=None, cross_out=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_REMOVE,
        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
        text=fitz.PDF_REDACT_TEXT_NONE,
    )
    for image_bytes, rectangle in payloads:
        page.insert_image(rectangle, stream=image_bytes, overlay=False)
    return len(payloads)


def _prefetch_native_figure_renders(
    source_path: Path,
    figure_regions: dict[int, list[tuple[float, float, float, float]]],
    vector_dpi: int,
    jpeg_quality: int,
    boosted_figure_regions: set[tuple[int, int]],
    excluded_pages: set[int],
    render_cache: RenderCache,
    work_directory: Path,
    cancel_event: threading.Event | None,
    native_stats: dict[str, float | int] | None,
    native_session: NativeWorkerSession | None,
    progress_callback: Callable[[int, int], None] | None,
) -> bool:
    """Fill the render cache concurrently with the optional C++ worker.

    Standalone image xrefs are changed in the in-memory candidate before its
    Figures are replaced. A page containing one of those images stays on the
    Python path so the rendered Figure can never observe stale source pixels.
    """
    if native_session is None or (
        native_stats is not None and native_stats.get("disabled")
    ):
        return False

    requests: list[NativeRenderRequest] = []
    request_keys: dict[
        int, tuple[tuple[object, ...], tuple[float, float, float, float]]
    ] = {}
    request_id = 0
    for page_number, rectangles in sorted(figure_regions.items()):
        if page_number in excluded_pages:
            continue
        for region_number, rectangle in enumerate(rectangles):
            render_dpi = vector_dpi + int(
                (page_number, region_number) in boosted_figure_regions
            )
            rectangle_key = tuple(round(float(value), 3) for value in rectangle)
            cache_key = (
                "figure",
                page_number,
                rectangle_key,
                render_dpi,
                jpeg_quality,
                0,
            )
            cached = render_cache.get(cache_key)
            if cached is not None:
                continue
            request = NativeRenderRequest(
                request_id,
                page_number,
                tuple(map(float, rectangle)),
                render_dpi,
                jpeg_quality,
            )
            requests.append(request)
            request_keys[request_id] = (cache_key, tuple(map(float, rectangle)))
            request_id += 1

    if not requests:
        return False

    started = time.perf_counter()
    try:
        rendered = native_session.render(
            requests,
            work_directory,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
    except NativeWorkerCancelled as exc:
        raise CompressionCancelled("压缩已取消。") from exc
    except NativeWorkerError:
        # A missing or incompatible native binary must never make compression
        # unavailable. The existing, fully tested Python/MuPDF path follows.
        native_session.close(force=True)
        if native_stats is not None:
            native_stats["disabled"] = 1
        return False

    elapsed = time.perf_counter() - started
    for item_id, image_bytes in rendered.items():
        cache_key, rectangle = request_keys[item_id]
        render_cache.put(cache_key, (image_bytes, fitz.Rect(rectangle)))
    if native_stats is not None:
        native_stats["batches"] = int(native_stats.get("batches", 0)) + 1
        native_stats["tasks"] = int(native_stats.get("tasks", 0)) + len(requests)
        native_stats["seconds"] = float(native_stats.get("seconds", 0.0)) + elapsed
    return True


def _prepare_planned_image_assets(
    source_path: Path,
    image_locations: dict[int, tuple[int, int]],
    profiles: tuple[tuple[int, float, int, int], ...],
    cancel_event: threading.Event | None,
) -> list[PlannedAsset]:
    """Encode a complete rate-distortion ladder for standalone images."""
    assets: list[PlannedAsset] = []
    with fitz.open(source_path) as document:
        for xref, (page_number, smask) in sorted(image_locations.items()):
            _check_cancel(cancel_event)
            try:
                base = _image_pixmap(document, xref, smask)
                if (
                    base.colorspace is None
                    or base.width < 2
                    or base.height < 2
                ):
                    raise PlannerUnavailable(
                        f"Image xref {xref} has no usable pixel data."
                    )
                variants = []
                for score, image_scale, jpeg_quality, dpi in profiles:
                    width = max(1, round(base.width * image_scale))
                    height = max(1, round(base.height * image_scale))
                    pixmap = (
                        base
                        if width == base.width and height == base.height
                        else fitz.Pixmap(base, width, height)
                    )
                    payload = (
                        pixmap.tobytes("png")
                        if pixmap.alpha
                        else pixmap.tobytes(
                            "jpg", jpg_quality=jpeg_quality
                        )
                    )
                    variants.append(
                        PlannedVariant(
                            score,
                            image_scale,
                            jpeg_quality,
                            dpi,
                            payload,
                        )
                    )
            except PlannerUnavailable:
                raise
            except Exception as exc:
                raise PlannerUnavailable(
                    f"Image xref {xref} could not be planned."
                ) from exc
            assets.append(
                PlannedAsset(
                    ("image", xref),
                    "image",
                    page_number,
                    tuple(variants),
                    xref=xref,
                    visual_weight=max(1.0, float(base.width * base.height)),
                )
            )
    return assets


def _image_only_might_reach_target(
    source_path: Path,
    image_locations: dict[int, tuple[int, int]],
    lossless_size: int,
    target_bytes: int,
) -> bool:
    """Return False only when even deleting selected streams cannot suffice."""
    if not image_locations:
        return False
    try:
        removable_xrefs = set(image_locations)
        removable_xrefs.update(
            smask for _page, smask in image_locations.values() if smask > 0
        )
        with fitz.open(source_path) as document:
            removable_bytes = sum(
                len(document.xref_stream_raw(xref) or b"")
                for xref in removable_xrefs
            )
    except Exception:
        # An uncertain estimate must preserve the conservative image-only try.
        return True
    return lossless_size - removable_bytes <= target_bytes


def _prepare_planned_figure_assets(
    native_session: NativeWorkerSession,
    figure_regions: dict[int, list[tuple[float, float, float, float]]],
    profiles: tuple[tuple[int, float, int, int], ...],
    work_directory: Path,
    cancel_event: threading.Event | None,
    progress_callback: ProgressCallback | None,
    native_stats: dict[str, float | int],
) -> list[PlannedAsset]:
    """Render one master per Figure and encode its complete quality ladder."""
    requests: list[NativeRenderRequest] = []
    request_map: dict[int, tuple[int, int]] = {}
    descriptors: list[
        tuple[int, int, tuple[float, float, float, float]]
    ] = []
    request_id = 0
    group_id = 0
    for page_number, rectangles in sorted(figure_regions.items()):
        for region_number, rectangle in enumerate(rectangles):
            normalized = tuple(map(float, rectangle))
            descriptors.append((page_number, region_number, normalized))
            for variant_number, (
                _score,
                _image_scale,
                jpeg_quality,
                dpi,
            ) in enumerate(profiles):
                requests.append(
                    NativeRenderRequest(
                        request_id,
                        page_number,
                        normalized,
                        dpi,
                        jpeg_quality,
                        group_id,
                    )
                )
                request_map[request_id] = (group_id, variant_number)
                request_id += 1
            group_id += 1

    if not requests:
        return []

    def native_progress(completed: int, total: int) -> None:
        _notify(
            progress_callback,
            12 + round(33 * completed / max(1, total)),
            f"正在建立 Figure 质量曲线：{completed}/{total}",
        )

    started = time.perf_counter()
    try:
        rendered = native_session.render_ladder(
            requests,
            work_directory,
            cancel_event=cancel_event,
            progress_callback=native_progress,
        )
    except NativeWorkerCancelled as exc:
        raise CompressionCancelled("压缩已取消。") from exc
    except NativeWorkerError as exc:
        raise PlannerUnavailable("Native Figure planning failed.") from exc
    elapsed = time.perf_counter() - started
    if len(rendered) != len(requests):
        raise PlannerUnavailable("Native Figure ladder is incomplete.")

    grouped: list[list[bytes | None]] = [
        [None] * len(profiles) for _descriptor in descriptors
    ]
    for item_id, payload in rendered.items():
        asset_number, variant_number = request_map[item_id]
        grouped[asset_number][variant_number] = payload

    assets: list[PlannedAsset] = []
    for asset_number, (page_number, region_number, rectangle) in enumerate(
        descriptors
    ):
        payloads = grouped[asset_number]
        if any(payload is None for payload in payloads):
            raise PlannerUnavailable("Native Figure ladder is incomplete.")
        variants = tuple(
            PlannedVariant(
                score,
                image_scale,
                jpeg_quality,
                dpi,
                payload if payload is not None else b"",
            )
            for (score, image_scale, jpeg_quality, dpi), payload in zip(
                profiles, payloads
            )
        )
        width = max(0.0, rectangle[2] - rectangle[0])
        height = max(0.0, rectangle[3] - rectangle[1])
        assets.append(
            PlannedAsset(
                ("figure", page_number, region_number),
                "figure",
                page_number,
                variants,
                rectangle=rectangle,
                visual_weight=max(1.0, width * height),
            )
        )

    response = native_session.last_response
    native_stats["batches"] = int(native_stats.get("batches", 0)) + 1
    native_stats["tasks"] = int(native_stats.get("tasks", 0)) + len(requests)
    native_stats["seconds"] = float(native_stats.get("seconds", 0.0)) + elapsed
    native_stats["master_renders"] = int(
        native_stats.get("master_renders", 0)
    ) + int(response.get("master_renders", len(descriptors)))
    native_stats["variants"] = int(native_stats.get("variants", 0)) + len(
        requests
    )
    return assets


def _selection_stream_bytes(
    assets: list[PlannedAsset], selection: tuple[int, ...]
) -> int:
    return sum(
        len(asset.variants[index].payload)
        for asset, index in zip(assets, selection)
    )


def _selection_utility(
    assets: list[PlannedAsset], selection: tuple[int, ...]
) -> float:
    return sum(
        asset.variants[index].score * asset.visual_weight
        for asset, index in zip(assets, selection)
    )


def _plan_variant_selection(
    assets: list[PlannedAsset], stream_budget: int
) -> tuple[int, ...]:
    """Solve a quality-fair multiple-choice byte allocation problem."""
    if not assets:
        return ()
    common_levels = min(len(asset.variants) for asset in assets)
    floor = 0
    for level in range(1, common_levels):
        level_bytes = sum(
            len(asset.variants[level].payload) for asset in assets
        )
        if level_bytes <= stream_budget:
            floor = level
        else:
            break

    base_bytes = sum(
        len(asset.variants[floor].payload) for asset in assets
    )
    extra_budget = max(0, stream_budget - base_bytes)
    if not extra_budget:
        return tuple(floor for _asset in assets)

    # Cap the dynamic-programming table while conservatively rounding every
    # payload increase upward. This guarantees the selected raw stream bytes
    # remain inside the measured PDF budget.
    quantum = max(1, (extra_budget + 24_999) // 25_000)
    capacity = extra_budget // quantum
    unavailable = float("-inf")
    previous = [unavailable] * (capacity + 1)
    previous[0] = 0.0
    parents: list[list[int]] = []
    choices: list[list[int]] = []

    weights = sorted(asset.visual_weight for asset in assets)
    median_weight = max(1.0, weights[len(weights) // 2])
    for asset in assets:
        next_scores = [unavailable] * (capacity + 1)
        parent_row = [-1] * (capacity + 1)
        choice_row = [-1] * (capacity + 1)
        base_variant = asset.variants[floor]
        area_weight = max(
            0.5,
            min(2.0, (asset.visual_weight / median_weight) ** 0.5),
        )
        options = []
        for index in range(floor, len(asset.variants)):
            variant = asset.variants[index]
            added_bytes = max(
                0, len(variant.payload) - len(base_variant.payload)
            )
            units = (added_bytes + quantum - 1) // quantum
            # Diminishing returns keep small Figures from being starved while
            # still assigning more bits to visually larger regions.
            utility = area_weight * (max(0, variant.score - base_variant.score) ** 0.5)
            options.append((units, utility, index))
        for used, previous_score in enumerate(previous):
            if previous_score == unavailable:
                continue
            for units, utility, index in options:
                total = used + units
                if total > capacity:
                    continue
                score = previous_score + utility
                if score > next_scores[total]:
                    next_scores[total] = score
                    parent_row[total] = used
                    choice_row[total] = index
        previous = next_scores
        parents.append(parent_row)
        choices.append(choice_row)

    final_cost = max(
        range(capacity + 1),
        key=lambda value: (previous[value], value),
    )
    selection = [floor] * len(assets)
    cursor = final_cost
    for asset_number in range(len(assets) - 1, -1, -1):
        choice = choices[asset_number][cursor]
        parent = parents[asset_number][cursor]
        if choice < 0 or parent < 0:
            return tuple(floor for _asset in assets)
        selection[asset_number] = choice
        cursor = parent
    return tuple(selection)


def _assemble_planned_candidate(
    source_path: Path,
    destination: Path,
    assets: list[PlannedAsset],
    selection: tuple[int, ...],
    cancel_event: threading.Event | None,
) -> tuple[int, int]:
    """Apply a selected payload set and perform one complete PDF save."""
    output = fitz.open(source_path)
    images_processed = 0
    figures_processed = 0
    try:
        figure_payloads: dict[int, list[tuple[bytes, fitz.Rect]]] = {}
        for asset, variant_number in zip(assets, selection):
            _check_cancel(cancel_event)
            variant = asset.variants[variant_number]
            if asset.kind == "image":
                if asset.xref is None:
                    raise PlannerUnavailable("A planned image has no xref.")
                output[asset.page_number].replace_image(
                    asset.xref, stream=variant.payload
                )
                images_processed += 1
            elif asset.kind == "figure":
                if asset.rectangle is None:
                    raise PlannerUnavailable("A planned Figure has no region.")
                figure_payloads.setdefault(asset.page_number, []).append(
                    (variant.payload, fitz.Rect(asset.rectangle))
                )
        for page_number, payloads in sorted(figure_payloads.items()):
            _check_cancel(cancel_event)
            figures_processed += _replace_figure_payloads(
                output, page_number, payloads
            )
        output.save(
            destination,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
        )
    except CompressionCancelled:
        raise
    except PlannerUnavailable:
        raise
    except Exception as exc:
        raise PlannerUnavailable("Planned PDF assembly failed.") from exc
    finally:
        output.close()
    return images_processed, figures_processed


def _run_planned_stage(
    source_path: Path,
    temp_dir: Path,
    stage_name: str,
    assets: list[PlannedAsset],
    target_bytes: int,
    attempt_offset: int,
    progress_callback: ProgressCallback | None,
    cancel_event: threading.Event | None,
) -> tuple[PlannedStageResult | None, int, int]:
    """Calibrate PDF overhead, plan variants, and assemble at most four times."""
    attempts = 0
    best: PlannedStageResult | None = None
    seen: set[tuple[int, ...]] = set()

    def evaluate(selection: tuple[int, ...]) -> PlannedStageResult:
        nonlocal attempts
        attempts += 1
        _notify(
            progress_callback,
            min(94, 48 + (attempt_offset + attempts) * 8),
            f"正在装配计划结果（第 {attempt_offset + attempts} 次）…",
        )
        candidate = temp_dir / (
            f"planned_{stage_name}_{attempt_offset + attempts:02d}.pdf"
        )
        images, figures = _assemble_planned_candidate(
            source_path, candidate, assets, selection, cancel_event
        )
        return PlannedStageResult(
            candidate,
            candidate.stat().st_size,
            selection,
            attempts,
            images,
            figures,
        )

    low_selection = tuple(0 for _asset in assets)
    current = evaluate(low_selection)
    minimum_size = current.size
    seen.add(low_selection)
    if current.size > target_bytes:
        return None, minimum_size, attempts
    best = current

    safety_margin = max(512, target_bytes // 20_000)
    for _correction in range(4):
        _check_cancel(cancel_event)
        current_stream_bytes = _selection_stream_bytes(
            assets, current.selection
        )
        measured_overhead = current.size - current_stream_bytes
        planned = _plan_variant_selection(
            assets,
            target_bytes - measured_overhead - safety_margin,
        )
        if planned in seen:
            break
        seen.add(planned)
        current = evaluate(planned)
        if current.size <= target_bytes and (
            best is None
            or _selection_utility(assets, current.selection)
            > _selection_utility(assets, best.selection)
            or (
                _selection_utility(assets, current.selection)
                == _selection_utility(assets, best.selection)
                and current.size > best.size
            )
        ):
            best = current

    if best is None:
        return None, minimum_size, attempts
    return (
        PlannedStageResult(
            best.path,
            best.size,
            best.selection,
            attempts,
            best.images_processed,
            best.figures_processed,
        ),
        minimum_size,
        attempts,
    )


def _try_planned_compression(
    source_path: Path,
    destination: Path,
    original_bytes: int,
    lossless_size: int,
    target_bytes: int,
    image_locations: dict[int, tuple[int, int]],
    vector_pages: list[int],
    figure_regions: dict[int, list[tuple[float, float, float, float]]],
    temp_dir: Path,
    progress_callback: ProgressCallback | None,
    cancel_event: threading.Event | None,
    native_session: NativeWorkerSession | None,
    native_stats: dict[str, float | int],
) -> CompressionResult | None:
    """Run the one-shot planner when every selected vector is a Figure."""
    if native_session is None or vector_pages or not figure_regions:
        return None

    _notify(progress_callback, 6, "正在建立一次性压缩计划…")
    profiles = _planner_profiles()
    image_assets = _prepare_planned_image_assets(
        source_path, image_locations, profiles, cancel_event
    )
    attempts = 0
    minimum_sizes: list[int] = []

    if image_assets and _image_only_might_reach_target(
        source_path,
        image_locations,
        lossless_size,
        target_bytes,
    ):
        image_result, minimum_size, used = _run_planned_stage(
            source_path,
            temp_dir,
            "images",
            image_assets,
            target_bytes,
            attempts,
            progress_callback,
            cancel_event,
        )
        attempts += used
        minimum_sizes.append(minimum_size)
        if image_result is not None:
            selected_assets = image_assets
            selected_result = image_result
        else:
            selected_assets = []
            selected_result = None
    else:
        selected_assets = []
        selected_result = None

    if selected_result is None:
        figure_assets = _prepare_planned_figure_assets(
            native_session,
            figure_regions,
            profiles,
            temp_dir / "planned-native-ladder",
            cancel_event,
            progress_callback,
            native_stats,
        )
        expected_figures = sum(
            len(rectangles) for rectangles in figure_regions.values()
        )
        if len(figure_assets) != expected_figures:
            raise PlannerUnavailable("Not every Figure received a ladder.")
        selected_assets = image_assets + figure_assets
        selected_result, minimum_size, used = _run_planned_stage(
            source_path,
            temp_dir,
            "combined",
            selected_assets,
            target_bytes,
            attempts,
            progress_callback,
            cancel_event,
        )
        attempts += used
        minimum_sizes.append(minimum_size)

    if selected_result is None:
        raise TargetTooSmallError(target_bytes, min(minimum_sizes))

    _check_cancel(cancel_event)
    _notify(progress_callback, 96, "正在写入一次性规划结果…")
    _atomic_install(selected_result.path, destination)
    final_size = destination.stat().st_size
    if final_size > target_bytes:
        raise PlannerUnavailable("Planned result exceeded the target.")

    chosen = [
        asset.variants[index]
        for asset, index in zip(selected_assets, selected_result.selection)
    ]
    image_choices = [
        variant
        for asset, variant in zip(selected_assets, chosen)
        if asset.kind == "image"
    ]
    figure_choices = [
        variant
        for asset, variant in zip(selected_assets, chosen)
        if asset.kind == "figure"
    ]
    _notify(progress_callback, 100, "压缩完成。")
    return CompressionResult(
        source_path,
        destination,
        original_bytes,
        final_size,
        target_bytes,
        (
            "images_vectors"
            if selected_result.images_processed
            and selected_result.figures_processed
            else "vectors"
            if selected_result.figures_processed
            else "images"
        ),
        max((variant.image_scale for variant in image_choices), default=None),
        max((variant.jpeg_quality for variant in chosen), default=None),
        selected_result.images_processed,
        0,
        selected_result.figures_processed,
        max((variant.dpi for variant in figure_choices), default=None),
        attempts,
        0,
        0,
        bool(native_stats.get("batches")),
        int(native_stats.get("batches", 0)),
        int(native_stats.get("tasks", 0)),
        float(native_stats.get("seconds", 0.0)),
        True,
        int(native_stats.get("master_renders", 0)),
        int(native_stats.get("variants", 0)),
    )


def _compress_candidate(
    source_path: Path,
    destination: Path,
    image_locations: dict[int, tuple[int, int]],
    vector_pages: list[int],
    figure_regions: dict[int, list[tuple[float, float, float, float]]],
    include_vectors: bool,
    image_scale: float,
    jpeg_quality: int,
    vector_dpi: int,
    attempt: int,
    max_attempts: int,
    progress_callback: ProgressCallback | None,
    cancel_event: threading.Event | None,
    figure_dpi_boost_count: int = 0,
    render_cache: RenderCache | None = None,
    native_stats: dict[str, float | int] | None = None,
    native_session: NativeWorkerSession | None = None,
) -> tuple[int, int, int]:
    """Create a candidate by replacing bitmap images and optionally vector paths."""
    output = fitz.open(source_path)
    processed_images = 0
    processed_vector_pages = 0
    processed_figures = 0
    try:
        image_count = len(image_locations)
        vector_count = len(vector_pages) if include_vectors else 0
        figure_count = (
            sum(len(rectangles) for rectangles in figure_regions.values())
            if include_vectors
            else 0
        )
        figure_entries = sorted(
            (
                (
                    max(
                        0.0,
                        (rectangle[2] - rectangle[0])
                        * (rectangle[3] - rectangle[1]),
                    ),
                    page_number,
                    region_number,
                )
                for page_number, rectangles in figure_regions.items()
                for region_number, rectangle in enumerate(rectangles)
            ),
            key=lambda item: (item[0], item[1], item[2]),
        )
        boosted_figure_regions = {
            (page_number, region_number)
            for _area, page_number, region_number in figure_entries[
                : max(0, figure_dpi_boost_count)
            ]
        }
        work_count = max(1, image_count + vector_count + figure_count)
        for image_number, (xref, (page_number, smask)) in enumerate(
            image_locations.items(), start=1
        ):
            _check_cancel(cancel_event)
            try:
                pixmap = _image_pixmap(output, xref, smask)
                if pixmap.colorspace is None or pixmap.width < 2 or pixmap.height < 2:
                    continue

                width = max(1, round(pixmap.width * image_scale))
                height = max(1, round(pixmap.height * image_scale))
                if width != pixmap.width or height != pixmap.height:
                    pixmap = fitz.Pixmap(pixmap, width, height)

                # PNG is used only when transparency must be retained. Opaque
                # color and grayscale images use lossy JPEG compression.
                if pixmap.alpha:
                    image_bytes = pixmap.tobytes("png")
                else:
                    image_bytes = pixmap.tobytes(
                        "jpg", jpg_quality=jpeg_quality
                    )
                output[page_number].replace_image(xref, stream=image_bytes)
                processed_images += 1
            except Exception:
                # Leave unusual or unsupported image objects untouched.
                pass

            overall = 5 + round(
                88 * ((attempt - 1) + image_number / work_count) / max_attempts
            )
            _notify(
                progress_callback,
                overall,
                "正在压缩内嵌图片："
                f"保留 {image_scale:.0%} 分辨率"
                f"（第 {image_number}/{image_count} 张）",
            )

        if include_vectors:
            if figure_regions and render_cache is not None:
                def native_progress(completed: int, total: int) -> None:
                    overall = 5 + round(
                        88
                        * (
                            (attempt - 1)
                            + (image_count + completed) / work_count
                        )
                        / max_attempts
                    )
                    _notify(
                        progress_callback,
                        overall,
                        "C++ 高速渲染 Figure："
                        f"{completed}/{total}（{vector_dpi} DPI）",
                    )

                _prefetch_native_figure_renders(
                    source_path,
                    figure_regions,
                    vector_dpi,
                    jpeg_quality,
                    boosted_figure_regions,
                    {
                        image_page
                        for image_page, _smask in image_locations.values()
                    },
                    render_cache,
                    destination.parent / f"{destination.stem}-native",
                    cancel_event,
                    native_stats,
                    native_session,
                    native_progress,
                )
            figure_number = 0
            for page_number, rectangles in sorted(figure_regions.items()):
                _check_cancel(cancel_event)
                try:
                    replaced = _replace_figure_regions(
                        output,
                        page_number,
                        rectangles,
                        vector_dpi,
                        jpeg_quality,
                        dpi_overrides=[
                            vector_dpi
                            + int(
                                (page_number, region_number)
                                in boosted_figure_regions
                            )
                            for region_number in range(len(rectangles))
                        ],
                        render_cache=render_cache,
                        cache_variant=(
                            round(image_scale * 10_000)
                            if any(
                                image_page == page_number
                                for image_page, _smask in image_locations.values()
                            )
                            else 0
                        ),
                    )
                    processed_figures += replaced
                except Exception:
                    # Keep the original Figure if its composite region cannot
                    # be rendered and redacted safely.
                    replaced = 0
                figure_number += len(rectangles)
                item_number = image_count + figure_number
                overall = 5 + round(
                    88
                    * ((attempt - 1) + item_number / work_count)
                    / max_attempts
                )
                _notify(
                    progress_callback,
                    overall,
                    f"正在压缩完整论文 Figure：{vector_dpi} DPI"
                    f"（第 {figure_number}/{figure_count} 张）",
                )

            for vector_number, page_number in enumerate(vector_pages, start=1):
                _check_cancel(cancel_event)
                try:
                    if _replace_vector_layer(
                        output,
                        page_number,
                        vector_dpi,
                        jpeg_quality,
                        render_cache,
                    ):
                        processed_vector_pages += 1
                except Exception:
                    # Preserve an unusual page's original vector layer and keep
                    # processing the remaining pages.
                    pass

                item_number = image_count + figure_count + vector_number
                overall = 5 + round(
                    88
                    * ((attempt - 1) + item_number / work_count)
                    / max_attempts
                )
                _notify(
                    progress_callback,
                    overall,
                    f"正在压缩矢量绘图：{vector_dpi} DPI"
                    f"（第 {vector_number}/{vector_count} 页）",
                )

        output.save(
            destination,
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
        )
    finally:
        output.close()
    return processed_images, processed_vector_pages, processed_figures


def _atomic_install(candidate: Path, output_path: Path) -> None:
    """Install a completed candidate without leaving a partial result."""
    os.replace(candidate, output_path)


def compress_pdf(
    input_path: str | Path,
    output_path: str | Path,
    target_bytes: int,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    compress_vectors: bool = True,
    vector_page_numbers: set[int] | None = None,
    selected_image_xrefs: set[int] | None = None,
    selected_vector_pages: set[int] | None = None,
    selected_figure_regions: dict[
        int, list[tuple[float, float, float, float]]
    ] | None = None,
) -> CompressionResult:
    """Compress *input_path* to no more than *target_bytes*.

    The original is never modified.  The output is replaced only after a full,
    valid candidate has been created.
    """
    source_path = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()

    if not source_path.is_file():
        raise CompressionError("请选择一个存在的 PDF 文件。")
    if source_path.suffix.lower() != ".pdf":
        raise CompressionError("所选文件不是 PDF。")
    if target_bytes <= 0:
        raise CompressionError("目标大小必须大于 0。")
    if source_path == destination:
        raise CompressionError("输出文件不能覆盖原 PDF，请选择其他位置。")

    destination.parent.mkdir(parents=True, exist_ok=True)
    original_bytes = source_path.stat().st_size
    _notify(progress_callback, 1, "正在读取 PDF…")
    _check_cancel(cancel_event)

    with tempfile.TemporaryDirectory(
        prefix=".pdf_size_reducer_", dir=destination.parent
    ) as temporary_directory:
        temp_dir = Path(temporary_directory)

        if original_bytes <= target_bytes:
            copied = temp_dir / "unchanged.pdf"
            shutil.copy2(source_path, copied)
            # copy2 preserves Windows' read-only bit. Output files belong to
            # the user and must remain replaceable even when the source came
            # from a read-only attachment or archive.
            copied.chmod(copied.stat().st_mode | stat.S_IWRITE)
            _check_cancel(cancel_event)
            _atomic_install(copied, destination)
            _notify(progress_callback, 100, "文件已经小于目标大小，无需压缩。")
            return CompressionResult(
                source_path,
                destination,
                original_bytes,
                destination.stat().st_size,
                target_bytes,
                "copied",
            )

        native_session: NativeWorkerSession | None = None
        try:
            source = fitz.open(source_path)
        except (RuntimeError, ValueError) as exc:
            raise CompressionError(f"无法打开此 PDF：{exc}") from exc

        try:
            if source.needs_pass:
                raise PasswordProtectedPDF("此 PDF 受密码保护，请先解密后再处理。")
            if source.page_count == 0:
                raise CompressionError("PDF 中没有可处理的页面。")

            # Capture xrefs before saving: MuPDF's structural cleanup may
            # renumber objects in the in-memory document.
            all_image_locations = _collect_images(source)
            if selected_image_xrefs is not None:
                image_locations = {
                    xref: location
                    for xref, location in all_image_locations.items()
                    if xref in selected_image_xrefs
                }
            elif vector_page_numbers is not None:
                image_locations = _collect_images(source, vector_page_numbers)
            else:
                image_locations = all_image_locations

            all_vector_pages = _collect_vector_pages(source)
            if compress_vectors:
                allowed_vector_pages = (
                    selected_vector_pages
                    if selected_vector_pages is not None
                    else vector_page_numbers
                    if vector_page_numbers is not None
                    else set(all_vector_pages)
                )
                vector_pages = [
                    page
                    for page in all_vector_pages
                    if page in allowed_vector_pages
                ]
            else:
                vector_pages = []
            figure_regions = (
                {
                    int(page): [tuple(map(float, rectangle)) for rectangle in regions]
                    for page, regions in selected_figure_regions.items()
                    if 0 <= int(page) < source.page_count and regions
                }
                if compress_vectors and selected_figure_regions is not None
                else {}
            )

            _notify(progress_callback, 3, "正在尝试无损优化…")
            lossless = temp_dir / "lossless.pdf"
            _save_lossless(source, lossless)
            _check_cancel(cancel_event)
            lossless_size = lossless.stat().st_size
            if lossless_size <= target_bytes:
                _atomic_install(lossless, destination)
                _notify(progress_callback, 100, "已通过无损优化达到目标大小。")
                return CompressionResult(
                    source_path,
                    destination,
                    original_bytes,
                    destination.stat().st_size,
                    target_bytes,
                    "lossless",
                )

            if not image_locations and not vector_pages and not figure_regions:
                if all_image_locations or (all_vector_pages and compress_vectors):
                    raise NoCompressibleImagesError(
                        "检测到了图片或矢量图，但指定页码范围内没有可处理的图。"
                    )
                raise NoCompressibleImagesError(
                    "这个 PDF 没有可压缩的内嵌位图或矢量绘图，文件主要由文字、"
                    "公式或字体构成。为保证文字清晰，工具不会缩小整个页面。"
                )

            # Standalone-image compression is attempted first. Complete Figures
            # and legacy vector layers are rasterized only if images alone cannot
            # meet the requested limit.
            # The coarse search uses at most 12 renders. Additional fixed-JPEG
            # resolution refinement closes size gaps caused by an integer JPEG
            # quality jump, while still spending every useful byte on clarity.
            max_attempts = 36
            attempt = 0
            render_cache = RenderCache()
            native_stats: dict[str, float | int] = {
                "batches": 0,
                "tasks": 0,
                "seconds": 0.0,
                "disabled": 0,
                "master_renders": 0,
                "variants": 0,
            }
            if figure_regions and find_native_worker() is not None:
                try:
                    _notify(progress_callback, 4, "正在启动 C++ 高速压缩引擎…")
                    native_session = NativeWorkerSession(
                        source_path, workspace=temp_dir
                    )
                except NativeWorkerError:
                    native_session = None

            if (
                os.environ.get("PDF_SIZE_REDUCER_DISABLE_PLANNER", "").strip()
                != "1"
            ):
                try:
                    planned_result = _try_planned_compression(
                        source_path,
                        destination,
                        original_bytes,
                        lossless_size,
                        target_bytes,
                        image_locations,
                        vector_pages,
                        figure_regions,
                        temp_dir,
                        progress_callback,
                        cancel_event,
                        native_session,
                        native_stats,
                    )
                except PlannerUnavailable:
                    planned_result = None
                    if (
                        native_session is not None
                        and native_session.process.poll() is not None
                    ):
                        native_session = None
                        native_stats["disabled"] = 1
                if planned_result is not None:
                    return planned_result
            candidates: dict[
                tuple[bool, int], tuple[Path, int, int, int, int]
            ] = {}
            parameter_candidates: dict[
                tuple[bool, int, int, int, int],
                tuple[Path, int, int, int, int],
            ] = {}

            def render_parameters(
                image_scale: float,
                quality: int,
                dpi: int,
                include_vectors: bool,
                figure_dpi_boost_count: int = 0,
            ) -> tuple[Path, int, int, int, int]:
                nonlocal attempt
                scale_key = round(image_scale * 10_000)
                key = (
                    include_vectors,
                    scale_key,
                    quality,
                    dpi,
                    figure_dpi_boost_count,
                )
                if key in parameter_candidates:
                    return parameter_candidates[key]
                attempt += 1
                stage = "vectors" if include_vectors else "images"
                candidate = temp_dir / (
                    f"candidate_{stage}_s{scale_key:05d}_q{quality:03d}"
                    f"_d{dpi:04d}_b{figure_dpi_boost_count:03d}.pdf"
                )
                processed_images, processed_vectors, processed_figures = (
                    _compress_candidate(
                        source_path,
                        candidate,
                        image_locations,
                        vector_pages,
                        figure_regions,
                        include_vectors,
                        image_scale,
                        quality,
                        dpi,
                        attempt,
                        max_attempts,
                        progress_callback,
                        cancel_event,
                        figure_dpi_boost_count,
                        render_cache,
                        native_stats,
                        native_session,
                    )
                )
                result = (
                    candidate,
                    candidate.stat().st_size,
                    processed_images,
                    processed_vectors,
                    processed_figures,
                )
                parameter_candidates[key] = result
                return result

            def render(
                score: int, include_vectors: bool
            ) -> tuple[Path, int, int, int, int]:
                key = (include_vectors, score)
                if key in candidates:
                    return candidates[key]
                quality_score = score * 100 / QUALITY_STEPS
                image_scale, quality = _quality_profile(quality_score)
                dpi = _vector_dpi(quality_score)
                result = render_parameters(
                    image_scale, quality, dpi, include_vectors
                )
                candidates[key] = result
                return result

            def search_stage(
                include_vectors: bool,
            ) -> tuple[
                tuple[int, Path, int, int, int, int] | None,
                tuple[Path, int, int, int, int],
            ]:
                """Find the highest feasible quality for one compression stage."""
                low_result = render(0, include_vectors)
                (
                    low_path,
                    low_size,
                    low_images,
                    low_vectors,
                    low_figures,
                ) = low_result
                if low_images + low_vectors + low_figures == 0 or low_size > target_bytes:
                    return None, low_result

                high_result = render(QUALITY_STEPS, include_vectors)
                (
                    high_path,
                    high_size,
                    high_images,
                    high_vectors,
                    high_figures,
                ) = high_result
                if high_size <= target_bytes:
                    return (
                        QUALITY_STEPS,
                        high_path,
                        high_size,
                        high_images,
                        high_vectors,
                        high_figures,
                    ), low_result

                low = 0
                high = QUALITY_STEPS
                low_bound_size = low_size
                high_bound_size = high_size
                best: tuple[int, Path, int, int, int, int] = (
                    low,
                    low_path,
                    low_size,
                    low_images,
                    low_vectors,
                    low_figures,
                )
                while high - low > 1:
                    _check_cancel(cancel_event)
                    # Search every remaining quality interval instead of
                    # stopping after a few coarse probes. This prevents a PDF
                    # from jumping from (for example) 3.97 MB to 2.97 MB when
                    # the requested budget is 3.12 MB.
                    score_span = high - low
                    size_span = high_bound_size - low_bound_size
                    if size_span > 0:
                        estimated = low + round(
                            (target_bytes - low_bound_size)
                            / size_span
                            * score_span
                        )
                        # Keep interpolation robust when PDF size is only
                        # approximately monotonic. Every probe removes at
                        # least one eighth of the remaining score interval.
                        guard = max(1, score_span // 8)
                        middle = max(
                            low + guard,
                            min(high - guard, estimated),
                        )
                    else:
                        middle = (low + high) // 2
                    (
                        middle_path,
                        middle_size,
                        middle_images,
                        middle_vectors,
                        middle_figures,
                    ) = render(middle, include_vectors)
                    if middle_size <= target_bytes:
                        low = middle
                        low_bound_size = middle_size
                        best = (
                            middle,
                            middle_path,
                            middle_size,
                            middle_images,
                            middle_vectors,
                            middle_figures,
                        )
                    else:
                        high = middle
                        high_bound_size = middle_size

                feasible = [
                    (score, path, size, images, vectors, figures)
                    for (stage_vectors, score), (
                        path,
                        size,
                        images,
                        vectors,
                        figures,
                    ) in candidates.items()
                    if stage_vectors == include_vectors and size <= target_bytes
                ]
                if feasible:
                    best = max(feasible, key=lambda item: (item[0], item[2]))
                return best, low_result

            best_result: tuple[int, Path, int, int, int, int] | None = None
            minimum_results: list[tuple[Path, int, int, int, int]] = []

            if image_locations:
                best_result, image_minimum = search_stage(False)
                minimum_results.append(image_minimum)

            if best_result is None and (vector_pages or figure_regions):
                best_result, combined_minimum = search_stage(True)
                minimum_results.append(combined_minimum)

            if best_result is None:
                processed_any = any(
                    images + vectors + figures > 0
                    for _path, _size, images, vectors, figures in minimum_results
                )
                if not processed_any:
                    raise NoCompressibleImagesError(
                        "检测到了图片或矢量图，但其编码格式无法安全处理。"
                        "为保证文字和页面结构，相关对象已保持原样。"
                    )
                minimum_size = min(result[1] for result in minimum_results)
                raise TargetTooSmallError(target_bytes, minimum_size)

            (
                best_score,
                best_path,
                best_size,
                best_images,
                best_vectors,
                best_figures,
            ) = best_result

            _check_cancel(cancel_event)
            quality_score = best_score * 100 / QUALITY_STEPS
            image_scale, quality = _quality_profile(quality_score)
            profile_dpi = _vector_dpi(quality_score)
            include_best_vectors = bool(best_vectors or best_figures)
            final_figure_dpi_boost_count = 0
            target_tolerance = max(1024, target_bytes // 10_000)

            # JPEG quality is integer-valued. Raising it by one for every
            # selected Figure can add tens or hundreds of kilobytes at once.
            # Keep the best feasible JPEG quality fixed and use a separate,
            # smooth resolution search to spend the remaining byte budget on
            # real detail instead of leaving a large unexplained size gap.
            resolution_at_maximum = False
            maximum_dpi = 900 if include_best_vectors else profile_dpi
            can_refine_resolution = (
                image_scale < 1.0 or profile_dpi < maximum_dpi
            )
            if best_size < target_bytes and can_refine_resolution:
                base_scale = image_scale
                base_dpi = profile_dpi

                def render_resolution_step(
                    step: int,
                ) -> tuple[
                    tuple[Path, int, int, int, int], float, int
                ]:
                    fraction = step / 1000
                    refined_scale = base_scale + (1.0 - base_scale) * fraction
                    refined_dpi = round(
                        base_dpi + (maximum_dpi - base_dpi) * fraction
                    )
                    return (
                        render_parameters(
                            refined_scale,
                            quality,
                            refined_dpi,
                            include_best_vectors,
                        ),
                        refined_scale,
                        refined_dpi,
                    )

                low_step = 0
                low_size = best_size
                high_step = 1000
                high_result, high_scale, high_dpi = render_resolution_step(
                    high_step
                )
                high_size = high_result[1]
                if high_size <= target_bytes:
                    (
                        best_path,
                        best_size,
                        best_images,
                        best_vectors,
                        best_figures,
                    ) = high_result
                    image_scale = high_scale
                    profile_dpi = high_dpi
                    resolution_at_maximum = True
                else:
                    for _refinement in range(12):
                        _check_cancel(cancel_event)
                        if (
                            high_step - low_step <= 1
                            or target_bytes - low_size <= target_tolerance
                        ):
                            break
                        size_span = high_size - low_size
                        if size_span > 0:
                            fraction = (target_bytes - low_size) / size_span
                            middle_step = low_step + round(
                                fraction * (high_step - low_step)
                            )
                        else:
                            middle_step = (low_step + high_step) // 2
                        middle_step = max(
                            low_step + 1, min(high_step - 1, middle_step)
                        )
                        (
                            middle_result,
                            middle_scale,
                            middle_dpi,
                        ) = render_resolution_step(middle_step)
                        middle_size = middle_result[1]
                        if middle_size <= target_bytes:
                            low_step = middle_step
                            low_size = middle_size
                            (
                                best_path,
                                best_size,
                                best_images,
                                best_vectors,
                                best_figures,
                            ) = middle_result
                            image_scale = middle_scale
                            profile_dpi = middle_dpi
                        else:
                            high_step = middle_step
                            high_size = middle_size

            # A one-DPI change applied to every Figure at once can still be a
            # sizeable jump. Spend the residual budget on the smallest Figure
            # regions first, raising only a subset by one DPI.
            total_figures = sum(
                len(rectangles) for rectangles in figure_regions.values()
            )
            if (
                include_best_vectors
                and best_figures
                and total_figures
                and target_bytes - best_size > target_tolerance
            ):
                low_boost_count = 0
                high_boost_count = total_figures
                high_result = render_parameters(
                    image_scale,
                    quality,
                    profile_dpi,
                    include_best_vectors,
                    high_boost_count,
                )
                if (
                    high_result[1] <= target_bytes
                    and high_result[1] > best_size
                ):
                    (
                        best_path,
                        best_size,
                        best_images,
                        best_vectors,
                        best_figures,
                    ) = high_result
                    final_figure_dpi_boost_count = high_boost_count
                elif high_result[1] > target_bytes:
                    while high_boost_count - low_boost_count > 1:
                        middle_boost_count = (
                            low_boost_count + high_boost_count
                        ) // 2
                        middle_result = render_parameters(
                            image_scale,
                            quality,
                            profile_dpi,
                            include_best_vectors,
                            middle_boost_count,
                        )
                        if middle_result[1] <= target_bytes:
                            low_boost_count = middle_boost_count
                            if middle_result[1] > best_size:
                                (
                                    best_path,
                                    best_size,
                                    best_images,
                                    best_vectors,
                                    best_figures,
                                ) = middle_result
                                final_figure_dpi_boost_count = (
                                    middle_boost_count
                                )
                        else:
                            high_boost_count = middle_boost_count

            # Standalone images have their own continuous scale control. Tune
            # that independently after the Figure allocation for sub-DPI-sized
            # adjustments to the final byte count.
            if (
                best_images
                and image_scale < 1.0
                and target_bytes - best_size > target_tolerance
            ):
                base_image_scale = image_scale

                def render_image_scale_step(
                    step: int,
                ) -> tuple[tuple[Path, int, int, int, int], float]:
                    refined_scale = base_image_scale + (
                        1.0 - base_image_scale
                    ) * (step / 1000)
                    return (
                        render_parameters(
                            refined_scale,
                            quality,
                            profile_dpi,
                            include_best_vectors,
                            final_figure_dpi_boost_count,
                        ),
                        refined_scale,
                    )

                low_step = 0
                low_size = best_size
                high_step = 1000
                high_result, high_scale = render_image_scale_step(high_step)
                high_size = high_result[1]
                if high_size <= target_bytes and high_size > best_size:
                    (
                        best_path,
                        best_size,
                        best_images,
                        best_vectors,
                        best_figures,
                    ) = high_result
                    image_scale = high_scale
                elif high_size > target_bytes:
                    for _refinement in range(10):
                        if (
                            high_step - low_step <= 1
                            or target_bytes - low_size <= target_tolerance
                        ):
                            break
                        size_span = high_size - low_size
                        middle_step = (
                            low_step
                            + round(
                                (target_bytes - low_size)
                                / size_span
                                * (high_step - low_step)
                            )
                            if size_span > 0
                            else (low_step + high_step) // 2
                        )
                        middle_step = max(
                            low_step + 1, min(high_step - 1, middle_step)
                        )
                        middle_result, middle_scale = (
                            render_image_scale_step(middle_step)
                        )
                        middle_size = middle_result[1]
                        if middle_size <= target_bytes:
                            low_step = middle_step
                            low_size = middle_size
                            if middle_size > best_size:
                                (
                                    best_path,
                                    best_size,
                                    best_images,
                                    best_vectors,
                                    best_figures,
                                ) = middle_result
                                image_scale = middle_scale
                        else:
                            high_step = middle_step
                            high_size = middle_size

            # If even maximum useful resolution remains below the target,
            # spend the remaining budget on JPEG quality up to 100.
            if (
                resolution_at_maximum
                and best_size < target_bytes
                and quality < 100
            ):
                low_quality = quality
                high_quality = 100
                high_result = render_parameters(
                    image_scale,
                    high_quality,
                    profile_dpi,
                    include_best_vectors,
                    final_figure_dpi_boost_count,
                )
                if high_result[1] <= target_bytes:
                    (
                        best_path,
                        best_size,
                        best_images,
                        best_vectors,
                        best_figures,
                    ) = high_result
                    quality = high_quality
                else:
                    while high_quality - low_quality > 1:
                        middle_quality = (low_quality + high_quality) // 2
                        middle_result = render_parameters(
                            image_scale,
                            middle_quality,
                            profile_dpi,
                            include_best_vectors,
                            final_figure_dpi_boost_count,
                        )
                        if middle_result[1] <= target_bytes:
                            low_quality = middle_quality
                            (
                                best_path,
                                best_size,
                                best_images,
                                best_vectors,
                                best_figures,
                            ) = middle_result
                            quality = middle_quality
                        else:
                            high_quality = middle_quality

            dpi = (
                profile_dpi + int(final_figure_dpi_boost_count > 0)
                if best_vectors or best_figures
                else None
            )
            _notify(progress_callback, 96, "正在写入最终文件…")
            _atomic_install(best_path, destination)
            final_size = destination.stat().st_size
            if final_size > target_bytes:
                raise CompressionError("最终文件未能满足目标大小，请重试。")
            _notify(progress_callback, 100, "压缩完成。")
            return CompressionResult(
                source_path,
                destination,
                original_bytes,
                final_size,
                target_bytes,
                (
                    "images_vectors"
                    if best_images and (best_vectors or best_figures)
                    else "vectors"
                    if best_vectors or best_figures
                    else "images"
                ),
                image_scale,
                quality,
                best_images,
                best_vectors,
                best_figures,
                dpi,
                attempt,
                render_cache.hits,
                render_cache.misses,
                bool(native_stats["batches"]),
                int(native_stats["batches"]),
                int(native_stats["tasks"]),
                float(native_stats["seconds"]),
            )
        finally:
            if native_session is not None:
                native_session.close()
            source.close()
