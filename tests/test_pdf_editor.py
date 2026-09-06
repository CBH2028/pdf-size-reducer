from dataclasses import replace
from io import BytesIO
import threading

import pymupdf as fitz
from PIL import Image
import pytest

from compressor import CompressionCancelled, CompressionError, get_pdf_source_state
import pdf_editor as editor


def make_pdf(path, rotation=0, background=False):
    with fitz.open() as doc:
        page = doc.new_page(width=500, height=600)
        if background:
            page.draw_rect(page.rect, fill=(0.8, 0.9, 1), color=None)
        page.insert_text((35, 60), "Original heading", fontname="hebo", fontsize=18, color=(0.2, 0.3, 0.5))
        page.insert_text((35, 110), "Left column first line\nLeft column second line", fontname="tiro", fontsize=12)
        page.insert_text((275, 110), "Right column stays\nRight column second", fontname="tiro", fontsize=12)
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(35, 40, 190, 64), "uri": "https://example.com"})
        page.set_rotation(rotation)
        doc.new_page().insert_text((40, 80), "Untouched second page")
        doc.save(path)


def layout(path, page=0):
    with fitz.open(path) as doc:
        return editor.detect_page_regions(doc, page)


def replacement(path, text="New heading"):
    region = next(region for region in layout(path).regions if "Original heading" in region.text)
    return editor.TextEdit(region.key, 0, region.key, region.rect, text, region.style)


def test_columns_are_separate_and_style_is_detected(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    page = layout(source)
    texts = [region for region in page.regions if region.kind == "text"]
    assert len(texts) == 3
    assert not any("Left" in region.text and "Right" in region.text for region in texts)
    heading = texts[0]
    assert heading.style.font_name == "Helvetica-Bold"
    assert heading.style.font_size == 18
    assert heading.style.flags & 16
    assert heading.style.color == 0x334D80
    nearest = editor.nearest_text_region(page, (280, 155, 450, 200))
    assert "Right" in nearest.text


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_replace_preserves_background_links_other_pages_and_baseline(tmp_path, rotation):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source, rotation, background=True)
    before = source.read_bytes()
    edit = replacement(source)
    result = editor.save_pdf_edits(source, output, [edit])
    assert result.edit_count == 1 and source.read_bytes() == before
    with fitz.open(source) as original, fitz.open(output) as changed:
        text = changed[0].get_text()
        assert "Original heading" not in text and "New heading" in text
        assert "Left column second line" in text and "Right column stays" in text
        assert changed[1].get_pixmap().samples == original[1].get_pixmap().samples
        assert changed[0].rotation == rotation
        assert len(changed[0].get_drawings()) == len(original[0].get_drawings())
        old_link, new_link = original[0].get_links()[0], changed[0].get_links()[0]
        assert new_link["uri"] == old_link["uri"]
        assert tuple(new_link["from"]) == pytest.approx(tuple(old_link["from"]), abs=0.1)
        span = next(span for block in changed[0].get_text("dict")["blocks"] for line in block.get("lines", []) for span in line["spans"] if "New heading" in span["text"])
        assert span["origin"][1] == pytest.approx(60, abs=0.01)
        assert span["size"] == 18 and span["color"] == edit.style.color


