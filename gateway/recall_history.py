"""Давнее из архива (SAM-228, compression.md «Свойства контекста» и «Два канала»).

Сжатая беседа лежит в архиве (`sessions_archive`). Два пути вернуть её в ход, оба без копий в истории:
- блок к фразе: ход основной модели полнотекстом ищет в архиве своей сессии и получает несколько реплик рядом с
  памятью (archive_block);
- запись-ссылка: основная модель инструментом «поднять историю» пишет в историю одну запись с id найденных реплик
  архива; при сборке контекста она разворачивается в эти реплики по времени, между несмежными кусками - пометка
  пропуска (expand). Сжатие считает ссылку мыслью, она уходит из окна сама, и расширение сходит на нет.

Быстрая модель архива не получает: в архиве, записанном до пометки секретов, они не помечены. Развёрнутые реплики
помечены полем RECALLED, сборка промпта быстрой их пропускает. Модуль без внешнего мира: поиск и чтение архива
приходят параметрами.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable

from .env import env_float, env_int

RECALL_ROLE = "recall"          # запись-ссылка в истории сессии
IDS_FIELD = "recall_ids"        # id реплик архива, на которые она ссылается
RECALLED = "recalled"           # пометка развёрнутой из архива реплики в контексте хода
# Сколько реплик архива разворачивает одна ссылка: окно сверх обычного (compression.md, SAM_RECALL_WINDOW)
RECALL_WINDOW = env_int("SAM_RECALL_WINDOW", 20)
# Реплик архива рядом с фразой: подсказка о давнем, а не пересказ - больше трёх мешает фразе
ARCHIVE_HITS = env_int("SAM_ARCHIVE_RAG_LIMIT", 3)
# Предел ожидания поиска в архиве к фразе: ход не ждёт его дольше поиска по памяти голосового хода (1,5 с)
ARCHIVE_TIMEOUT_S = env_float("SAM_ARCHIVE_TIMEOUT_S", 1.5)
# Пауза между соседними репликами больше этой - куски несмежные, между ними пометка пропуска; 5 минут - перерыв
# в живом разговоре
GAP_S = env_float("SAM_RECALL_GAP_S", 300.0)
GAP_MARK = "[… пропуск в поднятой истории …]"
START_MARK = "[поднято из архива, это давний разговор, не сейчас]"
ARCHIVE_HEADER = "Из прошлого разговора (архив, дословно):"
ARCHIVE_LINE_CHARS = 200   # строка архива к фразе: суть реплики, полный текст - инструментом recall_history
TALK_ROLES = ("user", "assistant")
SPEAKERS = {"user": "человек", "assistant": "SAM"}

Search = Callable[[str, str, int], list[dict]]          # (запрос, сессия, сколько) -> реплики архива
Fetch = Callable[[Iterable], list[dict]]                 # (id реплик архива) -> реплики


def _talk(docs: Iterable[dict]) -> list[dict]:
    return [d for d in docs if d.get("role") in TALK_ROLES and str(d.get("content") or "").strip()]


def _hhmm(ts) -> str:
    return ts.strftime("%H:%M") if isinstance(ts, datetime) else ""


def archive_block(session: str, query: str, search: Search, limit: int = ARCHIVE_HITS) -> str:
    """Несколько реплик архива сессии по фразе - блок к фразе для основной; ничего нет - пусто."""
    if not (query or "").strip():
        return ""
    hits = _talk(search(query, session, limit))[:limit]
    if not hits:
        return ""
    lines = [f"- [{_hhmm(d.get('timestamp'))}] {d.get('who') or SPEAKERS.get(d['role'], d['role'])}: "
             f"{' '.join(str(d['content']).split())[:ARCHIVE_LINE_CHARS]}" for d in hits]
    return ARCHIVE_HEADER + "\n" + "\n".join(lines)


def expand(history: list[dict], fetch: Fetch, window: int = RECALL_WINDOW, gap_s: float = GAP_S) -> list[dict]:
    """История с развёрнутыми записями-ссылками: реплики архива по времени, пометка пропуска между несмежными
    кусками, у каждой - RECALLED. Ссылок нет - история как есть."""
    ids = [i for h in history if h.get("role") == RECALL_ROLE for i in (h.get(IDS_FIELD) or [])]
    if not ids:
        return history
    # двойник в окне (dedup_recent кладёт в архив копию, оригинал остаётся) не повторяется
    seen_ids = {h.get("_id") for h in history}
    seen_text = {str(h.get("content") or "").strip() for h in history}
    pieces = sorted((d for d in _talk(fetch(ids))
                     if d.get("_orig_id") not in seen_ids and str(d["content"]).strip() not in seen_text),
                    key=lambda d: d.get("timestamp") or datetime.min)[-window:]
    recalled, prev = [], None
    for d in pieces:
        content = str(d["content"])
        ts = d.get("timestamp")
        if not recalled:
            content = f"{START_MARK}\n{content}"
        elif isinstance(ts, datetime) and isinstance(prev, datetime) and (ts - prev).total_seconds() > gap_s:
            content = f"{GAP_MARK}\n{content}"
        recalled.append({**d, "content": content, RECALLED: True})
        prev = ts
    return sorted([*history, *recalled], key=lambda d: d.get("timestamp") or datetime.min)


def recall_impl(params: dict, session: str, search: Search, write: Callable[[dict], None],
                window: int = RECALL_WINDOW) -> str:
    """Инструмент «поднять историю»: params.query - тема; запись-ссылка в историю, реплики архива - в контекст."""
    query = str(params.get("query") or "").strip()
    if not query:
        return "Нужна тема: о чём поднять прошлый разговор"
    hits = _talk(search(query, session, window))
    if not hits:
        return f"В архиве разговора о «{query}» ничего нет"
    write({"role": RECALL_ROLE, "content": f"Вернулись к прошлому разговору: {query} (реплик из архива: {len(hits)})",
           IDS_FIELD: [d["_id"] for d in hits]})
    return f"Прошлый разговор о «{query}» поднят, реплик: {len(hits)}; они в контексте следующих ходов дословно"
