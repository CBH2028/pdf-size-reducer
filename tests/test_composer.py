from __future__ import annotations

import hashlib
from pathlib import Path
import threading

import pymupdf as fitz
import pytest

from compressor import CompressionCancelled, CompressionError, get_pdf_source_state
from pdf_composer import CompositionPlan, MAX_OUTPUT_PAGES, PageRef, compose_pdf, parse_pages, render_page_png


def make_pdf(path, pages=3):
    with fitz.open() as document:
        for number in range(pages):
            page = document.new_page(width=300 + number * 10, height=450)
            page.insert_text((35, 55), f"{path.stem} page {number + 1}")
            page.draw_rect(fitz.Rect(30, 100, 200, 210), color=(.2, .4, .7), fill=(.8, .9, .95))
        document.save(path)
    return path.resolve()


@pytest.fixture
def sources(tmp_path):
    return [make_pdf(tmp_path / name) for name in ("main.pdf", "material.pdf", "appendix.pdf")]


def refs(path, *indexes):
    return [PageRef(path, index) for index in indexes]


@pytest.mark.parametrize("text,expected", [
    ("1,3-5", [0, 2, 3, 4]), ("5-3,1", [4, 3, 2, 0]),
    ("1，2 3", [0, 1, 2]), ("2,2,1-3", [1, 0, 2]), ("", []),
])
def test_parse_page_ranges(text, expected):
    assert parse_pages(text, 5) == expected


@pytest.mark.parametrize("text", ["0", "6", "1-6", "a", "1--2", "-1", "1.5", "1;2"])
def test_invalid_ranges(text):
    with pytest.raises(CompressionError):
        parse_pages(text, 5)


def test_tree_insert_nested_move_remove_undo_redo(sources):
    main, material, appendix = sources
    plan = CompositionPlan()
    group = plan.insert(refs(main, 0, 1, 2), group="Main")[0]
    inserted = plan.insert(refs(material, 2, 0), group, 1)
    assert plan.pages() == refs(main, 0) + refs(material, 2, 0) + refs(main, 1, 2)
    nested = plan.insert(refs(appendix, 1), group, 3, "Nested")[0]
    assert plan.bookmarks() == [[1, "Main", 1], [2, "Nested", 4]]
    plan.move(inserted, "root", 1)
    assert plan.pages()[-2:] == refs(material, 2, 0)
    plan.remove([group, nested])
    assert plan.pages() == refs(material, 2, 0)
    plan.undo()
    assert len(plan.pages()) == 6
    plan.redo()
    assert len(plan.pages()) == 2
    plan.undo()
    plan.rename(group, "Renamed")
    assert not plan.future
    assert plan.bookmarks()[0][1] == "Renamed"


def test_tree_rejects_cycles_without_mutation(sources):
    plan = CompositionPlan()
    parent = plan.insert(refs(sources[0], 0), group="Main")[0]
    child = plan.insert([], parent, group="Nested")[0]
    before = plan.pages()
    count = len(plan.history)
    with pytest.raises(CompressionError):
        plan.move([parent], child, 0)
    assert plan.pages() == before and len(plan.history) == count
    with pytest.raises(CompressionError):
        plan.insert(refs(sources[1], 0), plan.find(parent).children[0].uid)


@pytest.mark.parametrize("rows,direction,expected", [
    ([1, 2], -1, [1, 2, 0, 3]), ([1, 2], 1, [0, 3, 1, 2]),
    ([0, 1], -1, [0, 1, 2, 3]), ([2, 3], 1, [0, 1, 2, 3]),
    ([1, 3], -1, [1, 0, 3, 2]),
])
def test_tree_group_movement(rows, direction, expected, sources):
    plan = CompositionPlan()
    ids = plan.insert(refs(sources[0], 0, 1, 2, 3))
    plan.shift([ids[index] for index in rows], direction)
    assert [ref.index for ref in plan.pages()] == expected


