"""Ворота молчания надзора стенда (SAM-195): тревога, когда отбивок scripts/stand_watch.py нет дольше порога.

Запуск: `python -m gateway.stand_silence` - ворота-команда workflow stand-silence. Печатает повод одной строкой или
ничего (модель не зовётся). Отбивку читает из файла gateway.stand_report.beat_file(): его пишет шлюз на каждый отчёт
надзора. Молчание считается от более поздней из двух меток: последняя отбивка и старт шлюза (STARTED_ENV, шлюз
ставит её себе в окружение, процесс ворот наследует). Так шлюз, поднявшийся после простоя, не поднимает тревогу по
старой отбивке, пока надзор не успел отчитаться, а файла нет совсем - тревога через тот же порог после старта.

Повод называет время, с которого надзор молчит, а не число секунд: тот же вывод, что в прошлый раз, - не повод
(хэш ворот), и одно молчание будит модель один раз, а не каждую минуту.
Решение (silence) отделено от файла, окружения и часов.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from .env import env_float
from .stand_report import Beat, beat_file, load_beat

# Три отбивки по 30 с (SAM_STAND_WATCH_BEAT_S): отчёт уходит на опросе раз в 15 с, так что между отбивками бывает до
# 45 с; две пропущенные - уже не случайность, а надзор, который не отчитывается
SILENT_S = env_float("SAM_STAND_WATCH_SILENT_S", 90.0)
STARTED_ENV = "SAM_GATEWAY_STARTED_AT"   # время старта шлюза, секунды эпохи; ставит gateway/main.py
TIME_FORMAT = "%H:%M:%S"


def _clock_time(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime(TIME_FORMAT)


def silence(beat: Beat | None, started_at: float | None, now: float, silent_s: float = SILENT_S) -> str:
    """Повод тревоги или пусто. Меток нет вовсе (ворота запущены вне шлюза, отбивок не было) - тоже повод."""
    if beat is None and started_at is None:
        return "надзор стенда не отбивался ни разу: отбивок в файле нет"
    if beat is not None and (started_at is None or beat.at >= started_at):
        since, what = beat.at, "последняя отбивка"
    else:
        since, what = started_at, "отбивок нет с запуска шлюза"
    if now - since <= silent_s:
        return ""
    return f"надзор стенда молчит с {_clock_time(since)} ({what}), дольше {silent_s:.0f} с"


def started_at(environ=None) -> float | None:
    raw = (environ if environ is not None else os.environ).get(STARTED_ENV, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def main() -> int:
    out = silence(load_beat(beat_file()), started_at(), time.time())
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
