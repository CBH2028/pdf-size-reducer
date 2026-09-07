"""Non-destructive page composition and a UI-independent composition tree."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
from uuid import uuid4

import pymupdf as fitz

from compressor import (
    CompressionError, MergeResult, _atomic_install, _check_cancel,
    get_pdf_source_state, merge_pdfs,
)

MAX_OUTPUT_PAGES = 20_000
MAX_DEPTH = 12


@dataclass(frozen=True)
class PageRef:
    source: Path
    index: int


@dataclass
class PlanNode:
    title: str
    page: PageRef | None = None
    children: list[PlanNode] = field(default_factory=list)
    uid: str = field(default_factory=lambda: uuid4().hex)


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


class CompositionPlan:
    def __init__(self):
        self.root = PlanNode("新的 PDF", uid="root")
        self.history, self.future = [], []

    def find(self, uid):
        return next((node for node in walk(self.root) if node.uid == uid), None)

    def parent(self, uid):
        return next((node for node in walk(self.root) if any(child.uid == uid for child in node.children)), None)

    def pages(self):
        return [node.page for node in walk(self.root) if node.page is not None]

    def bookmarks(self):
        result, offset = [], 0

        def visit(node, depth):
            nonlocal offset
            if node.page is not None:
                offset += 1
                return
            if node is not self.root and any(child.page is not None for child in walk(node)):
                result.append([depth, node.title.strip() or "分组", offset + 1])
            for child in node.children:
                visit(child, depth + (node is not self.root))

        visit(self.root, 1)
        return result

    def _change(self, action):
        before = deepcopy(self.root)
        try:
            action()
            self._validate()
        except Exception:
            self.root = before
            raise
        if self.root != before:
            self.history.append(before)
            self.future.clear()
            while len(self.history) > 40 or (len(self.history) > 1 and sum(sum(1 for _ in walk(root)) for root in self.history) > 100_000):
                self.history.pop(0)

    def _validate(self):
        seen, pages = set(), 0

        def visit(node, depth):
            nonlocal pages
            if node.uid in seen or depth > MAX_DEPTH:
                raise CompressionError(f"合成树不能循环嵌套，且最多 {MAX_DEPTH} 层。")
            seen.add(node.uid)
            if node.page is not None:
                pages += 1
                if node.children:
                    raise CompressionError("页面不能包含子节点，请插入到分组中。")
            for child in node.children:
                visit(child, depth + 1)

        visit(self.root, 0)
        if pages > MAX_OUTPUT_PAGES:
            raise CompressionError(f"单次合成最多 {MAX_OUTPUT_PAGES:,} 页，请分批处理。")

    def _container(self, uid):
        node = self.find(uid)
        if node is None or node.page is not None:
            raise CompressionError("请选择有效的目标分组。")
        return node

    def insert(self, pages, parent="root", index=None, group=None):
        container = self._container(parent)
        if len(self.pages()) + len(pages) > MAX_OUTPUT_PAGES:
            raise CompressionError(f"单次合成最多 {MAX_OUTPUT_PAGES:,} 页，请分批处理。")
        nodes = [PlanNode(f"{page.source.name} · 第 {page.index + 1} 页", page) for page in pages]
        if group is not None:
            nodes = [PlanNode(group, children=nodes)]
        at = len(container.children) if index is None else max(0, min(index, len(container.children)))
        self._change(lambda: container.children.__setitem__(slice(at, at), nodes))
        return [node.uid for node in nodes]

    def insert_groups(self, groups, parent="root", index=None):
        container = self._container(parent)
        if len(self.pages()) + sum(len(pages) for _title, pages in groups) > MAX_OUTPUT_PAGES:
            raise CompressionError(f"单次合成最多 {MAX_OUTPUT_PAGES:,} 页，请分批处理。")
        nodes = [PlanNode(title, children=[PlanNode(f"{page.source.name} · 第 {page.index + 1} 页", page) for page in pages]) for title, pages in groups]
        at = len(container.children) if index is None else max(0, min(index, len(container.children)))
        self._change(lambda: container.children.__setitem__(slice(at, at), nodes))
        return [node.uid for node in nodes]

    def top_selected(self, uids):
        chosen = set(uids) - {"root"}
        result = []

        def visit(node):
            if node.uid in chosen:
                result.append(node)
            else:
                for child in node.children:
                    visit(child)

        visit(self.root)
        return result

    def remove(self, uids):
        nodes = self.top_selected(uids)
        selected = {node.uid for node in nodes}

        def action():
            for parent in list(walk(self.root)):
                parent.children[:] = [child for child in parent.children if child.uid not in selected]

        self._change(action)

    def move(self, uids, parent, index):
        target = self._container(parent)
        nodes = self.top_selected(uids)
        if not nodes:
            return
        if any(target.uid in {child.uid for child in walk(node)} for node in nodes):
            raise CompressionError("不能把分组移入它自身或它的子分组。")
        index = max(0, min(index, len(target.children)))
        selected = {node.uid for node in nodes}
        index -= sum(child.uid in selected for child in target.children[:index])

        def action():
            for container in list(walk(self.root)):
                container.children[:] = [child for child in container.children if child.uid not in selected]
            target.children[index:index] = nodes

        self._change(action)

    def shift(self, uids, direction):
        nodes = self.top_selected(uids)
        if direction not in (-1, 1):
            return
        chosen = {node.uid for node in nodes}

        def action():
            for parent in walk(self.root):
                for at in range(len(parent.children))[::-direction]:
                    other = at + direction
                    if parent.children[at].uid in chosen and 0 <= other < len(parent.children) and parent.children[other].uid not in chosen:
                        parent.children[at], parent.children[other] = parent.children[other], parent.children[at]

        self._change(action)

    def rename(self, uid, title):
        node = self._container(uid)
        title = title.strip()[:200]
        if title:
            self._change(lambda: setattr(node, "title", title))

    def undo(self):
        if self.history:
            self.future.append(self.root)
            self.root = self.history.pop()

    def redo(self):
        if self.future:
            self.history.append(self.root)
            self.root = self.future.pop()


def parse_pages(text, count):
    """One-based ranges in entered order; duplicates within a selection are removed."""
    result, seen = [], set()
    for part in re.split(r"[,，\s]+", text.strip()):
        if not part:
            continue
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
        if not match:
            raise CompressionError("页码示例：1, 3-5；也可以在缩略图中 Ctrl / Shift 多选。")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= count or not 1 <= last <= count:
            raise CompressionError(f"页码必须在 1 到 {count} 之间。")
        for page in range(first - 1, last - 1 + (1 if last >= first else -1), 1 if last >= first else -1):
            if page not in seen:
                result.append(page)
                seen.add(page)
    return result


def compose_pdf(pages, output_path, expected_source_states=None, bookmarks=None,
                progress_callback=None, cancel_event=None):
    """Copy selected original pages, never rasterize; install only a validated PDF."""
    if not pages or len(pages) > MAX_OUTPUT_PAGES:
        raise CompressionError(f"合成结果必须包含 1 到 {MAX_OUTPUT_PAGES:,} 页。")
    if any(not isinstance(page, PageRef) or type(page.index) is not int or page.index < 0 for page in pages):
        raise CompressionError("合成树包含无效页码。")
    pages = [PageRef(Path(page.source).expanduser().resolve(), page.index) for page in pages]
    sources = tuple(dict.fromkeys(page.source for page in pages))
    destination = Path(output_path).expanduser().resolve()
    if destination in sources or destination.suffix.lower() != ".pdf":
        raise CompressionError("请另存为新的 PDF，不能覆盖源文件。")
    states = {path: get_pdf_source_state(path) for path in sources}
    if expected_source_states is not None and states != {Path(path).resolve(): state for path, state in expected_source_states.items()}:
        raise CompressionError("源 PDF 在页面预览后已发生变化，请重新检查并确认页码。")
    if any(state.size > 4 * 1024**3 for state in states.values()) or sum(state.size for state in states.values()) > 16 * 1024**3:
        raise CompressionError("单个 PDF 不能超过 4 GiB，使用的源文件总计不能超过 16 GiB。")
    cache, counts = OrderedDict(), {}

    def report(value, message):
        _check_cancel(cancel_event)
        if progress_callback:
            progress_callback(value, message)

    def document(path):
        if path not in cache:
            if len(cache) >= 4:
                cache.popitem(last=False)[1].close()
            doc = fitz.open(path)
            if doc.needs_pass or not doc.is_pdf or not doc.page_count:
                doc.close()
                raise CompressionError(f"{path.name} 无法读取、没有页面或受密码保护。")
            cache[path] = doc
        cache.move_to_end(path)
        return cache[path]

    try:
        for path in sources:
            report(2, f"检查源文件：{path.name}")
            counts[path] = document(path).page_count
        if any(page.index >= counts[page.source] for page in pages):
            raise CompressionError("合成树包含超出源 PDF 范围的页面，请重新检查。")
        # Whole documents in their original order still use the matched Rust/C++
        # merger. Arbitrary page plans take the form-aware compatibility route.
        whole_sequence, position = [], 0
        while position < len(pages):
            path = pages[position].source
            count = counts[path]
            if pages[position:position + count] != [PageRef(path, index) for index in range(count)]:
                whole_sequence = []
                break
            whole_sequence.append(path)
            position += count
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pdf_composition_", dir=destination.parent) as directory:
            candidate = Path(directory) / "composed.pdf"
            native_used, native_seconds = False, 0.0
            if 2 <= len(whole_sequence) <= 100:
                result = merge_pdfs(whole_sequence, candidate, expected_source_states=states,
                                    cancel_event=cancel_event,
                                    progress_callback=lambda value, message: report(5 + round(value * .8), message))
                native_used, native_seconds = result.native_worker_used, result.native_merge_seconds
                if bookmarks:
                    with fitz.open(candidate) as merged:
                        merged.set_toc(bookmarks)
                        merged.saveIncr()
            else:
                # Links to repeated source pages target their first output occurrence.
                # Links to omitted pages are intentionally dropped, never misdirected.
                mapping = {}
                for offset, ref in enumerate(pages):
                    mapping.setdefault((ref.source, ref.index), offset)
                with fitz.open() as merged:
                    offset = 0
                    while offset < len(pages):
                        ref = pages[offset]
                        end = offset + 1
                        while end < len(pages) and pages[end] == PageRef(ref.source, ref.index + end - offset):
                            end += 1
                        report(5 + round(60 * offset / len(pages)), f"复制原始页面：{offset + 1}/{len(pages)}")
                        merged.insert_pdf(document(ref.source), from_page=ref.index,
                                          to_page=ref.index + end - offset - 1,
                                          links=False, annots=True, widgets=True)
                        offset = end
                    for output_index, ref in enumerate(pages):
                        report(65 + round(20 * output_index / len(pages)), f"保留链接：{output_index + 1}/{len(pages)}")
                        source = document(ref.source)
                        source_page = source[ref.index]
                        for link in source_page.get_links():
                            kind = link.get("kind")
                            if kind == fitz.LINK_NAMED:
                                continue
                            target = {key: value for key, value in link.items() if key not in {"xref", "id"}}
                            target["from"] = fitz.Rect(link["from"]) * source_page.derotation_matrix
                            if kind == fitz.LINK_GOTO:
                                page_index = int(link.get("page", -1))
                                mapped = mapping.get((ref.source, page_index))
                                if mapped is None:
                                    continue
                                target["page"] = mapped
                                if "to" in link:
                                    target["to"] = fitz.Point(link["to"]) * source[page_index].derotation_matrix
                            merged[output_index].insert_link(target)
                    metadata = document(sources[0]).metadata
                    merged.set_metadata({key: value for key, value in metadata.items() if key not in {"format", "encryption"}})
                    if bookmarks:
                        merged.set_toc(bookmarks)
                    report(88, "保存合成结果，保留原始文字和页面内容…")
                    merged.save(candidate, garbage=4, deflate=True, use_objstms=1)
            report(94, "校验结果页数与页面…")
            with fitz.open(candidate) as result:
                if result.needs_pass or result.page_count != len(pages):
                    raise CompressionError("合成结果校验失败。")
                for page in result:
                    _check_cancel(cancel_event)
                    _ = page.rect
            report(98, "即将保存完整结果…")
            _atomic_install(candidate, destination, source_states=states, cancel_event=cancel_event)
        if progress_callback:
            progress_callback(100, "合成完成")
        return MergeResult(sources, destination, sum(state.size for state in states.values()),
                           destination.stat().st_size, len(pages), native_used, native_seconds)
    except CompressionError:
        raise
    except (RuntimeError, ValueError) as exc:
        raise CompressionError(f"页面合成失败：{exc}") from exc
    finally:
        for doc in cache.values():
            doc.close()


def render_page_png(ref, state, max_side=250):
    """Only called in the isolated renderer; bounded dimensions keep IPC small."""
    if get_pdf_source_state(ref.source) != state:
        raise CompressionError("源 PDF 已变化，请重新检查。")
    with fitz.open(ref.source) as doc:
        page = doc[ref.index]
        longest = max(page.rect.width, page.rect.height)
        if longest <= 0:
            raise CompressionError("页面尺寸无效。")
        pixmap = page.get_pixmap(matrix=fitz.Matrix(max_side / longest, max_side / longest), alpha=False)
        data = pixmap.tobytes("png")
    if get_pdf_source_state(ref.source) != state:
        raise CompressionError("渲染期间源 PDF 已变化，请重新检查。")
    return data