def test_add_chinese_searchable_text_reports_fallback(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    edit = editor.TextEdit("addition", 0, None, (35, 200, 400, 260), "新增中文说明，与原文共同保存。", editor.TextStyle())
    result = editor.save_pdf_edits(source, output, [edit])
    assert any("替换" in warning for warning in result.warnings)
    with fitz.open(output) as doc:
        assert "新增中文说明" in doc[0].get_text()
        assert "Original heading" in doc[0].get_text()


def test_embedded_font_is_reused_without_fallback(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_font(fontname="EmbeddedTest", fontbuffer=fitz.Font("tiro").buffer)
        page.insert_text((40, 80), "Embedded original words", fontname="EmbeddedTest", fontsize=14)
        doc.save(source)
    region = layout(source).regions[0]
    edit = editor.TextEdit(region.key, 0, region.key, region.rect, "Edited words", region.style)
    result = editor.save_pdf_edits(source, output, [edit])
    assert result.warnings == ()
    with fitz.open(output) as doc:
        assert "Edited words" in doc[0].get_text()


@pytest.mark.parametrize("problem", ["overflow", "overlap", "off_page", "nan", "missing_region", "duplicate", "same_source"])
def test_invalid_edits_do_not_replace_output(tmp_path, problem):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    output.write_bytes(b"existing output")
    edit = replacement(source)
    if problem == "overflow":
        edit = replace(edit, text="Very long text " * 300)
    elif problem == "overlap":
        edit = replace(edit, rect=(35, 40, 470, 180))
    elif problem == "off_page":
        edit = replace(edit, rect=(-1, 40, 100, 65))
    elif problem == "nan":
        edit = replace(edit, rect=(float("nan"), 40, 100, 65))
    elif problem == "missing_region":
        edit = replace(edit, region_key="missing")
    elif problem == "same_source":
        output = source
    previous = output.read_bytes()
    with pytest.raises(editor.PDFEditError):
        editor.save_pdf_edits(source, output, [edit, edit] if problem == "duplicate" else [edit])
    assert output.read_bytes() == previous


@pytest.mark.parametrize("phase", ["stale", "late_change", "cancel"])
def test_source_and_cancellation_checks(tmp_path, phase):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    edit = replacement(source)
    state = get_pdf_source_state(source)
    event = threading.Event()
    output.write_bytes(b"existing output")
    def change():
        with source.open("ab") as stream:
            stream.write(b"\n% external change\n")
    if phase == "stale":
        change()
    def progress(value, message):
        if value == 99:
            if phase == "late_change":
                change()
            elif phase == "cancel":
                event.set()
    with pytest.raises(CompressionError):
        editor.save_pdf_edits(source, output, [edit], state, event, progress)
    assert output.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".pdf_edit_*"))


def test_preview_matches_saved_render(tmp_path):
    source, output, preview = tmp_path / "source.pdf", tmp_path / "result.pdf", tmp_path / "preview.png"
    make_pdf(source)
    edit = replacement(source)
    editor.render_editor_page(source, 0, [edit], preview)
    editor.save_pdf_edits(source, output, [edit])
    with fitz.open(output) as doc:
        actual = doc[0].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        assert Image.open(preview).tobytes() == actual.samples


def test_scan_image_region_is_not_presented_as_editable_text(tmp_path):
    source = tmp_path / "scan.pdf"
    data = BytesIO()
    Image.new("RGB", (100, 100), "white").save(data, "PNG")
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, stream=data.getvalue())
        doc.save(source)
    page = layout(source)
    assert page.warnings
    assert page.regions[0].kind == "image" and not page.regions[0].editable


def test_existing_redaction_is_not_applied_accidentally(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    edit = replacement(source)
    with fitz.open(source) as doc:
        doc[0].add_redact_annot((200, 200, 300, 250))
        doc.saveIncr()
    with pytest.raises(editor.PDFEditError, match="已有待处理"):
        editor.save_pdf_edits(source, output, [edit])
    assert not output.exists()


@pytest.mark.parametrize("text, expected", [("2, 4-6", {1, 3, 4, 5}), ("2，4 – 5; 2", {1, 3, 4}), ("", set()), ("1 3 5", {0, 2, 4})])
def test_parse_page_ranges(text, expected):
    pages = editor.parse_page_ranges(text, 8)
    assert pages == expected
    assert editor.parse_page_ranges(editor.format_page_ranges(pages), 8) == pages


@pytest.mark.parametrize("text", ["0", "-1", "9", "4-2", "1-8", "one", "1,,2-9", "9" * 5000])
def test_invalid_page_ranges(text):
    with pytest.raises(editor.PDFEditError):
        editor.parse_page_ranges(text, 8)


def test_delete_pages_with_text_edits_preserves_original_numbering_and_links(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    with fitz.open() as doc:
        for index in range(5):
            doc.new_page().insert_text((40, 80), f"Original page {index + 1}")
        doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(40, 60, 140, 85), "page": 4, "to": fitz.Point(0, 0)})
        doc.set_toc([[1, "Kept chapter", 5], [1, "Removed chapter", 2]])
        doc.save(source)
    original_bytes = source.read_bytes()
    with fitz.open(source) as doc:
        region = editor.detect_page_regions(doc, 4).regions[0]
    edit = editor.TextEdit(region.key, 4, region.key, region.rect, "Edited page 5", region.style)
    result = editor.save_pdf_edits(source, output, [edit], deleted_pages={1, 3})
    assert result.page_count == 3 and result.deleted_pages == (1, 3)
    assert source.read_bytes() == original_bytes
    with fitz.open(source) as original, fitz.open(output) as doc:
        assert "Original page 1" in doc[0].get_text()
        assert doc[1].get_pixmap().samples == original[2].get_pixmap().samples
        assert "Edited page 5" in doc[2].get_text()
        assert doc[0].get_links()[0]["page"] == 2
        assert doc.get_toc()[0] == [1, "Kept chapter", 3]


