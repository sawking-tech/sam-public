"""Image processing для vision-моделей.

Задача: screenshot с 2K/4K монитора → набор HD-кадров для LLM.
- Если картинка нормальной формы и больше HD → пересжать в HD
- Если очень длинная (скрин страницы) → нарезать на чанки, каждый ≤ HD

Используется в gateway/tools/mcp.py перед возвратом результата vision-моделям.
"""

from __future__ import annotations

import base64
import io
import math
import re
from typing import Iterable

try:
    from PIL import Image  # type: ignore
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

HD_W, HD_H = 1920, 1080
ASPECT_SPLIT_RATIO = 2.5     # длиннее этого по короткой стороне → режем
TARGET_CHUNK_ASPECT = 1.5    # каждый кусок ~ 1.5x длиннее короткой стороны
DATA_URL_RE = re.compile(r"data:image/[\w+\-]+;base64,([A-Za-z0-9+/=]+)")


def _split_long(img: "Image.Image") -> list["Image.Image"]:
    """Разрезать картинку с экстремальным aspect ratio на чанки."""
    W, H = img.size
    long_side, short_side = max(W, H), min(W, H)
    aspect = long_side / max(short_side, 1)
    if aspect <= ASPECT_SPLIT_RATIO:
        return [img]
    n = max(2, math.ceil(aspect / TARGET_CHUNK_ASPECT))
    parts: list = []
    if H >= W:
        slice_h = H // n
        for i in range(n):
            top = i * slice_h
            bot = (i + 1) * slice_h if i < n - 1 else H
            parts.append(img.crop((0, top, W, bot)))
    else:
        slice_w = W // n
        for i in range(n):
            left = i * slice_w
            right = (i + 1) * slice_w if i < n - 1 else W
            parts.append(img.crop((left, 0, right, H)))
    return parts


def _to_hd(img: "Image.Image") -> "Image.Image":
    """Уменьшить картинку чтобы влезла в HD-bbox (1920x1080)."""
    W, H = img.size
    if W <= HD_W and H <= HD_H:
        return img
    img = img.copy()
    img.thumbnail((HD_W, HD_H), Image.LANCZOS)
    return img


def _encode(img: "Image.Image") -> str:
    """PIL → data URL PNG."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def process_screenshot_b64(b64_str: str) -> list[str]:
    """Принять base64 PNG → вернуть список HD data URL'ов.

    Возвращает 1 урл если картинка нормальная и ≤ HD.
    Возвращает 2-4 урла если длинная или большая 2K/4K.
    Если PIL недоступен — возвращает исходный data URL без обработки.
    """
    if not _PIL_OK:
        return [f"data:image/png;base64,{b64_str}"]
    try:
        raw = base64.b64decode(b64_str)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        print(f"[image_utils] decode error: {e}")
        return [f"data:image/png;base64,{b64_str}"]

    parts = _split_long(img)
    return [_encode(_to_hd(p)) for p in parts]


def process_text_with_images(text: str) -> str:
    """Найти все data:image/... в тексте и заменить их на HD-сжатые.

    Каждая оригинальная картинка может развернуться в 1-4 фрагмента —
    они идут подряд, разделены '\\n[chunk N/M]\\n'.
    """
    if not _PIL_OK:
        return text

    def _replace(match: "re.Match") -> str:
        b64 = match.group(1)
        urls = process_screenshot_b64(b64)
        if len(urls) == 1:
            return urls[0]
        return "\n".join(
            f"[chunk {i+1}/{len(urls)}]\n{u}" for i, u in enumerate(urls)
        )

    return DATA_URL_RE.sub(_replace, text)


def extract_data_urls(text: str) -> list[str]:
    """Извлечь все data:image/... URL'ы из текста."""
    return [m.group(0) for m in DATA_URL_RE.finditer(text)]
