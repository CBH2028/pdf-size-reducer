from __future__ import annotations

import io
import stat
import threading
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

import compressor
from compressor import (
    NoCompressibleImagesError,
    TargetTooSmallError,
    compress_pdf,
)


def make_image_pdf(path: Path, pages: int = 3) -> None:
    image = Image.new("RGB", (1000, 1400))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = (
                (x * 17 + y * 13) % 256,
                (x * 7 + y * 19) % 256,
                (x * 23 + y * 5) % 256,
            )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    document = fitz.open()
    for page_number in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=image_bytes, rotate=0)
        page.insert_text((30, 30), f"Page {page_number + 1}")
        page.draw_rect(
            fitz.Rect(25, 40, 150, 90), color=(0, 0, 1), width=2
        )
    document.save(path)
    document.close()


def make_complete_figure_pdf(path: Path) -> None:
    """Create a Figure made from vectors, an image, and internal PDF text."""
    bitmap = Image.new("RGB", (420, 220))
    pixels = bitmap.load()
    for y in range(bitmap.height):
        for x in range(bitmap.width):
            pixels[x, y] = (
                (x * 11 + y * 17) % 256,
                (x * 23 + y * 5) % 256,
                (x * 7 + y * 29) % 256,
            )
    image_buffer = io.BytesIO()
    bitmap.save(image_buffer, format="PNG")

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(70, 90, 525, 355), color=(0, 0.5, 0), width=2)
    page.draw_line(fitz.Point(90, 320), fitz.Point(500, 115), color=(0, 0, 1))
    page.insert_image(
        fitz.Rect(170, 145, 425, 285), stream=image_buffer.getvalue()
    )
    page.insert_text((95, 125), "Inside Figure Label")
    page.insert_text((150, 385), "Fig. 2: Complete composite control diagram.")
    page.insert_text((72, 475), "Body text outside the Figure")
    page.draw_line(fitz.Point(72, 520), fitz.Point(520, 520), color=(1, 0, 0))
    document.save(path)
    document.close()


def test_copies_when_input_already_fits(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Small PDF")
    doc.save(source)
    doc.close()

    result = compress_pdf(source, output, source.stat().st_size + 100)

    assert result.method == "copied"
    assert output.read_bytes() == source.read_bytes()
    assert output.stat().st_mode & stat.S_IWRITE


def test_image_only_compression_stays_under_target(tmp_path: Path) -> None:
    source = tmp_path / "large.pdf"
    output = tmp_path / "compressed.pdf"
    make_image_pdf(source)
    target = 260 * 1024

    result = compress_pdf(source, output, target, vector_page_numbers={0, 1, 2})

    assert result.method == "images"
    assert output.stat().st_size <= target
    assert 0 < result.saved_ratio < 1
    with fitz.open(output) as compressed:
        assert compressed.page_count == 3
        assert "Page 1" in compressed[0].get_text()
        assert "Page 2" in compressed[1].get_text()
        assert "Page 3" in compressed[2].get_text()
        assert compressed[0].get_drawings()
        compressed_images = compressed[0].get_images(full=True)
        assert compressed_images
        assert compressed_images[0][2] < 1000


def test_rejects_impossible_target(tmp_path: Path) -> None:
    source = tmp_path / "large.pdf"
    output = tmp_path / "compressed.pdf"
    make_image_pdf(source, pages=1)

    with pytest.raises(TargetTooSmallError):
        compress_pdf(source, output, 10, vector_page_numbers={0})
    assert not output.exists()


def test_text_only_pdf_is_never_rasterized(tmp_path: Path) -> None:
    source = tmp_path / "text.pdf"
    output = tmp_path / "compressed.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Selectable text must remain selectable")
    doc.save(source)
    doc.close()

    with pytest.raises(NoCompressibleImagesError):
        compress_pdf(source, output, 100)

    assert not output.exists()
    with fitz.open(source) as original:
        assert "Selectable text" in original[0].get_text()


