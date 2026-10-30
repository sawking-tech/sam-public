"""Ворота workflow «память» (SAM-94, SAM-96): что SAM услышала после прошлой проверки или своей записи в память.

Счётчик Hermes Agent (agent/background_review.py, nudge по числу ходов с последней записи памяти) здесь - курсор
по времени: позже прошлого срабатывания, позже последней записи в память (записала сама - счёт с нуля) и не
раньше LOOKBACK_S назад (первый запуск и долгий простой шлюза не тащат весь журнал). Пусто - модель не зовётся.

Слышанное - фразы `voice-in.jsonl` шины голоса, все, а не только сказанные SAM: разговор не к SAM в историю
сессии не попадает, а важное в нём бывает (например, чей-то день рождения). Разбор чистый, файл и Mongo
читают функции read_*.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .env import env_float, env_int

LOOKBACK_S = env_float("SAM_MEMORY_REVIEW_LOOKBACK_S", 3600.0)   # два периода workflow «память» (30 мин)
MAX_CHARS = env_int("SAM_MEMORY_REVIEW_MAX_CHARS", 4000)         # повод - в контекст модели; свежее важнее
TAIL_BYTES = 512 * 1024   # хвост журнала: ~2000 фраз по ~250 байт, больше часа разговора


def _parse_ts(raw) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def cursor(now: datetime, last_run: datetime | None, memory_written: datetime | None,
           lookback_s: float = LOOKBACK_S) -> datetime:
    return max(t for t in (now - timedelta(seconds=lookback_s), last_run, memory_written) if t is not None)


def heard_since(phrases: list[dict], since: datetime, max_chars: int = MAX_CHARS) -> str:
    """Фразы людей позже since строками «ЧЧ:ММ кто: текст»; без черновиков и эха. Не влезло - старые отбрасываются."""
    lines = []
    for p in phrases:
        ts = _parse_ts(p.get("ts"))
        text = str(p.get("text") or "").strip()
        if ts is None or ts <= since or not text or p.get("partial") or p.get("echo"):
            continue
        lines.append(f"{ts.astimezone():%H:%M} {p.get('who') or 'неизвестный'}: {text}")
    out, size = [], 0
    for line in reversed(lines):
        if size + len(line) > max_chars:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(reversed(out))


def read_phrases(path: Path, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """Хвост журнала фраз; нет файла - []. Битая строка пропускается."""
    try:
        with path.open("rb") as f:
            offset = max(0, path.stat().st_size - tail_bytes)
            f.seek(offset)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[1:] if offset else lines:   # с середины файла первая строка обрезана
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_memory_written(memory_repo) -> datetime | None:
    """Время последней записи в память SAM (`_memories.created_at`, UTC без пояса)."""
    docs = memory_repo.find_many({}, sort=[("created_at", -1)], limit=1)
    ts = docs[0].get("created_at") if docs else None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None
