"""Deterministic Pillow renderer; works offline with corrupt/missing pictures."""

import re
import unicodedata
import warnings
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .config import ROOT

Image.MAX_IMAGE_PIXELS = 16_000_000
NAVY = "#0b1225"
PANEL = "#131f38"
GREEN = "#8cdd35"
WHITE = "#f1f5ff"
MUTED = "#94a5c4"


def clean(text: str, limit=512) -> str:
    return " ".join(
        "".join(c for c in str(text)[:limit] if not unicodedata.category(c).startswith("C")).split()
    )


def font(size, text="", path=""):
    arabic = bool(re.search(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]", text))
    bundled = ROOT / "assets" / ("NotoSansArabic.ttf" if arabic else "NotoSans.ttf")
    candidates = [
        path,
        bundled,
        "DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(str(candidate), size)
        except (OSError, ValueError):
            pass
    return ImageFont.load_default(size=size)


def fit(draw, text, width, size, minimum, path=""):
    for value in range(size, minimum - 1, -1):
        selected = font(value, text, path)
        if draw.textlength(text, font=selected) <= width:
            return text, selected
    while text and draw.textlength(text + "…", font=selected) > width:
        text = text[:-1]
    return text + "…", selected


def wrap(draw, text, width, size, path="", lines=2):
    selected = font(size, text, path)
    result = []
    remaining = text
    for line in range(lines):
        if draw.textlength(remaining, font=selected) <= width:
            result.append(remaining)
            break
        if line == lines - 1:
            result.append(fit(draw, remaining, width, size, size, path)[0])
            break
        end = 1
        while end < len(remaining) and draw.textlength(remaining[: end + 1], font=selected) <= width:
            end += 1
        space = remaining.rfind(" ", 0, end + 1)
        if space > end // 2:
            end = space
        result.append(remaining[:end].rstrip())
        remaining = remaining[end:].lstrip()
    return result, selected


def picture(data: bytes | None, size, label):
    if data:
        try:
            with warnings.catch_warnings():
                # JPEG can be downsampled by libjpeg before pixel decoding. The hard
                # DecompressionBombError limit still applies to the source dimensions.
                jpeg = data.startswith(b"\xff\xd8")
                warnings.simplefilter("ignore" if jpeg else "error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as source:
                    if source.format == "JPEG":
                        source.draft("RGB", (size[0] * 2, size[1] * 2))
                    if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                        raise ValueError("Image dimensions too large")
                    source.seek(0)
                    converted = ImageOps.exif_transpose(source).convert("RGB")
                    return ImageOps.fit(converted, size, method=Image.Resampling.LANCZOS)
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
            SyntaxError,
        ):
            pass
    result = Image.new("RGB", size, PANEL)
    draw = ImageDraw.Draw(result)
    w, h = size
    if label == "MEMBER":
        draw.ellipse((w * 0.34, h * 0.19, w * 0.66, h * 0.47), fill="#344c71")
        draw.rounded_rectangle((w * 0.2, h * 0.53, w * 0.8, h * 0.94), radius=60, fill="#344c71")
    else:
        draw.rounded_rectangle(
            (w * 0.18, h * 0.22, w * 0.82, h * 0.73), radius=14, outline="#344c71", width=5
        )
        draw.ellipse((w * 0.58, h * 0.31, w * 0.69, h * 0.42), fill="#344c71")
        draw.polygon([(w * 0.24, h * 0.65), (w * 0.42, h * 0.43), (w * 0.58, h * 0.65)], fill="#344c71")
        draw.polygon([(w * 0.46, h * 0.65), (w * 0.61, h * 0.5), (w * 0.76, h * 0.65)], fill="#344c71")
        draw.text((w * 0.22, h * 0.79), "ROOM ARTWORK", font=font(18), fill=MUTED)
    return result


def render_card(
    display_name: str,
    thm_username: str,
    room_title: str,
    avatar_data: bytes | None = None,
    room_data: bytes | None = None,
    *,
    example=False,
    font_path="",
    difficulty="",
    operating_system="Not provided",
) -> bytes:
    image = Image.new("RGB", (1400, 440), NAVY)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 1387, 427), radius=26, outline="#263958", width=2)
    draw.line((40, 78, 1360, 78), fill="#263958", width=1)
    draw.ellipse((44, 36, 57, 49), fill=GREEN)
    draw.text((70, 27), "TRYHACKME  /  ROOM COMPLETE", font=font(20), fill=MUTED)
    if example:
        draw.rounded_rectangle((1110, 25, 1355, 62), radius=9, fill="#614616")
        draw.text((1130, 29), "EXAMPLE / TEST", font=font(20), fill="#ffe09a")

    for box, data, label in [
        ((44, 123, 288, 367), avatar_data, "MEMBER"),
        ((1064, 101, 1356, 389), room_data, "ROOM"),
    ]:
        x0, y0, x1, y1 = box
        tile = picture(data, (x1 - x0, y1 - y0), label)
        mask = Image.new("L", tile.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.width - 1, tile.height - 1), radius=18, fill=255)
        image.paste(tile, (x0, y0), mask)
        draw.rounded_rectangle(box, radius=18, outline=GREEN if label == "MEMBER" else "#344c71", width=3)

    name, selected = fit(draw, clean(display_name), 690, 43, 27, font_path)
    draw.text((326, 106), name, font=selected, fill=WHITE)
    thm, selected = fit(draw, "TryHackMe: " + clean(thm_username), 690, 23, 18, font_path)
    draw.text((328, 166), thm, font=selected, fill=GREEN)
    draw.text((328, 209), "Just completed", font=font(25), fill=MUTED)
    hard = difficulty.strip().casefold() in {"hard", "insane", "expert"}
    title_size = 44 if hard else 35
    lines, selected = wrap(draw, clean(room_title), 690, title_size, font_path)
    for index, line in enumerate(lines):
        draw.text((326, 248 + index * (title_size + 5)), line, font=selected, fill=WHITE)
    difficulty_label = clean(difficulty).title() or "Not provided"
    accent = "#ff7785" if hard else GREEN
    for box, label, color in [
        ((328, 357, 710, 390), f"OS: {operating_system}", MUTED),
        ((728, 357, 1016, 390), f"Difficulty: {difficulty_label}", accent),
    ]:
        draw.rounded_rectangle(box, radius=8, fill=PANEL, outline="#263958")
        label, selected = fit(draw, clean(label), box[2] - box[0] - 22, 18, 14, font_path)
        draw.text((box[0] + 11, box[1] + 4), label, font=selected, fill=color)
    draw.text(
        (328, 403),
        "FULL ROOM COMPLETION" if not example else "LOCAL EXAMPLE / NO REAL ACHIEVEMENT",
        font=font(13),
        fill=MUTED,
    )
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