def test_tree_same_parent_move_and_descendant_selection(sources):
    plan = CompositionPlan()
    ids = plan.insert(refs(sources[0], 0, 1, 2, 3))
    plan.move(ids[1:3], "root", 4)
    assert [ref.index for ref in plan.pages()] == [0, 3, 1, 2]
    plan.undo()
    assert [ref.index for ref in plan.pages()] == [0, 1, 2, 3]
    group = plan.insert(refs(sources[1], 0, 1), group="Materials")[0]
    plan.move([group, plan.find(group).children[0].uid], "root", 0)
    assert plan.pages()[:2] == refs(sources[1], 0, 1)
    assert len(plan.find(group).children) == 2


def test_tree_page_limit_and_empty_group_bookmarks(sources):
    plan = CompositionPlan()
    plan.insert([], group="Empty")
    assert not plan.bookmarks()
    with pytest.raises(CompressionError):
        plan.insert(refs(sources[0], 0) * (MAX_OUTPUT_PAGES + 1))
    assert not plan.pages()


def test_selected_pages_preserve_text_renders_sources_and_groups(sources, tmp_path):
    main, material, appendix = sources
    selected = refs(main, 0) + refs(material, 2, 0) + refs(main, 1, 2) + refs(appendix, 1)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    output = tmp_path / "composed.pdf"
    progress = []
    result = compose_pdf(selected, output, {path: get_pdf_source_state(path) for path in sources},
                         [[1, "Main", 1], [2, "Inserted", 2], [1, "Appendix", 6]],
                         lambda value, message: progress.append(value))
    assert result.page_count == 6 and result.source_count == 3
    assert not result.native_worker_used
    assert progress == sorted(progress) and progress[-1] == 100
    with fitz.open(output) as merged:
        assert merged.get_toc() == [[1, "Main", 1], [2, "Inserted", 2], [1, "Appendix", 6]]
        for number, ref in enumerate(selected):
            with fitz.open(ref.source) as source:
                assert source[ref.index].get_text() == merged[number].get_text()
                assert source[ref.index].get_pixmap().samples == merged[number].get_pixmap().samples
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}


def test_whole_document_plan_uses_existing_merger(sources, tmp_path):
    from native_worker import find_native_worker
    selected = refs(sources[1], 0, 1, 2) + refs(sources[0], 0, 1, 2)
    result = compose_pdf(selected, tmp_path / "whole.pdf", bookmarks=[[1, "Two documents", 1]])
    assert result.page_count == 6
    if find_native_worker():
        assert result.native_worker_used


def test_composition_can_export_more_than_100_source_pdfs(tmp_path):
    sources = [make_pdf(tmp_path / f"part-{index}.pdf", 1) for index in range(101)]
    result = compose_pdf([PageRef(path, 0) for path in sources], tmp_path / "combined.pdf")
    assert result.source_count == 101 and result.page_count == 101
    assert not result.native_worker_used
    with fitz.open(result.output_path) as document:
        assert "part-0 page 1" in document[0].get_text()
        assert "part-100 page 1" in document[100].get_text()


def test_single_pdf_reorder_and_repeated_page(sources, tmp_path):
    output = tmp_path / "repeated.pdf"
    result = compose_pdf(refs(sources[0], 2, 0, 2), output)
    assert result.source_count == 1 and result.page_count == 3
    with fitz.open(output) as document:
        assert "page 3" in document[0].get_text()
        assert "page 1" in document[1].get_text()
        assert document[0].get_text() == document[2].get_text()


