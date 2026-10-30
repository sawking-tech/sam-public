"""Долгие паузы поставщиков LLM переживают перезапуск шлюза (SAM-258).

Пауза 403 держится час: без файла после рестарта мёртвый ключ снова первый в выборе и каждый старт стоит холостой
попытки. Хранится настенное время окончания (монотонные часы после рестарта другие). Короткие паузы (429, 5xx) не
пишутся: они кончаются раньше, чем стоит трогать диск. Часы и путь приходят аргументами, чтобы проверять без файловой
системы шлюза.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FILE_ENV = "SAM_COOLDOWN_FILE"
DEFAULT_FILE = "data/llm_cooldowns.json"     # data/ не в git (личное и состояние машины)
PERSIST_MIN_S = 600                          # короче - не пишется: 429 и 5xx кончаются за секунды-минуты


def default_path(repo: Path, environ=None) -> Path:
    raw = (environ if environ is not None else os.environ).get(FILE_ENV) or DEFAULT_FILE
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def read_until(path: Path) -> dict[str, float]:
    """source -> настенное время окончания паузы; нет файла или он битый - пусто."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {s: float(t) for s, t in data.items() if isinstance(s, str) and isinstance(t, (int, float))} \
        if isinstance(data, dict) else {}


def remember(path: Path, source: str, cooldown_s: float, wall_now: float) -> None:
    """Записать паузу, если она долгая; просроченные записи заодно убираются."""
    if cooldown_s < PERSIST_MIN_S:
        return
    entries = {s: t for s, t in read_until(path).items() if t > wall_now}
    entries[source] = wall_now + cooldown_s
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries), encoding="utf-8")
    except OSError as e:
        print(f"[Cooldown] не записан {path}: {e}")


def restore(path: Path, wall_now: float, mono_now: float) -> dict[str, float]:
    """source -> монотонное время окончания для ещё не кончившихся пауз."""
    return {s: mono_now + (t - wall_now) for s, t in read_until(path).items() if t > wall_now}
