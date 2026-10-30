"""Web search tool (SAM-40).

HTTP-only поиск без браузера:
  - Для видео — Invidious API (несколько публичных инстансов)
  - Google Custom Search если есть API ключ
  - DuckDuckGo HTML fallback (без ключа)

Возвращает форматированный список результатов: title + url.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import unquote


_VIDEO_KEYWORDS = {
    "youtube", "видео", "клип", "song", "music", "watch", "фильм", "сериал",
}

_INVIDIOUS_INSTANCES = [
    "https://invidious.io.lol",
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
]


def _via(transport: Any) -> dict:
    """Аргумент transport для httpx.AsyncClient: только заданный, иначе клиент по умолчанию (сеть)."""
    return {"transport": transport} if transport is not None else {}


def _looks_like_video(query: str, intent: str) -> bool:
    text = (query + " " + intent).lower()
    return intent == "youtube" or any(k in text for k in _VIDEO_KEYWORDS)


async def _try_invidious(query: str, transport: Any = None) -> list[str]:
    import httpx
    for base in _INVIDIOUS_INSTANCES:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True, **_via(transport)) as client:
                r = await client.get(
                    f"{base}/api/v1/search",
                    params={"q": query, "type": "video", "page": 1},
                )
                if r.status_code != 200:
                    continue
                results = []
                for item in r.json()[:5]:
                    vid = item.get("videoId", "")
                    title = item.get("title", "")
                    if vid:
                        results.append(f"{title} → https://www.youtube.com/watch?v={vid}")
                if results:
                    return results
        except Exception:
            continue
    return []


async def _try_google(query: str, is_video: bool, transport: Any = None) -> list[str]:
    import httpx
    google_key = os.getenv("GOOGLE_API_KEY")
    google_cx = os.getenv("GOOGLE_CX")
    if not (google_key and google_cx):
        return []
    google_query = f"{query} site:youtube.com" if is_video else query
    try:
        async with httpx.AsyncClient(timeout=10, **_via(transport)) as client:
            r = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": google_key, "cx": google_cx,
                        "q": google_query, "num": 5},
            )
            if r.status_code != 200:
                return []
            return [
                f"{item['title']} → {item['link']}"
                for item in r.json().get("items", [])
            ]
    except Exception as e:
        print(f"[WebSearch] Google error: {e}")
        return []


async def _try_duckduckgo(query: str, transport: Any = None) -> list[str]:
    import httpx
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}, **_via(transport),
        ) as client:
            r = await client.get("https://html.duckduckgo.com/html/",
                                  params={"q": query})
            if r.status_code != 200:
                return []
            results = []
            links = re.findall(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)', r.text,
            )
            for href, title in links[:5]:
                if "uddg=" in href:
                    href = unquote(href.split("uddg=")[-1].split("&")[0])
                results.append(f"{title.strip()} → {href}")
            return results
    except Exception as e:
        return [f"Поиск недоступен: {e}"]


async def search_web_impl(params: dict, transport: Any = None) -> str:
    """Поиск в интернете: Invidious (для видео) → Google → DuckDuckGo.

    transport - транспорт httpx для всех запросов; None - сеть (тесты подставляют httpx.MockTransport)."""
    query = params.get("query", "")
    if not query:
        return "Нужен query — поисковый запрос"
    intent = params.get("intent", "")

    is_video = _looks_like_video(query, intent)
    results: list[str] = []

    if is_video:
        results = await _try_invidious(query, transport)

    if not results:
        results = await _try_google(query, is_video, transport)

    if not results:
        results = await _try_duckduckgo(query, transport)

    if not results:
        return f"Ничего не найдено по запросу: {query}"
    return "\n".join(results)