def test_links_to_included_omitted_and_repeated_pages(tmp_path):
    path = tmp_path / "linked.pdf"
    with fitz.open() as document:
        for _ in range(3):
            document.new_page()
        document[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(20, 20, 80, 40), "page": 2, "to": fitz.Point(40, 40)})
        document[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(20, 50, 80, 70), "page": 1})
        document[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(20, 80, 80, 100), "uri": "https://example.com/"})
        document[0].set_rotation(90)
        document[2].set_rotation(180)
        document[0].add_text_annot((100, 100), "Keep this annotation")
        document.save(path)
    output = tmp_path / "out.pdf"
    compose_pdf(refs(path, 2, 0, 2), output)
    with fitz.open(output) as document, fitz.open(path) as original:
        assert document[1].rotation == 90
        assert document[0].rotation == 180
        links = document[1].get_links()
        assert len(links) == 2
        internal = next(link for link in links if link["kind"] == fitz.LINK_GOTO)
        old = next(link for link in original[0].get_links() if link.get("page") == 2)
        assert internal["page"] == 0
        assert internal["from"] == old["from"]
        assert internal["to"] == old["to"]
        assert next(document[1].annots()).info["content"] == "Keep this annotation"


def test_form_fields_survive_selected_page_composition(tmp_path):
    path = tmp_path / "form.pdf"
    with fitz.open() as document:
        for i in range(2):
            page = document.new_page()
            widget = fitz.Widget()
            widget.field_name = f"field_{i}"
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.field_value = f"value {i}"
            widget.rect = fitz.Rect(30, 30, 200, 80)
            page.add_widget(widget)
        document.save(path)
    output = tmp_path / "out.pdf"
    compose_pdf(refs(path, 1, 0, 1), output)
    with fitz.open(output) as document:
        assert [next(page.widgets()).field_value for page in document] == ["value 1", "value 0", "value 1"]


@pytest.mark.parametrize("checkpoint", [2, 5, 65, 88, 94, 98])
def test_cancel_never_replaces_existing_output(sources, tmp_path, checkpoint):
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous result")
    event = threading.Event()

    def cancel(value, _message):
        if value >= checkpoint:
            event.set()

    with pytest.raises(CompressionCancelled):
        compose_pdf(refs(sources[0], 0) + refs(sources[1], 1), output,
                    progress_callback=cancel, cancel_event=event)
    assert output.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".pdf_composition_*"))


def test_changed_source_and_late_change_are_rejected(sources, tmp_path):
    selected = refs(sources[0], 0) + refs(sources[1], 2)
    expected = {path: get_pdf_source_state(path) for path in sources[:2]}
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous result")
    with sources[0].open("ab") as stream:
        stream.write(b"\nchanged\n")
    with pytest.raises(CompressionError, match="发生变化"):
        compose_pdf(selected, output, expected)

    def mutate(value, _message):
        if value == 98:
            with sources[1].open("ab") as stream:
                stream.write(b"\nchanged again\n")

    with pytest.raises(CompressionError, match="发生变化"):
        compose_pdf(selected, output, progress_callback=mutate)
    assert output.read_bytes() == b"previous result"


@pytest.mark.parametrize("indexes", [[], [-1], [3], [True]])
def test_invalid_plan_does_not_replace_output(sources, tmp_path, indexes):
    output = tmp_path / "out.pdf"
    output.write_bytes(b"previous result")
    with pytest.raises(CompressionError):
        compose_pdf(refs(sources[0], *indexes), output)
    assert output.read_bytes() == b"previous result"


def test_source_overwrite_is_forbidden(sources):
    before = sources[0].read_bytes()
    with pytest.raises(CompressionError):
        compose_pdf(refs(sources[0], 0), sources[0])
    assert sources[0].read_bytes() == before


def test_thumbnail_is_bounded_and_detects_stale_sources(sources):
    source = sources[0]
    state = get_pdf_source_state(source)
    data = render_page_png(PageRef(source, 1), state)
    image = fitz.Pixmap(data)
    assert max(image.width, image.height) <= 251
    with source.open("ab") as stream:
        stream.write(b"\nchanged\n")
    with pytest.raises(CompressionError):
        render_page_png(PageRef(source, 1), state)
