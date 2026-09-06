"""Local, copy-on-save PDF text editing with geometric region detection.

This is a layout heuristic, not OCR or a Word-style reflow engine. The same
typesetting routine drives preview and save. No page is rasterized on export.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
import unicodedata

import pymupdf as fitz

from compressor import (
    CompressionCancelled, CompressionError, PDFSourceState,
    _assert_unchanged_sources, _atomic_install, get_pdf_source_state,
)


class PDFEditError(CompressionError):
    pass


@dataclass(frozen=True)
class TextStyle:
    font_name: str = "Helvetica"
    font_size: float = 12.0
    color: int = 0
    flags: int = 0
    alignment: int = 0
    line_height: float = 1.2
    font_xref: int = 0


@dataclass(frozen=True)
class EditRegion:
    key: str
    kind: str
    rect: tuple[float, float, float, float]
    text: str = ""
    style: TextStyle = TextStyle()
    baseline: float = 0.0
    erase_rects: tuple[tuple[float, float, float, float], ...] = ()
    editable: bool = True
    note: str = ""


@dataclass(frozen=True)
class PageLayout:
    page_index: int
    page_count: int
    rect: tuple[float, float, float, float]
    rotation_matrix: tuple[float, ...]
    display_rect: tuple[float, float, float, float]
    regions: tuple[EditRegion, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TextEdit:
    key: str
    page_index: int
    region_key: str | None
    rect: tuple[float, float, float, float]
    text: str
    style: TextStyle


@dataclass(frozen=True)
class EditResult:
    output_path: Path
    page_count: int
    edit_count: int
    output_bytes: int
    warnings: tuple[str, ...]
    deleted_pages: tuple[int, ...] = ()


def validate_deleted_pages(page_count, pages):
    pages = tuple(pages)
    if any(type(page) is not int or not 0 <= page < page_count for page in pages):
        raise PDFEditError("删除页码超出 PDF 范围。")
    pages = frozenset(pages)
    if len(pages) >= page_count:
        raise PDFEditError("不能删除全部页面，PDF 至少需要保留一页。")
    return pages


def parse_page_ranges(text, page_count):
    """Parse user-facing 1-based page ranges into original 0-based indices."""
    if len(text) > 20_000:
        raise PDFEditError("页码列表过长。")
    text = re.sub(r"\s*[-–—]\s*", "-", text.strip())
    pages = set()
    for part in re.split(r"[,，、;；\s]+", text):
        if not part:
            continue
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
        if match is None:
            raise PDFEditError("页码格式不正确，请输入例如 2, 4-6；页码从 1 开始。")
        if len(match[1]) > 10 or (match[2] and len(match[2]) > 10):
            raise PDFEditError("页码范围无效。")
        first = int(match[1])
        last = int(match[2] or match[1])
        if not 1 <= first <= last <= page_count:
            raise PDFEditError("页码范围无效；请使用文档的原始页码，起始页不能大于结束页。")
        pages.update(range(first - 1, last))
    return validate_deleted_pages(page_count, pages)


def format_page_ranges(pages):
    runs = []
    for page in sorted(set(pages)):
        if runs and page == runs[-1][1] + 1:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    return ", ".join(str(first + 1) if first == last else f"{first + 1}-{last + 1}" for first, last in runs)


def _cancel(event):
    if event is not None and event.is_set():
        raise CompressionCancelled("PDF 编辑任务已取消。")


def _source(path, expected):
    path = Path(path).expanduser().resolve()
    state = get_pdf_source_state(path)
    if expected is not None and state != expected:
        raise PDFEditError("源 PDF 已发生变化，请关闭编辑器并重新加载。")
    if state.size > 4 * 1024**3:
        raise PDFEditError("编辑器暂不支持超过 4 GiB 的 PDF。")
    return path, state


def _open(path):
    document = fitz.open(path)
    if not document.is_pdf or document.needs_pass or not document.page_count:
        document.close()
        raise PDFEditError("请选择有效且未加密的 PDF；密码保护文件需要先解密。")
    return document


def _font_key(name):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"^[A-Z]{6}\+", "", name).lower())


def _style(fonts, spans, lines):
    counts = Counter()
    representatives = {}
    for span in spans:
        key = (span["font"], round(span["size"], 2), span["color"], span["flags"])
        counts[key] += len(span["text"].strip()) or 1
        representatives[key] = span
    dominant = representatives[counts.most_common(1)[0][0]]
    name = dominant["font"]
    xref = next((entry[0] for entry in fonts if _font_key(entry[3]) == _font_key(name)), 0)
    alignment = 0
    if len(lines) > 1:
        lefts = [line["bbox"][0] for line in lines]
        rights = [line["bbox"][2] for line in lines]
        centers = [(left + right) / 2 for left, right in zip(lefts, rights)]
        if max(lefts) - min(lefts) > 3:
            if max(centers) - min(centers) < 2:
                alignment = 1
            elif max(rights) - min(rights) < 2:
                alignment = 2
    baselines = [line["spans"][0]["origin"][1] for line in lines]
    steps = [b - a for a, b in zip(baselines, baselines[1:]) if b > a + 1]
    leading = statistics.median(steps) / dominant["size"] if steps else 1.2
    style = TextStyle(name, dominant["size"], dominant["color"], dominant["flags"], alignment, max(0.8, min(3.0, leading)), xref)
    mixed = len(counts) > 1
    return style, mixed


def _line_groups(block):
    """Keep native paragraphs, but split distant columns and large paragraph gaps."""
    groups = []
    for line in block.get("lines", ()):
        spans = [span for span in line["spans"] if span.get("text", "").strip()]
        fragments = []
        for span in spans:
            if fragments and span["bbox"][0] - fragments[-1][-1]["bbox"][2] <= max(24, span["size"] * 2):
                fragments[-1].append(span)
            else:
                fragments.append([span])
        for fragment in fragments:
            box = fitz.Rect(fragment[0]["bbox"])
            for span in fragment[1:]:
                box |= fitz.Rect(span["bbox"])
            part = dict(line, spans=fragment, bbox=tuple(box))
            target = None
            for group in reversed(groups):
                previous = fitz.Rect(group[-1]["bbox"])
                size = fragment[0]["size"]
                overlap = min(box.x1, previous.x1) - max(box.x0, previous.x0)
                step = fragment[0]["origin"][1] - group[-1]["spans"][0]["origin"][1]
                if (0 < step <= size * 2.2 and
                        (overlap > min(box.width, previous.width) * 0.2 or abs(box.x0 - previous.x0) < 3) and
                        0.7 < size / group[-1]["spans"][0]["size"] < 1.4 and
                        tuple(line.get("dir", (1, 0))) == tuple(group[-1].get("dir", (1, 0)))):
                    target = group
                    break
            if target is None:
                groups.append([part])
            else:
                target.append(part)
    return groups


def detect_page_regions(document, page_index, cancel_event=None):
    _cancel(cancel_event)
    if not 0 <= page_index < document.page_count:
        raise PDFEditError("页码超出 PDF 范围。")
    page = document[page_index]
    bounds = page.rect * page.derotation_matrix
    if bounds.is_empty or not all(math.isfinite(v) for v in bounds) or max(bounds.width, bounds.height) > 100_000:
        raise PDFEditError("页面尺寸无效或过大。")
    regions, warnings = [], []
    widgets = [fitz.Rect(widget.rect) for widget in page.widgets()]
    fonts = page.get_fonts(full=True)
    blocks = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]
    if sum(len(block.get("lines", ())) for block in blocks) > 10_000:
        raise PDFEditError("单页文字过于复杂，暂不支持自动编辑。")
    for block_index, block in enumerate(blocks):
        if block.get("type") != 0:
            continue
        for part_index, lines in enumerate(_line_groups(block)):
            _cancel(cancel_event)
            spans = [span for line in lines for span in line["spans"]]
            box = fitz.Rect(lines[0]["bbox"])
            for line in lines[1:]:
                box |= fitz.Rect(line["bbox"])
            style, mixed = _style(fonts, spans, lines)
            text = "\n".join("".join(span["text"] for span in line["spans"]) for line in lines)
            horizontal = all(not line.get("wmode") and abs(line.get("dir", (1, 0))[0] - 1) < 0.001 and abs(line.get("dir", (1, 0))[1]) < 0.001 for line in lines)
            visible = all(span.get("alpha", 255) == 255 and bool(span.get("char_flags", 24) & 24) and not (span.get("char_flags", 0) & 32) and "glyphless" not in span["font"].lower() for span in spans)
            editable = horizontal and visible and bounds.contains(box) and not any(box.intersects(widget) for widget in widgets)
            note = "混合样式区域：整区替换使用主要样式，请预览确认。" if mixed else ""
            if not editable:
                note = "此区包含旋转/竖排、不可见文字、页面外文字或表单，请使用专用编辑器。"
            erase = []
            for span in spans:
                # Thin strips intersect glyph boxes without blanking backgrounds
                # or the full whitespace between lines of a paragraph.
                rect = fitz.Rect(span["bbox"])
                y = min(rect.y1 - 0.1, max(rect.y0 + 0.1, span["origin"][1] - span["size"] * 0.4))
                erase.append((rect.x0, y - 0.05, rect.x1, y + 0.05))
            regions.append(EditRegion(f"text:{page_index}:{block_index}:{part_index}", "text", tuple(box), text, style, spans[0]["origin"][1], tuple(erase), editable, note))
    for index, info in enumerate(page.get_image_info()):
        box = fitz.Rect(info["bbox"]) & bounds
        if not box.is_empty:
            regions.append(EditRegion(f"image:{page_index}:{index}", "image", tuple(box), editable=False, note="图片区：本版不含 OCR，不能直接替换图片里的文字。"))
    if not any(region.kind == "text" and region.editable for region in regions):
        warnings.append("本页没有可直接编辑的文字层。可以添加文本，但扫描图内的原文字不会自动消除。")
    if document.get_sigflags() > 0:
        warnings.append("检测到签名字段：为保护签名，本版不保存此文档的编辑。")
    return PageLayout(page_index, document.page_count, tuple(bounds), tuple(page.rotation_matrix), tuple(page.rect), tuple(regions), tuple(warnings))


def nearest_text_region(layout, rect):
    box = fitz.Rect(rect)
    candidates = [region for region in layout.regions if region.kind == "text" and region.editable]
    def distance(region):
        other = fitz.Rect(region.rect)
        dx = max(other.x0 - box.x1, box.x0 - other.x1, 0)
        dy = max(other.y0 - box.y1, box.y0 - other.y1, 0)
        return dx * 3 + dy + abs(other.x0 - box.x0) * 0.1
    return min(candidates, key=distance) if candidates else None


def _covers(font, text):
    return all(font.has_glyph(ord(char), fallback=False) for char in set(text) if not char.isspace())


def _resolve_font(document, page, style, text):
    original = _font_key(style.font_name)
    valid_xrefs = {font[0] for font in page.get_fonts(full=True) if _font_key(font[3]) == original}
    if style.font_xref in valid_xrefs:
        try:
            _name, _extension, _kind, data = document.extract_font(style.font_xref)
            if data and len(data) <= 32 * 1024**2:
                font = fitz.Font(fontbuffer=data)
                if _covers(font, text):
                    return font, ""
        except (ValueError, RuntimeError):
            pass
    bold, italic, serif, mono = bool(style.flags & 16), bool(style.flags & 2), bool(style.flags & 4), bool(style.flags & 8)
    base_names = {
        "helvetica": "helv", "helveticabold": "hebo", "helveticaoblique": "heit", "helveticaboldoblique": "hebi",
        "timesroman": "tiro", "timesbold": "tibo", "timesitalic": "tiit", "timesbolditalic": "tibi",
        "courier": "cour", "courierbold": "cobo", "courieroblique": "coit", "courierboldoblique": "cobi",
    }
    if original in base_names:
        font = fitz.Font(base_names[original])
        if _covers(font, text):
            return font, ""
    family_files = [
        (("timesnewroman", "times"), ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf")),
        (("arial", "helvetica"), ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf")),
        (("calibri",), ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf")),
        (("cambria",), ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf")),
        (("simsun", "song", "宋"), ("simsun.ttc",) * 4),
        (("microsoftyahei", "yahei"), ("msyh.ttc", "msyhbd.ttc", "msyh.ttc", "msyhbd.ttc")),
    ]
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for names, files in family_files:
        if any(name in original for name in names):
            path = font_dir / files[int(bold) + int(italic) * 2]
            if path.is_file():
                font = fitz.Font(fontfile=str(path))
                if _covers(font, text):
                    return font, f"{style.font_name}：已使用本机同字体族 {font.name}，请核对字形与字距。"
    index = int(bold) + int(italic) * 2
    fallback = ("cour", "cobo", "coit", "cobi") if mono else (("tiro", "tibo", "tiit", "tibi") if serif else ("helv", "hebo", "heit", "hebi"))
    font = fitz.Font(fallback[index])
    if not _covers(font, text):
        font = fitz.Font("cjk")
    if not _covers(font, text):
        raise PDFEditError("字体不包含某些新增字符。请更换文字或使用支持该字体的编辑器。")
    return font, f"原字体 {style.font_name} 不可复用或缺少字形，已替换为 {font.name}；请预览确认。"


def _wrap(text, font, size, width):
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        widths = font.char_lengths(paragraph, fontsize=size)
        start = 0
        while start < len(paragraph):
            total, end, last_space = 0, start, -1
            while end < len(paragraph) and total + widths[end] <= width + 0.05:
                total += widths[end]
                if paragraph[end].isspace():
                    last_space = end
                end += 1
            if end == start:
                raise PDFEditError("文本区域太窄，无法容纳一个字符，请扩大区域。")
            if end < len(paragraph) and last_space > start:
                end = last_space + 1
            lines.append(paragraph[start:end].rstrip())
            start = end
            while start < len(paragraph) and paragraph[start].isspace():
                start += 1
    return lines


def _check_edit(edit, layout):
    if len(edit.rect) != 4 or not all(math.isfinite(v) for v in edit.rect):
        raise PDFEditError("编辑区域坐标无效。")
    box = fitz.Rect(edit.rect)
    if box.is_empty or not fitz.Rect(layout.rect).contains(box):
        raise PDFEditError("文本区域必须完整位于页面内。")
    style = edit.style
    if not (math.isfinite(style.font_size) and 4 <= style.font_size <= 200 and math.isfinite(style.line_height) and 0.8 <= style.line_height <= 3 and style.alignment in (0, 1, 2) and 0 <= style.color <= 0xFFFFFF):
        raise PDFEditError("字号、行距、颜色或对齐参数无效。")
    if len(edit.text) > 50_000 or any(unicodedata.category(char) == "Cc" and char not in "\n\t\r" for char in edit.text):
        raise PDFEditError("单个区域文字过长或包含不支持的控制字符。")
    if re.search("[\u0590-\u08ff\u0900-\u0dff]", edit.text):
        raise PDFEditError("本版暂不支持需复杂字形重排的文字；请使用专门的排版工具。")


def apply_text_edits(document, edits, cancel_event=None, progress_callback=None):
    if len(edits) > 1000 or sum(len(edit.text) for edit in edits) > 500_000:
        raise PDFEditError("单次编辑超出安全限额（1000 个区域 / 50 万字符）。")
    if len({edit.key for edit in edits}) != len(edits):
        raise PDFEditError("存在重复的编辑记录。")
    if edits and document.get_sigflags() > 0:
        raise PDFEditError("文档含签名字段；为避免使签名失效，未保存编辑。")
    grouped = defaultdict(list)
    for edit in edits:
        grouped[edit.page_index].append(edit)
    warnings = []
    for page_index, page_edits in grouped.items():
        _cancel(cancel_event)
        layout = detect_page_regions(document, page_index, cancel_event)
        page = document[page_index]
        regions = {region.key: region for region in layout.regions}
        if any(annot.type[0] == fitz.PDF_ANNOT_REDACT for annot in page.annots()):
            raise PDFEditError("此页已有待处理的删改标记；请先在原编辑器处理，避免误应用。")
        original_keys = [edit.region_key for edit in page_edits if edit.region_key]
        if len(set(original_keys)) != len(original_keys):
            raise PDFEditError("同一文字区域不能重复替换。")
        plans, erasures = [], []
        for edit in page_edits:
            _cancel(cancel_event)
            _check_edit(edit, layout)
            region = regions.get(edit.region_key) if edit.region_key else None
            if edit.region_key and (region is None or not region.editable or region.kind != "text"):
                raise PDFEditError("该文字分区不支持直接修改，请重新选择。")
            if region:
                if region.note:
                    warnings.append(region.note)
                for erase in region.erase_rects:
                    if any(other.kind == "text" and other.key != region.key and fitz.Rect(erase).intersects(fitz.Rect(other.rect)) for other in layout.regions):
                        raise PDFEditError("此区文字与其他分区重叠，不能安全替换。")
                erasures.extend(region.erase_rects)
            text = unicodedata.normalize("NFC", edit.text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))
            if not text.strip():
                continue
            box = fitz.Rect(edit.rect)
            # No implicit overwrite of neighboring text or interactive fields.
            if any(other.kind == "text" and other.key not in original_keys and box.intersects(fitz.Rect(other.rect)) for other in layout.regions):
                raise PDFEditError("新文字区域与其他文字重叠，请移动或缩小区域。")
            if any(box.intersects(fitz.Rect(widget.rect)) for widget in page.widgets()):
                raise PDFEditError("新文字区域与表单字段重叠。")
            if any(box.intersects(fitz.Rect(other.rect)) for other in layout.regions if other.kind == "image"):
                warnings.append("新增/替换文字位于图片区上方，图片本身不会被删除，请核对预览。")
            font, warning = _resolve_font(document, page, edit.style, text)
            if warning:
                warnings.append(warning)
            size = edit.style.font_size
            lines = _wrap(text, font, size, box.width)
            baseline = box.y0 + font.ascender * size
            if region:
                baseline = box.y0 + (region.baseline - region.rect[1]) * size / region.style.font_size
            bottom = baseline + (len(lines) - 1) * size * edit.style.line_height - font.descender * size
            if baseline - font.ascender * size < box.y0 - 0.5 or bottom > box.y1 + 0.5:
                raise PDFEditError("文字超出区域：请扩大高度、精简文字或手动调小字号。为保持原文风格，没有自动缩小文字。")
            if any(box.intersects(previous[1]) for previous in plans):
                raise PDFEditError("两个新增或替换区域互相重叠，请调整位置。")
            plans.append((edit, box, font, lines, baseline))
        # Capture link destinations before redaction (which removes intersecting
        # links); normalize rotated page coordinates before restoring any lost link.
        links = page.get_links() if erasures else []
        for link in links:
            link["from"] = fitz.Rect(link["from"]) * page.derotation_matrix
            if link.get("kind") == fitz.LINK_GOTO and link.get("page", -1) >= 0 and "to" in link:
                link["to"] = fitz.Point(link["to"]) * document[link["page"]].derotation_matrix
        for rect in erasures:
            page.add_redact_annot(rect, fill=False, cross_out=False)
        if erasures:
            page.apply_redactions(images=0, graphics=0, text=0)
            page = document.reload_page(page)
            remaining = {link.get("xref") for link in page.get_links()}
            for link in links:
                if link.get("xref") not in remaining:
                    link.pop("xref", None)
                    link.pop("id", None)
                    page.insert_link(link)
        for edit, box, font, lines, baseline in plans:
            _cancel(cancel_event)
            name = "Edit" + hashlib.sha256(font.buffer).hexdigest()[:12]
            page.insert_font(fontname=name, fontbuffer=font.buffer)
            color = tuple(((edit.style.color >> shift) & 255) / 255 for shift in (16, 8, 0))
            for index, line in enumerate(lines):
                if not line:
                    continue
                width = font.text_length(line, fontsize=edit.style.font_size)
                x = box.x0 + (box.width - width) * (0, 0.5, 1)[edit.style.alignment]
                page.insert_text((x, baseline + index * edit.style.font_size * edit.style.line_height), line, fontname=name, fontsize=edit.style.font_size, color=color)
        if progress_callback:
            progress_callback(min(90, 10 + round(80 * (list(grouped).index(page_index) + 1) / len(grouped))), f"已排版第 {page_index + 1} 页")
    return tuple(dict.fromkeys(warnings))


def render_editor_page(source, page_index, edits, image_path, expected_source_state=None, cancel_event=None, deleted_pages=()):
    source, state = _source(source, expected_source_state)
    _cancel(cancel_event)
    with _open(source) as document:
        layout = detect_page_regions(document, page_index, cancel_event)
        deleted_pages = validate_deleted_pages(document.page_count, deleted_pages)
        warnings = ("此页已标记删除，保存时会移除；可撤销或在页面管理中取消删除。",) if page_index in deleted_pages else apply_text_edits(document, [edit for edit in edits if edit.page_index == page_index], cancel_event)
        _cancel(cancel_event)
        page = document[page_index]
        scale = min(1.6, math.sqrt(12_000_000 / (page.rect.width * page.rect.height)))
        page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(image_path)
    _assert_unchanged_sources({source: state})
    _cancel(cancel_event)
    return layout, tuple(dict.fromkeys((*layout.warnings, *warnings)))


def save_pdf_edits(source, destination, edits, expected_source_state=None, cancel_event=None, progress_callback=None, deleted_pages=()):
    source, state = _source(source, expected_source_state)
    destination = Path(destination).expanduser().resolve()
    if source == destination or (destination.exists() and os.path.samefile(source, destination)):
        raise PDFEditError("编辑结果必须另存为，不能覆盖源 PDF。")
    if destination.suffix.lower() != ".pdf":
        raise PDFEditError("编辑结果必须保存为 PDF 文件。")
    if not edits and not deleted_pages:
        raise PDFEditError("尚未添加修改。")
    _cancel(cancel_event)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pdf_edit_", dir=destination.parent) as directory:
        candidate = Path(directory) / "edited.pdf"
        with _open(source) as document:
            deleted_pages = validate_deleted_pages(document.page_count, deleted_pages)
            if deleted_pages and document.get_sigflags() > 0:
                raise PDFEditError("文档含签名字段；为避免使签名失效，未删除页面。")
            page_count = document.page_count - len(deleted_pages)
            active_edits = [edit for edit in edits if edit.page_index not in deleted_pages]
            warnings = apply_text_edits(document, active_edits, cancel_event, progress_callback)
            _cancel(cancel_event)
            if deleted_pages:
                document.delete_pages(sorted(deleted_pages))
            _cancel(cancel_event)
            document.subset_fonts()
            document.save(candidate, garbage=4, deflate=True)
        with _open(candidate) as verified:
            if verified.page_count != page_count:
                raise PDFEditError("编辑结果页数校验失败。")
        if progress_callback:
            progress_callback(99, "编辑完成，正在校验并保存…")
        _atomic_install(candidate, destination, source_states={source: state}, cancel_event=cancel_event)
    return EditResult(destination, page_count, len(active_edits), destination.stat().st_size, warnings, tuple(sorted(deleted_pages)))