def test_vector_layer_compresses_without_rasterizing_text(tmp_path: Path) -> None:
    source = tmp_path / "vector.pdf"
    output = tmp_path / "compressed.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 72), "This text must stay selectable")
    shape = page.new_shape()
    for index in range(6000):
        x1 = 20 + (index * 37) % 550
        y1 = 100 + (index * 53) % 700
        x2 = 20 + (index * 97) % 550
        y2 = 100 + (index * 29) % 700
        shape.draw_line(fitz.Point(x1, y1), fitz.Point(x2, y2))
    shape.finish(color=(0.1, 0.3, 0.8), width=0.5)
    shape.commit()
    doc.save(source)
    doc.close()

    with fitz.open(source) as document:
        assert compressor._replace_vector_layer(document, 0, 72)
        document.save(output, garbage=4, deflate=True)

    with fitz.open(output) as compressed:
        assert "This text must stay selectable" in compressed[0].get_text()
        assert not compressed[0].get_drawings()
        vector_images = compressed[0].get_images(full=True)
        assert vector_images
        assert vector_images[0][1] == 0  # no transparency soft mask / black background
        assert vector_images[0][8] == "DCTDecode"  # opaque JPEG only
        encoded = compressed.extract_image(vector_images[0][0])["image"]
        rendered_layer = Image.open(io.BytesIO(encoded)).convert("RGB")
        corners = (
            rendered_layer.getpixel((0, 0)),
            rendered_layer.getpixel((rendered_layer.width - 1, 0)),
            rendered_layer.getpixel((0, rendered_layer.height - 1)),
            rendered_layer.getpixel(
                (rendered_layer.width - 1, rendered_layer.height - 1)
            ),
        )
        assert all(min(pixel) > 240 for pixel in corners)


def test_existing_alpha_channel_does_not_get_a_second_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeColorSpace:
        n = 3

    class FakePixmap:
        alpha = 1
        colorspace = FakeColorSpace()

    calls: list[tuple[object, ...]] = []
    pixmap = FakePixmap()

    def fake_pixmap_factory(*args: object) -> FakePixmap:
        calls.append(args)
        return pixmap

    monkeypatch.setattr(compressor.fitz, "Pixmap", fake_pixmap_factory)

    result = compressor._image_pixmap(object(), 10, 11)

    assert result is pixmap
    assert len(calls) == 1


def test_results_section_detection_and_page_range_parser(tmp_path: Path) -> None:
    source = tmp_path / "sections.pdf"
    document = fitz.open()
    headings = ("Method", "Results and discussion", "More results", "Conclusion")
    for heading in headings:
        page = document.new_page()
        page.insert_text((72, 72), heading)
    document.save(source)
    document.close()

    detected, page_count = compressor.detect_results_pages(source)

    assert page_count == 4
    assert detected == {1, 2}
    assert compressor.format_page_ranges(detected) == "2-3"
    assert compressor.parse_page_ranges("2-3, 4", page_count) == {1, 2, 3}
    assert compressor.parse_page_ranges("", page_count) == set()
    with pytest.raises(ValueError):
        compressor.parse_page_ranges("2-5", page_count)


def test_image_shared_with_excluded_page_stays_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "shared.pdf"
    make_image_pdf(source, pages=2)

    with fitz.open(source) as document:
        all_images = compressor._collect_images(document)
        result_page_only = compressor._collect_images(document, {1})

    assert all_images
    assert result_page_only == {}


def test_assets_can_be_listed_and_previewed(tmp_path: Path) -> None:
    source = tmp_path / "assets.pdf"
    make_image_pdf(source, pages=2)
    progress: list[tuple[int, str]] = []

    assets, page_count = compressor.list_pdf_assets(
        source,
        progress_callback=lambda value, message: progress.append(
            (value, message)
        ),
    )

    assert page_count == 2
    assert progress[0][0] == 2
    assert progress[-1][0] == 100
    assert all(
        current[0] <= following[0]
        for current, following in zip(progress, progress[1:])
    )
    assert any("第 1 / 2 页" in message for _value, message in progress)
    assert any(asset.kind == "image" for asset in assets)
    assert not any(asset.kind == "vector" for asset in assets)
    for asset in assets:
        assert asset.storage_bytes > 0
        assert not asset.storage_is_estimate
        assert "当前占用" in asset.storage_label
        thumbnail = compressor.render_asset_thumbnail(source, asset)
        with Image.open(io.BytesIO(thumbnail)) as image:
            assert image.size == (210, 145)


def test_asset_scan_can_be_cancelled(tmp_path: Path) -> None:
    source = tmp_path / "cancel-assets.pdf"
    make_image_pdf(source, pages=2)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(compressor.CompressionCancelled):
        compressor.list_pdf_assets(source, cancel_event=cancel_event)


def test_figure_storage_accounts_for_form_xobjects(tmp_path: Path) -> None:
    form_source = tmp_path / "form-source.pdf"
    source_document = fitz.open()
    source_page = source_document.new_page(width=400, height=240)
    for index in range(180):
        y = 12 + index % 90 * 2.3
        source_page.draw_line(
            fitz.Point(12, y),
            fitz.Point(388, 225 - (index * 7) % 200),
            color=(0.1, 0.3, 0.7),
        )
    source_document.save(form_source)
    source_document.close()

    target = tmp_path / "form-figure.pdf"
    embedded = fitz.open(form_source)
    target_document = fitz.open()
    target_page = target_document.new_page(width=595, height=842)
    target_page.show_pdf_page(fitz.Rect(70, 90, 525, 355), embedded, 0)
    target_page.insert_text((150, 385), "Fig. 3: Form XObject plot.")
    target_document.save(target)
    target_document.close()
    embedded.close()

    assets, _page_count = compressor.list_pdf_assets(target)
    assert len(assets) == 1
    figure = assets[0]
    with fitz.open(target) as document:
        form_bytes = sum(
            len(document.xref_stream_raw(int(item[0])) or b"")
            for item in document[0].get_xobjects()
        )
    assert form_bytes > 0
    assert figure.storage_bytes >= form_bytes // 2


