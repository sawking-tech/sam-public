"""Вид сессии для монитора по признакам из данных: канал человека, агент или проверка.

Канал человека - голос, терминал (chat.py), устройства каталога и устройство этой машины; агент - `task:<id>`;
остальное - проверки: живые прогоны и тесты, которые ставят свой X-Device-ID. Явная метка проверки - префикс
CHECK_PREFIX, его сессии уборка снимает сама по сроку (gateway/session_purge.py). Здесь только решение, без Mongo.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Iterable

KIND_PERSON, KIND_AGENT, KIND_CHECK = "person", "agent", "check"
TASK_PREFIX = "task:"
# Метка очевидно тестовых данных: так называют сессии живых проверок и интеграционных тестов (docs/features/stand.md)
CHECK_PREFIX = "test-do-not-use-"
VOICE_SESSION = "voice"
# Каналы людей кроме устройств: голос и терминальный чат (chat.py --device, по умолчанию cli)
PERSON_SESSIONS_DEFAULT = "voice,cli"
# Удалять нельзя: канал голоса и сессию шлюза для запросов без X-Device-ID (gateway/main.py «default»); устройство
# этой машины добавляет вызывающий (catalog.LOCAL_DEVICE)
PROTECTED_SESSIONS_DEFAULT = f"{VOICE_SESSION},default"


def _env_set(name: str, default: str) -> frozenset[str]:
    """Список через запятую в нижнем регистре; пустая переменная - то же, что не заданная (защита не снимается)."""
    raw = os.getenv(name, "").strip() or default
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def person_sessions(devices: Iterable[str]) -> frozenset[str]:
    """Каналы людей: SAM_PERSON_SESSIONS (голос, терминал) и устройства каталога."""
    return _env_set("SAM_PERSON_SESSIONS", PERSON_SESSIONS_DEFAULT) | frozenset(d.lower() for d in devices if d)


def protected_sessions(local_device: str) -> frozenset[str]:
    """Сессии, которые монитор не удаляет: SAM_PROTECTED_SESSIONS (голос) и устройство этой машины."""
    return _env_set("SAM_PROTECTED_SESSIONS", PROTECTED_SESSIONS_DEFAULT) | {local_device.lower()}


def is_protected(session_id: str, protected: frozenset[str]) -> bool:
    return session_id.lower() in protected


def delete_needs_key(session_id: str) -> bool:
    """Сессию задачи агента удаляет любой, кого пускает шлюз; любую другую - только с ключом команд или ведущего."""
    return not session_id.lower().startswith(TASK_PREFIX)


def classify(session_id: str, people: frozenset[str]) -> str:
    """Регистр не важен: устройство из каталога хранится в нижнем, X-Device-ID приходит как его прислали."""
    sid = session_id.lower()
    if sid.startswith(TASK_PREFIX):
        return KIND_AGENT
    if sid in people and not sid.startswith(CHECK_PREFIX):
        return KIND_PERSON
    return KIND_CHECK


def merge_summaries(*parts: Iterable[dict]) -> list[dict]:
    """Сводки одной сессии из истории и архива - одной строкой: последняя запись - самая поздняя, записей - сумма."""
    out: dict[str, dict] = {}
    for rows in parts:
        for r in rows:
            cur = out.setdefault(r["id"], {"id": r["id"], "last": None, "count": 0})
            last = r.get("last")
            if last and (cur["last"] is None or last > cur["last"]):
                cur["last"] = last
            cur["count"] += r.get("count", 0)
    return list(out.values())


def describe(rows: Iterable[dict], people: frozenset[str], protected: frozenset[str], default: str,
             now: datetime, fresh: timedelta) -> list[dict]:
    """Строки селектора из сводки сессий ({id, last, count}), свежие первыми. stale - проверка или агент без записей
    за `fresh`: монитор их сворачивает. Каналы людей не устаревают: голос молчит ночью, но остаётся каналом."""
    out = []
    for r in sorted(rows, key=lambda r: r.get("last") or datetime.min, reverse=True):
        sid, last = r["id"], r.get("last")
        kind = classify(sid, people)
        out.append({
            "id": sid, "kind": kind, "count": r.get("count", 0),
            "last": last.isoformat() + "Z" if last else None,
            "stale": kind != KIND_PERSON and (last is None or now - last > fresh),
            "protected": is_protected(sid, protected), "default": sid == default,
        })
    if default not in {o["id"] for o in out}:   # устройство этой машины - адресат по умолчанию, даже без истории
        out.append({"id": default, "kind": KIND_PERSON, "count": 0, "last": None, "stale": False,
                    "protected": is_protected(default, protected), "default": True})
    return out
