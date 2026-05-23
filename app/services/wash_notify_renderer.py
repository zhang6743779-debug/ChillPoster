import os
import tempfile
from io import BytesIO
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.logger import logger


_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int, bold: bool = False):
    candidates = _FONT_CANDIDATES if bold else list(reversed(_FONT_CANDIDATES))
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    try:
        return float(draw.textlength(str(text), font=font))
    except Exception:
        return float(font.getlength(str(text)))


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 2) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for char in paragraph:
            if _text_width(draw, current + char, font) <= max_width:
                current += char
                continue
            if current:
                lines.append(current)
            current = char
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) >= max_lines:
            break
    if lines and len(lines[-1]) < len(str(text or "")) and not lines[-1].endswith("..."):
        lines[-1] = lines[-1].rstrip("，。；、 ") + "..."
    return lines


def _rounded(draw: ImageDraw.ImageDraw, box, fill, outline=None, width: int = 1, radius: int = 28):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        logger.debug(f"[WashNotify] 下载通知海报失败: {e}")
        return None


def _fit_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    src_w, src_h = image.size
    scale = max(target_w / max(src_w, 1), target_h / max(src_h, 1))
    resized = image.resize((max(1, int(src_w * scale)), max(1, int(src_h * scale))), Image.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def _draw_placeholder_poster(draw: ImageDraw.ImageDraw, box, title: str, accent: tuple[int, int, int]):
    x1, y1, x2, y2 = box
    width = x2 - x1
    height = y2 - y1
    for y in range(y1, y2):
        p = (y - y1) / max(height, 1)
        color = (
            int(accent[0] * (0.45 + p * 0.35)),
            int(accent[1] * (0.45 + p * 0.35)),
            int(accent[2] * (0.45 + p * 0.35)),
        )
        draw.line([(x1, y), (x2, y)], fill=color)
    draw.polygon([(x1, y2), (x1 + int(width * 0.42), y2), (x1 + int(width * 0.82), y1), (x1 + int(width * 0.55), y1)], fill=(238, 238, 238))
    font = _font(24, True)
    y = y1 + 30
    for line in _wrap_text(draw, title, font, width - 44, max_lines=5):
        draw.text((x1 + 22, y), line, font=font, fill=(255, 255, 255))
        y += 36


def _draw_poster(canvas: Image.Image, draw: ImageDraw.ImageDraw, box, title: str, poster_url: str, accent: tuple[int, int, int]):
    x1, y1, x2, y2 = box
    poster = _download_image(poster_url)
    if poster:
        canvas.paste(_fit_cover(poster, (x2 - x1, y2 - y1)), (x1, y1))
    else:
        _draw_placeholder_poster(draw, box, title, accent)
    draw.rounded_rectangle(box, radius=18, outline=(85, 94, 110), width=3)


def _draw_pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fill, fg):
    font = _font(24, True)
    width = int(_text_width(draw, text, font)) + 36
    _rounded(draw, (x, y, x + width, y + 42), fill, radius=21)
    draw.text((x + 18, y + 7), text, font=font, fill=fg)


def _draw_kv(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, max_width: int,
             size: int = 24, value_fill=(236, 240, 245), max_lines: int = 2) -> int:
    label_font = _font(size, True)
    value_font = _font(size, True)
    draw.text((x, y), label, font=label_font, fill=(150, 162, 176))
    offset = int(_text_width(draw, label, label_font)) + 8
    line_y = y
    for line in _wrap_text(draw, value, value_font, max_width - offset, max_lines=max_lines):
        draw.text((x + offset, line_y), line, font=value_font, fill=value_fill)
        line_y += size + 11
    return max(line_y, y + size + 12)


def _draw_resource_box(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, title: str,
                       items: list[tuple[str, str]], accent):
    _rounded(draw, (x, y, x + w, y + 230), (27, 29, 33), outline=(58, 64, 74), width=2, radius=22)
    draw.text((x + 24, y + 20), title, font=_font(26, True), fill=accent)
    line_y = y + 66
    for label, value in items:
        line_y = _draw_kv(draw, x + 24, line_y, label, value, w - 48, size=21, max_lines=1)


def _safe_text(payload: dict[str, Any], key: str, default: str = "") -> str:
    return str(payload.get(key) or default)