def test_complete_figure_is_one_asset_and_replaced_as_one_image(
    tmp_path: Path,
) -> None:
    source = tmp_path / "complete-figure.pdf"
    output = tmp_path / "complete-figure-compressed.pdf"
    make_complete_figure_pdf(source)

    assets, page_count = compressor.list_pdf_assets(source)

    assert page_count == 1
    assert [asset.kind for asset in assets] == ["figure"]
    figure = assets[0]
    assert figure.title == "Fig. 2"
    assert figure.rect is not None
    assert figure.storage_bytes > 0
    assert figure.storage_is_estimate
    assert "约" in figure.storage_label
    thumbnail = compressor.render_asset_thumbnail(source, figure)
    with Image.open(io.BytesIO(thumbnail)) as preview:
        assert preview.size == (210, 145)
    full_preview = compressor.render_asset_image(source, figure, dpi=180)
    with Image.open(io.BytesIO(full_preview)) as preview:
        assert preview.width > 1000
        assert preview.height > 600

    with fitz.open(source) as document:
        replaced = compressor._replace_figure_regions(
            document, 0, [figure.rect], dpi=72, jpeg_quality=70
        )
        assert replaced == 1
        document.save(output, garbage=4, deflate=True)

    with fitz.open(output) as compressed:
        text = compressed[0].get_text()
        assert "Fig. 2: Complete composite control diagram." in text
        assert "Body text outside the Figure" in text
        assert "Inside Figure Label" in text
        assert compressed[0].get_drawings()  # the line outside the Figure survives
        images = compressed[0].get_images(full=True)
        assert len(images) == 1
        assert images[0][1] == 0
        assert images[0][8] == "DCTDecode"


def test_public_compressor_processes_only_selected_complete_figure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "selected-figure.pdf"
    minimum = tmp_path / "minimum.pdf"
    lossless = tmp_path / "lossless.pdf"
    output = tmp_path / "result.pdf"
    make_complete_figure_pdf(source)
    figure = compressor.list_pdf_assets(source)[0][0]
    assert figure.rect is not None

    compressor._compress_candidate(
        source,
        minimum,
        {},
        [],
        {0: [figure.rect]},
        True,
        image_scale=0.15,
        jpeg_quality=35,
        vector_dpi=36,
        attempt=1,
        max_attempts=1,
        progress_callback=None,
        cancel_event=None,
        figure_dpi_boost_count=1,
    )
    with fitz.open(source) as document:
        compressor._save_lossless(document, lossless)
    assert minimum.stat().st_size < lossless.stat().st_size
    target = (minimum.stat().st_size + lossless.stat().st_size) // 2

    result = compress_pdf(
        source,
        output,
        target,
        selected_image_xrefs=set(),
        selected_vector_pages=set(),
        selected_figure_regions={0: [figure.rect]},
    )

    assert result.method == "vectors"
    assert result.figures_processed == 1
    assert result.images_processed == 0
    assert result.vector_pages_processed == 0
    assert output.stat().st_size <= target
    assert target - output.stat().st_size <= 4096
    with fitz.open(output) as compressed:
        assert "Fig. 2: Complete composite control diagram." in compressed[0].get_text()
        assert "Inside Figure Label" in compressed[0].get_text()


def test_dense_vector_plot_uses_scalable_component_grouping() -> None:
    rectangles = [
        fitz.Rect(
            50 + (index % 100) * 1.8,
            80 + (index % 70) * 1.8,
            52 + (index % 100) * 1.8,
            82 + (index % 70) * 1.8,
        )
        for index in range(15_000)
    ]

    components = compressor._graphic_components(rectangles)

    assert len(components) == 1
    assert components[0].width > 170
    assert components[0].height > 120


def test_figure_quality_profile_preserves_readable_resolution() -> None:
    assert compressor._vector_dpi(0) == 180
    assert compressor._vector_dpi(100) == 720
    assert compressor._quality_profile(0)[1] == 45
    assert compressor._quality_profile(100)[1] == 98
    assert compressor.QUALITY_STEPS == 400
