"""Заметки основной модели о разговоре (SAM-131): кратко тема, линия и прогноз, и ветки развития - кто из участников
что вероятно скажет и короткий возможный ответ на это. Ветки - подсказка быстрой модели, а не сценарий: отвечает она,
как считает лучше.

Основная модель ведёт их для себя шагом workflow dialogue-notes: dialogue_context отдаёт свежий кусок разговора и
прежние заметки, шаг ask обновляет их, update_dialogue_notes сохраняет. Читают их быстрая модель голоса (контекст
хода) и проверка её ответов. Заметки - JSON с полями FIELDS; разбор и вид в контексте - чистые функции, хранилище и
история приходят параметрами.
"""
from __future__ import annotations

import json
import re
from typing import Callable

import json_repair

from ..repos.dialogue_notes_repo import FIELDS

NOTES_HEADER = ("Твои заметки о разговоре на сейчас и возможные ходы беседы. Это подсказка, а не сценарий: "
                "отвечай, как считаешь лучше.")
FIELD_TITLES = {"topic": "Тема", "line": "Линия", "forecast": "Дальше вероятно", "digest": "Выжимка"}
# Быстрая модель (ADR-027): линия и выжимка - выше диалога (блоки 4-5), заготовки - после него (блок 8)
LINE_HEADER = "Заметки основной модели о разговоре (линия и выжимка нужного по теме):"
PREPARED_HEADER = ("Заготовки - подсказка, а не текст для зачитывания: можно взять одну и подправить под фразу, "
                   "соединить несколько или ответить своим вариантом, если он точнее; правила честности действуют.")
REBUILDING = "rebuilding"   # основная перестраивает линию после смены темы: быстрая отходит в сторону
BRANCH_LINE = "Если {who}скажет «{says}» - можно ответить: «{reply}»"
SOMEONE = ""   # участник не назван - «Если скажет ...»
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
# Подпись записи истории в контексте заметок: сводка сжатия - не реплика человека (SAM-219), иначе модель читала
# «Человек: я четыре раза вызывала sync_skills» как слова человека
HUMAN, SUMMARY_ROLE = "Человек", "summary"
SPEAKERS = {"assistant": "SAM", SUMMARY_ROLE: "[сводка]"}


def _json_of(raw: str):
    """JSON заметок из ответа модели. Целый - как есть; битый или обрезанный пределом ответа - чинится json-repair:
    модель путает ключ ветки или упирается в потолок посреди последней ветки, а тема и линия при этом целы (SAM-218)."""
    start = (raw or "").find("{")
    if start < 0:
        return None
    m = _JSON_BLOCK.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    return json_repair.loads(raw[start:])


def parse_notes(raw: str) -> dict | None:
    """Ответ модели -> поля заметок; JSON в тексте (в том числе в ```), ветки - список {who, says, reply}. Нет - None."""
    data = _json_of(raw)
    if not isinstance(data, dict) or not any(data.get(k) for k in FIELDS):
        return None
    notes = {k: str(data.get(k) or "").strip() for k in FIELDS if k not in ("branches", REBUILDING)}
    notes[REBUILDING] = data.get(REBUILDING) is True or str(data.get(REBUILDING)).strip().lower() == "true"
    notes["branches"] = [b for b in (_branch(x) for x in (data.get("branches") or [])
                                     if isinstance(data.get("branches"), list)) if b]
    return notes


def _branch(raw) -> dict | None:
    """Ветка: кто (может не быть), что вероятно скажет, короткий ответ; без реплики или ответа - не ветка."""
    if not isinstance(raw, dict):
        return None
    says, reply = str(raw.get("says") or "").strip(), str(raw.get("reply") or "").strip()
    return {"who": str(raw.get("who") or "").strip(), "says": says, "reply": reply} if says and reply else None


def _field_lines(doc: dict) -> list[str]:
    return [f"{title}: {doc[key]}" for key, title in FIELD_TITLES.items() if doc.get(key)]


def _branch_lines(doc: dict) -> list[str]:
    return [BRANCH_LINE.format(who=f"{b['who']} " if b.get("who") else SOMEONE, says=b["says"], reply=b["reply"])
            for b in doc.get("branches") or []]


def render_line(doc: dict | None) -> str:
    """Линия разговора и выжимка для быстрой (блоки 4-5); нет - пусто, на месте встаёт постоянная строка."""
    lines = _field_lines(doc or {})
    return (LINE_HEADER + "\n" + "\n".join(lines)) if lines else ""


def render_prepared(doc: dict | None) -> str:
    """Заготовки для быстрой (блок 8) после диалога; нет - пусто, блок не показывается."""
    lines = _branch_lines(doc or {})
    return (PREPARED_HEADER + "\n" + "\n".join(lines)) if lines else ""


def is_rebuilding(doc: dict | None) -> bool:
    """Основная поставила признак «перестраиваю» (смена темы): ход ведёт она, пока не обновит заметки."""
    return bool((doc or {}).get(REBUILDING))


def render_notes(doc: dict | None) -> str:
    """Заметки текстом для контекста модели; нет заметок - пусто."""
    if not doc:
        return ""
    lines = _field_lines(doc) + _branch_lines(doc)
    return (NOTES_HEADER + "\n" + "\n".join(lines)) if lines else ""


def update_impl(params: dict, session: str, put: Callable[[str, dict], int]) -> str:
    """params: notes - ответ шага ask с JSON заметок."""
    notes = parse_notes(str(params.get("notes") or ""))
    if notes is None:
        return "Заметки не разобраны: ждали JSON с полями " + ", ".join(FIELDS)
    version = put(session, notes)
    return f"OK: заметки о разговоре, версия {version}" if version else "Заметки не записались: нет MongoDB"


def context_impl(params: dict, session: str, recent: Callable[[str, int], list[dict]],
                 current: Callable[[str], dict | None], default_turns: int,
                 own_work: Callable[[], str] = lambda: "") -> str:
    """Свежий кусок разговора (реплики людей и SAM по порядку), своя работа SAM для выжимки и прежние заметки - вход
    шагу ask."""
    try:
        turns = int(params.get("turns") or default_turns)
    except (TypeError, ValueError):
        turns = default_turns
    lines = []
    for m in recent(session, turns):
        who = m.get("who") or SPEAKERS.get(m.get("role"), HUMAN)
        if m.get("role") == SUMMARY_ROLE and m.get("time_range"):
            who = f"{who} {m['time_range']}"
        text = str(m.get("content") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    if not lines:
        return ""
    old = render_notes(current(session)) or "Прежних заметок нет."
    work = own_work()
    return ("Разговор, последние реплики:\n" + "\n".join(lines) + "\n\n" + (work + "\n\n" if work else "") + old)
