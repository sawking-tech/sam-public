"""Память на входе (SAM-101, ADR-018): поиск по памяти идёт до развилки основной модели и едет вместе со входом.

Голосовой ход, чат монитора и inject зовут `at_input` одинаково. Модели в шлюзе успевшие факты идут в контекст
хода, опоздавшие пропадают, как раньше. Модели вне шлюза вход уходит в её `deliver` (lead-in.jsonl) с полем
`facts`; поиск, не успевший к сроку, вход не держит: факты догоняют отдельной записью `source: rag` со ссылкой
`at` на время входа. Голос сессия Claude Code слышит сама (тикер читает voice-in.jsonl), поэтому факты к фразе
всегда идут такой записью.

Логика не знает про файл и сокет: поиск и доставка передаются параметрами из корня композиции (gateway/agent.py).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .env import env_float, env_int

# Срок поиска до входа. Не замерен; на CPU-эмбеддере поиск обычно короче секунды. Имя переменной прежнее:
# срок был у голосового хода и остался общим для всех входов
TIMEOUT_S = env_float("VOICE_RAG_TIMEOUT_S", 1.5)
FACTS = "facts"            # поле фактов в записи deliver и в тике
LATE_SOURCE = "rag"        # source записи с фактами к уже доставленному входу
# Тик должен читаться глазами: длинная заметка памяти режется, суть в начале. 240 знаков - две-три фразы
FACT_CHARS = env_int("SAM_RECALL_FACT_CHARS", 240)
# Цитата фразы в записи rag: по ней видно, к чему факты, даже если время входа не совпало с тиком
PHRASE_CHARS = 80
_BULLET = "- "             # строка факта у rag.get_rag_context


@dataclass
class Found:
    facts: str                                # успевшие факты строками «- факт»; пусто - нет или не успели
    late: asyncio.Future | None = None        # поиск, не успевший к сроку; None - успел, упал или не нужен


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fact_lines(facts: str, max_chars: int = FACT_CHARS) -> list[str]:
    """Факты для тика: по строке на факт, без маркера списка, длинный обрезан."""
    out = []
    for line in facts.splitlines():
        line = line.strip()
        if line.startswith(_BULLET):
            line = line[len(_BULLET):].strip()
        if line:
            out.append(line if len(line) <= max_chars else line[:max_chars - 1].rstrip() + "…")
    return out


def _result(task: asyncio.Future) -> str:
    if task.cancelled():
        return ""
    exc = task.exception()
    if exc is not None:
        print(f"[Recall] поиск по памяти упал, вход без неё: {exc}")
        return ""
    return task.result() or ""


async def search_in_time(search: Callable[[str, int], str], query: str, limit: int, timeout_s: float) -> Found:
    """Поиск в потоке не дольше срока: эмбеддер, Qdrant и Mongo синхронны (SAM-74). Опоздавший поиск не
    отменяется - его итог в Found.late."""
    if not query:
        return Found("")
    task = asyncio.ensure_future(asyncio.to_thread(search, query, limit))
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        print(f"[Recall] память не успела за {timeout_s:g} с, вход уходит без неё")
        return Found("", late=task)
    return Found(_result(task))


def late_record(facts: str, at: str, phrase: str, session: str) -> dict:
    """Запись фактов к уже доставленному входу: at - время входа, phrase - его начало."""
    return {"source": LATE_SOURCE, "at": at, "phrase": phrase[:PHRASE_CHARS], "session": session,
            FACTS: fact_lines(facts)}


def hand_over(deliver: Callable[[dict], None], found: Found, *, message: dict | None, at: str, phrase: str,
              session: str) -> None:
    """Вход и найденное - модели вне шлюза. message - сам вход (чат, inject) с временем at и успевшими фактами;
    None - вход приходит модели своим путём (голос), успевшие факты идут записью rag. Опоздавшие - записью rag,
    когда поиск закончится; пустой итог не пишется."""
    if message is not None:
        deliver({"ts": at, **message, **({FACTS: fact_lines(found.facts)} if found.facts else {})})
    elif found.facts:
        deliver(late_record(found.facts, at, phrase, session))
    if found.late is not None:
        def _catch_up(task: asyncio.Future) -> None:
            facts = _result(task)
            if facts:
                deliver(late_record(facts, at, phrase, session))
        found.late.add_done_callback(_catch_up)


async def at_input(model, search: Callable[[str, int], str], query: str, *, limit: int, timeout_s: float,
                   at: str, phrase: str, session: str, message: dict | None = None) -> str:
    """Поиск по памяти на входе, до развилки основной модели (model - gateway/main_model.ConcreteModel).
    Модели вне шлюза вход и факты уходят в её deliver; возвращает успевшие факты - контекст модели в шлюзе."""
    found = await search_in_time(search, query, limit, timeout_s)
    if not model.in_gateway:
        hand_over(model.deliver, found, message=message, at=at, phrase=phrase, session=session)
    elif found.late is not None:
        found.late.add_done_callback(_result)   # ход ушёл без памяти; сбой поиска - строкой в лог, не варнингом
    return found.facts