def test_delete_only_and_skip_invalid_draft_on_removed_page(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    discarded_edit = replace(replacement(source), text="Overflow " * 500)
    result = editor.save_pdf_edits(source, output, [discarded_edit], deleted_pages=[0])
    assert result.page_count == 1 and result.edit_count == 0
    with fitz.open(output) as doc:
        assert "Untouched second page" in doc[0].get_text()


@pytest.mark.parametrize("deleted", [{0, 1}, {2}, {-1}, {True}])
def test_invalid_page_deletion_preserves_existing_output(tmp_path, deleted):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    output.write_bytes(b"existing")
    with pytest.raises(editor.PDFEditError):
        editor.save_pdf_edits(source, output, [], deleted_pages=deleted)
    assert output.read_bytes() == b"existing"


def test_delete_pages_cancelled_before_final_install(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    output.write_bytes(b"existing")
    event = threading.Event()
    with pytest.raises(CompressionCancelled):
        editor.save_pdf_edits(source, output, [], cancel_event=event, progress_callback=lambda value, text: event.set() if value == 99 else None, deleted_pages=[0])
    assert output.read_bytes() == b"existing"


def test_edit_internal_link_to_rotated_page_is_preserved(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source, rotation=90)
    with fitz.open(source) as doc:
        doc[1].set_rotation(270)
        doc[0].insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(35, 40, 190, 64), "page": 1, "to": fitz.Point(40, 80)})
        doc.saveIncr()
    editor.save_pdf_edits(source, output, [replacement(source)])
    with fitz.open(source) as original, fitz.open(output) as changed:
        previous = next(link for link in original[0].get_links() if link["kind"] == fitz.LINK_GOTO)
        actual = next(link for link in changed[0].get_links() if link["kind"] == fitz.LINK_GOTO)
        assert actual["page"] == previous["page"]
        assert tuple(actual["to"]) == pytest.approx(tuple(previous["to"]), abs=0.1)
        assert tuple(actual["from"]) == pytest.approx(tuple(previous["from"]), abs=0.1)


def test_deleted_text_is_removed_without_white_patch(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source, background=True)
    editor.save_pdf_edits(source, output, [replacement(source, text="")])
    with fitz.open(output) as doc:
        assert "Original heading" not in doc[0].get_text()
        image = Image.open(BytesIO(doc[0].get_pixmap().tobytes("png")))
        assert image.getpixel((70, 53)) == image.getpixel((70, 30))


@pytest.mark.parametrize("mode", ["invisible", "rotated", "transparent"])
def test_special_text_is_read_only(tmp_path, mode):
    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((100, 200), "Special text", rotate=90 if mode == "rotated" else 0, render_mode=3 if mode == "invisible" else 0, fill_opacity=0.4 if mode == "transparent" else 1)
        doc.save(source)
    assert not layout(source).regions[0].editable


def test_signature_fields_protect_against_editing_or_deletion(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    with fitz.open(source) as doc:
        widget = fitz.Widget()
        widget.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
        widget.field_name = "Signature"
        widget.rect = fitz.Rect(40, 400, 250, 450)
        doc[0].add_widget(widget)
        doc.saveIncr()
    assert layout(source).warnings
    with pytest.raises(editor.PDFEditError, match="签名"):
        editor.save_pdf_edits(source, output, [replacement(source)])
    with pytest.raises(editor.PDFEditError, match="签名"):
        editor.save_pdf_edits(source, output, [], deleted_pages=[1])
    assert not output.exists()


def test_cropped_rotated_page_keeps_crop_and_other_content(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    make_pdf(source)
    with fitz.open(source) as doc:
        doc[0].set_cropbox((20, 20, 480, 580))
        doc[0].set_rotation(90)
        doc.saveIncr()
    editor.save_pdf_edits(source, output, [replacement(source)])
    with fitz.open(source) as original, fitz.open(output) as result:
        assert result[0].cropbox == original[0].cropbox
        assert result[0].rotation == 90
        assert "New heading" in result[0].get_text()
        assert "Right column stays" in result[0].get_text()


def test_password_pdf_cannot_be_edited(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 80), "Encrypted")
        doc.new_page()
        doc.save(source, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="password", owner_pw="owner")
    with pytest.raises(editor.PDFEditError, match="加密"):
        editor.save_pdf_edits(source, output, [], deleted_pages=[0])
    assert not output.exists()