def _resource_items(resource: dict[str, Any]) -> list[tuple[str, str]]:
    items = []
    if resource.get("episode"):
        items.append(("集数：", str(resource.get("episode"))))
    items.extend([
        ("画质：", str(resource.get("quality") or "未知")),
        ("视频：", str(resource.get("video") or "未知")),
        ("音频：", str(resource.get("audio") or "未知")),
        ("大小：", str(resource.get("size") or "未知")),
    ])
    return items


def render_wash_notification_image(payload: dict[str, Any], poster_url: str = "") -> str:
    """Render a wash result notification image and return a temporary JPEG path."""
    success = str(payload.get("status") or "") == "success"
    accent = (68, 220, 122) if success else (255, 178, 45)
    border = (38, 155, 83) if success else (190, 124, 30)
    title = _safe_text(payload, "media_title", "未知媒体")
    subtitle = _safe_text(payload, "subtitle", "洗版通知")

    width, height = 1100, 1480
    canvas = Image.new("RGB", (width, height), (17, 19, 23))
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=(16 + y // 220, 18 + y // 360, 24 + y // 150))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-180, -140, 460, 360), fill=(45, 110, 210, 42))
    glow_draw.ellipse((760, -180, 1260, 360), fill=(20, 155, 110, 34))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(70))).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    draw.text((42, 34), "洗版通知", font=_font(38, True), fill=(255, 255, 255))
    draw.text((42, 88), "整理过程中触发资源质量比较", font=_font(21), fill=(150, 162, 176))

    card = (38, 140, width - 38, height - 42)
    _rounded(draw, card, (23, 25, 28), outline=border, width=3, radius=32)
    _draw_pill(
        draw,
        70,
        178,
        "洗版成功" if success else "洗版失败",
        (23, 84, 45) if success else (92, 59, 20),
        (165, 255, 190) if success else (255, 225, 165),
    )
    draw.text((215, 176), title, font=_font(32, True), fill=(250, 252, 255))
    draw.text((215, 224), subtitle, font=_font(22), fill=(150, 162, 176))

    _draw_poster(canvas, draw, (70, 288, 306, 644), title, poster_url, (55, 120, 220) if success else (210, 125, 35))
    meta_x = 340
    line_y = 298
    meta_lines = [
        ("识别：", _safe_text(payload, "media_type_label", "未知")),
        ("库位：", _safe_text(payload, "library_location", "未知")),
        ("TMDb：", _safe_text(payload, "tmdb_id", "无")),
        ("本次决策：", _safe_text(payload, "decision_text"), accent),
        ("判断原因：", _safe_text(payload, "reason_text")),
    ]
    for item in meta_lines:
        label, value = item[0], item[1]
        value_fill = item[2] if len(item) > 2 else (236, 240, 245)
        line_y = _draw_kv(draw, meta_x, line_y, label, value, width - meta_x - 80, size=23, value_fill=value_fill, max_lines=2)

    box_y = 710
    box_w = (width - 160) // 2
    _draw_resource_box(draw, 70, box_y, box_w, "已入库旧资源", _resource_items(payload.get("old_resource") or {}), (155, 196, 255))
    _draw_resource_box(draw, 92 + box_w, box_y, box_w, "本次整理资源", _resource_items(payload.get("new_resource") or {}), accent)

    y = 990
    footer_font = _font(20)
    label_font = _font(20, True)
    for label, value in [
        ("源媒体文件：", _safe_text(payload, "old_file_name", "未知")),
        ("新媒体文件：", _safe_text(payload, "new_file_name", "未知")),
    ]:
        draw.text((72, y), label, font=label_font, fill=(144, 156, 170))
        offset = int(_text_width(draw, label, label_font)) + 8
        for line in _wrap_text(draw, value, footer_font, width - 150 - offset, max_lines=2):
            draw.text((72 + offset, y), line, font=footer_font, fill=(185, 193, 204))
            y += 30
        y += 8
    draw.text((72, y + 10), f"完成时间：{_safe_text(payload, 'now')}   ·   ChillPoster", font=footer_font, fill=(126, 138, 150))

    tmp = tempfile.NamedTemporaryFile(prefix="chillposter_wash_", suffix=".jpg", delete=False)
    tmp.close()
    canvas.save(tmp.name, "JPEG", quality=88, optimize=True)
    return tmp.name
