"""Pillow -> branded 1080x1080 JPEG quote card.

Colors and type match drikeadvisory.com (Fraunces headings, Inter body,
ink navy #0a1428, accent blue #2e9bd6, off-white #f6f8fb). The TTFs are
bundled in static/fonts so cards render identically on any host.
"""
from PIL import Image, ImageDraw, ImageFont

import config

CANVAS = 1080
BG = "#f6f8fb"
INK = "#0a1428"
BLUE = "#2e9bd6"
BLUE_SOFT = "#cfe3f1"
GRAY = "#545d6b"

SERIF_PATH = config.FONT_DIR / "Fraunces-SemiBold.ttf"
SANS_PATH = config.FONT_DIR / "Inter-Medium.ttf"

TEXT_MAX_WIDTH = 880
TEXT_MAX_HEIGHT = 540


def _font(path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_quote(draw, text: str):
    """Largest serif size (76 down to 40) whose wrapped block fits the text area."""
    for size in range(76, 39, -4):
        font = _font(SERIF_PATH, size)
        lines = _wrap(draw, text, font, TEXT_MAX_WIDTH)
        line_height = int(size * 1.32)
        if (len(lines) * line_height <= TEXT_MAX_HEIGHT
                and all(draw.textlength(l, font=font) <= TEXT_MAX_WIDTH for l in lines)):
            return font, lines, line_height
    font = _font(SERIF_PATH, 40)
    return font, _wrap(draw, text, font, TEXT_MAX_WIDTH), 53


def _draw_tracked(draw, center_x: float, y: float, text: str, font, fill, tracking: int = 5):
    """Letter-spaced small caps for the wordmark (Pillow has no tracking option)."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = center_x - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def render_card(image_text: str, out_path: str) -> None:
    img = Image.new("RGB", (CANVAS, CANVAS), BG)
    draw = ImageDraw.Draw(img)

    # top accent bar
    draw.rectangle([0, 0, CANVAS, 10], fill=BLUE)

    # decorative open quote
    draw.text((96, 96), "“", font=_font(SERIF_PATH, 160), fill=BLUE_SOFT)

    # the quote, vertically centered in the middle band
    font, lines, line_height = _fit_quote(draw, image_text)
    block_height = len(lines) * line_height
    y = 250 + (TEXT_MAX_HEIGHT - block_height) / 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((CANVAS - w) / 2, y), line, font=font, fill=INK)
        y += line_height

    # footer: IL monogram + wordmark
    cx, cy, r = CANVAS / 2, 900, 42
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=INK)
    mono_font = _font(SERIF_PATH, 40)
    bbox = draw.textbbox((0, 0), "IL", font=mono_font)
    draw.text(
        (cx - (bbox[2] - bbox[0]) / 2 - bbox[0], cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
        "IL", font=mono_font, fill="#ffffff",
    )
    _draw_tracked(draw, cx, cy + r + 22, "IROKO LIFESCIENCES ADVISORY",
                  _font(SANS_PATH, 24), GRAY)
    _draw_tracked(draw, cx, cy + r + 60, "IKE OGBAA, MD", _font(SANS_PATH, 17), BLUE)

    img.save(out_path, "JPEG", quality=92)


# Instagram accepts aspect ratios from 4:5 (0.8) to 1.91:1
MIN_ASPECT, MAX_ASPECT = 0.8, 1.91
MAX_EDGE = 1920


def prepare_photo(file_obj, out_path: str) -> None:
    """Uploaded photo -> publishable JPEG: RGB, IG-safe aspect, sane size.

    Photos outside Instagram's 4:5–1.91:1 range are center-cropped to the
    nearest allowed bound.
    """
    img = Image.open(file_obj)
    img = img.convert("RGB")

    w, h = img.size
    aspect = w / h
    if aspect < MIN_ASPECT:  # too tall — crop height
        new_h = int(w / MIN_ASPECT)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif aspect > MAX_ASPECT:  # too wide — crop width
        new_w = int(h * MAX_ASPECT)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))

    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE))

    img.save(out_path, "JPEG", quality=90)
