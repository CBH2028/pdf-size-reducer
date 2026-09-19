"""Generate the early PyInstaller splash asset reproducibly."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 720
HEIGHT = 420
SCALE = 2


def _font(size: int, *, semibold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows_fonts = Path("C:/Windows/Fonts")
    candidates = (
        [windows_fonts / "seguisb.ttf", windows_fonts / "msyhbd.ttc"]
        if semibold
        else [windows_fonts / "segoeui.ttf", windows_fonts / "msyh.ttc"]
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size * SCALE)
    return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font, fill) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    x0, y0, x1, y1 = box
    draw.text(
        ((x0 + x1 - width) / 2, (y0 + y1 - height) / 2 - bounds[1]),
        text,
        font=font,
        fill=fill,
    )


def generate_startup_splash(destination: str | Path) -> Path:
    """Create the 720x420 PNG consumed by ``PDF_Size_Reducer.spec``."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    width, height = WIDTH * SCALE, HEIGHT * SCALE
    image = Image.new("RGB", (width, height), "#F8F7FF")
    pixels = image.load()
    for y in range(height):
        vertical = y / max(1, height - 1)
        for x in range(width):
            horizontal = x / max(1, width - 1)
            blend = (horizontal + vertical) / 2
            pixels[x, y] = (
                round(252 - 14 * blend),
                round(252 - 13 * blend),
                round(255 - 1 * blend),
            )

    draw = ImageDraw.Draw(image, "RGBA")
    card = tuple(value * SCALE for value in (18, 15, 702, 397))
    draw.rounded_rectangle(
        tuple(value * SCALE for value in (18, 22, 702, 404)),
        radius=30 * SCALE,
        fill=(35, 26, 91, 34),
    )
    draw.rounded_rectangle(
        card,
        radius=30 * SCALE,
        fill=(250, 249, 255, 250),
        outline=(225, 222, 255, 255),
        width=1 * SCALE,
    )

    cx, cy = 360 * SCALE, 126 * SCALE
    draw.ellipse(
        (cx - 56 * SCALE, cy - 56 * SCALE, cx + 56 * SCALE, cy + 56 * SCALE),
        outline=(99, 91, 255, 38),
        width=8 * SCALE,
    )
    draw.rounded_rectangle(
        (cx - 35 * SCALE, cy - 35 * SCALE, cx + 35 * SCALE, cy + 35 * SCALE),
        radius=20 * SCALE,
        fill=(99, 91, 255, 255),
    )
    draw.rounded_rectangle(
        (cx - 15 * SCALE, cy - 22 * SCALE, cx + 16 * SCALE, cy + 23 * SCALE),
        radius=5 * SCALE,
        fill=(255, 255, 255, 250),
    )
    line_color = (99, 91, 255, 215)
    for offset, line_width in ((-7, 20), (1, 20), (9, 13)):
        draw.rounded_rectangle(
            (
                cx - 10 * SCALE,
                cy + offset * SCALE,
                cx + (-10 + line_width) * SCALE,
                cy + (offset + 2) * SCALE,
            ),
            radius=SCALE,
            fill=line_color,
        )

    _centered(
        draw,
        tuple(value * SCALE for value in (40, 205, 680, 246)),
        "PDF Size Reducer",
        _font(27, semibold=True),
        (25, 25, 31, 255),
    )
    _centered(
        draw,
        tuple(value * SCALE for value in (40, 250, 680, 277)),
        "MERGE  ·  COMPRESS  ·  EXPORT",
        _font(11, semibold=True),
        (119, 115, 130, 255),
    )
    draw.rounded_rectangle(
        tuple(value * SCALE for value in (90, 302, 630, 308)),
        radius=3 * SCALE,
        fill=(226, 224, 239, 255),
    )
    draw.rounded_rectangle(
        tuple(value * SCALE for value in (90, 302, 230, 308)),
        radius=3 * SCALE,
        fill=(99, 91, 255, 230),
    )
    _centered(
        draw,
        tuple(value * SCALE for value in (50, 369, 670, 391)),
        "LOCAL  ·  PRIVATE  ·  PRECISE",
        _font(10, semibold=True),
        (169, 165, 182, 255),
    )
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    image.save(destination, "PNG", optimize=True)
    return destination


if __name__ == "__main__":
    generate_startup_splash(Path(__file__).resolve().parents[1] / "build" / "startup_splash.png")
