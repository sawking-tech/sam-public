"""Контракт завершения агента (SAM-237): итог принимается с артефактом, словам о себе не верят.

Задача кода (в описании коммит, рабочее место, ветка agent/) обязана назвать хэш коммита, который существует в репозитории;
остальным хватает текста не короче порога. Пустое «Готово.» не закрывает задачу: статус unverified и причина создателю.
Чистые функции: проверку существования хэша (git) приносит вызывающий.
"""
from __future__ import annotations

import os
import re
from typing import Callable

MIN_CHARS_ENV = "SAM_RESULT_MIN_CHARS"
MIN_CHARS_DEFAULT = 15       # короче - «Сделал, ок»: описать нечего; «Нашла 2 новости.» (16) и «3 новости за неделю» (19) проходят
EMPTY_WORDS = frozenset({"готово", "сделано", "done", "ok", "ок", "выполнено"})
CODE_TASK = re.compile(r"коммит|commit|workspace|рабочее место|agent/|ветк[аеуи]", re.IGNORECASE)
COMMIT_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")

REASON_EMPTY = "итог пустой: «{text}» не описывает, что сделано; верни факты, пути и результат проверки"
REASON_SHORT = "итог короче {limit} знаков: приведи, что сделано и чем подтверждено"
REASON_NO_HASH = "задача кода без хэша коммита в итоге: закоммить явными путями в свою ветку и назови хэш"
REASON_BAD_HASH = "хэш {hash} не найден в репозитории: проверь коммит и назови настоящий"

CommitExists = Callable[[str], bool]


def min_chars(environ=None) -> int:
    raw = (environ if environ is not None else os.environ).get(MIN_CHARS_ENV)
    return int(raw) if raw and raw.isdigit() else MIN_CHARS_DEFAULT


def check(description: str, result: str, commit_exists: CommitExists, environ=None) -> str | None:
    """None - итог принят; иначе одна строка причины."""
    text = (result or "").strip()
    bare = re.sub(r"[\W_]+", " ", text.lower()).strip()
    if not bare or bare in EMPTY_WORDS:
        return REASON_EMPTY.format(text=text or "")
    if CODE_TASK.search(description or ""):
        hashes = COMMIT_HASH.findall(text)
        if not hashes:
            return REASON_NO_HASH
        missing = [h for h in hashes if not commit_exists(h)]
        return REASON_BAD_HASH.format(hash=missing[0]) if len(missing) == len(hashes) else None
    limit = min_chars(environ)
    return REASON_SHORT.format(limit=limit) if len(text) < limit else None
