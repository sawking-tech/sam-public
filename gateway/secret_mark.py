"""Секреты в контексте быстрой модели (ADR-027): что пометить секретом и что быстрая видит вместо него.

Фраза, где прозвучало «по секрету» или «никому не говори», помечается правилом кода при записи в историю, не дожидаясь
основной модели. Быстрая вместо помеченной записи и вместо факта памяти с меткой SECRET_TAG видит обобщённое описание
без содержания; основная видит всё. Модуль без внешнего мира: только решение по тексту и подмена.
"""
from __future__ import annotations

import os
import re

FIELD = "secret"          # поле записи истории: фраза сказана по секрету
SECRET_TAG = "secret"     # метка факта памяти: содержание - только основной, summary - обобщённое описание
SKILL_TAG_PREFIX = "skill"   # факты-описания скиллов (gateway/skill_memory.py): быстрая по ним не действует
# Слова, по которым фраза - секрет; свои - через запятую в SAM_SECRET_TRIGGERS
# Имена домашних («не говори <имя>») - в SAM_SECRET_TRIGGERS из .env, не здесь; «между нами» без «только» - обычная
# речь («расстояние между нами»)
DEFAULT_TRIGGERS = ("по секрету", "никому не говори", "никому не рассказывай", "только между нами", "это секрет")
PLACEHOLDER = "[личное, сказано по секрету: тема закрыта, говорить о ней только с тем, кто сказал]"
SECRET_FACT = "Личное, только с тем, кто сказал: {summary}"


def triggers() -> tuple[str, ...]:
    raw = os.getenv("SAM_SECRET_TRIGGERS", "")
    return tuple(t.strip().lower() for t in raw.split(",") if t.strip()) or DEFAULT_TRIGGERS


def is_secret_phrase(text: str, words: tuple[str, ...] | None = None) -> bool:
    """Фраза с секретом по словам-триггерам; регистр и лишние пробелы не важны."""
    norm = re.sub(r"\s+", " ", (text or "").lower())
    return any(w in norm for w in (words or triggers()))


def fast_fact(text: str, tags: list[str], summary: str) -> str | None:
    """Факт памяти для быстрой: описание скилла - нет; секрет - обобщённое описание, а без своего описания (summary
    пуст или это начало самого текста, так remember заполняет его по умолчанию) - нет; иначе как есть."""
    tags = [str(t).strip().lower() for t in tags or []]
    if any(t.startswith(SKILL_TAG_PREFIX) for t in tags):
        return None
    if SECRET_TAG in tags:
        own = (summary or "").strip()
        return SECRET_FACT.format(summary=own) if own and not (text or "").startswith(own) else None
    return text


def hidden(history: list[dict], i: int) -> bool:
    """Запись истории закрыта для быстрой: помеченная фраза и ответ SAM сразу за ней (он часто повторяет секрет)."""
    if history[i].get(FIELD):
        return True
    prev = history[i - 1] if i > 0 else {}
    return history[i].get("role") == "assistant" and prev.get("role") == "user" and bool(prev.get(FIELD))
