"""Живость процессов голосового слоя файлом в шине (SAM-195): у слуха и моста нет HTTP, их health check - файл.

Процесс раз в WRITE_EVERY_S пишет `<имя>.health` в папку шины: время, статус ok или degraded и части. Надзор
(scripts/stand_watch.py) читает файл: свежий и ok - компонент жив; старше STALE_S или не ok - лежит. Повисший
процесс перестаёт писать, и файл стареет.

Слух (voice.layer.voice): ok, когда живы поток озвучки (say) и приём подписчиков узла звука (vmic-accept); слушатель -
главный поток процесса, без него процесс выходит. Мост (voice.layer.bridge): ok, пока его цикл событий отзывается -
сам ставит метку раз в WRITE_EVERY_S (call_later), и метка свежее STALE_S; связь со шлюзом - поле gateway, на статус
не влияет: шлюз лежит - мост переподключается, перезапуск моста не поможет. Пишет отдельный поток на time.sleep: от
цикла событий и его подмен в тестах он не зависит.

Процесс при старте удаляет свой старый файл: пока слух грузит модель (около 20 с), файла нет, и надзор судит по
процессу, а не по файлу прошлого запуска, который успел устареть. Нет файла - для надзора «неизвестно» (None).

Решение (status, state) отделено от файла и часов.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from gateway.env import env_float

VOICE_HEALTH = "voice.health"
BRIDGE_HEALTH = "bridge.health"
OK, DEGRADED = "ok", "degraded"
UP, DOWN = "up", "down"
# Запись раз в 2 с: надзор опрашивает раз в 15 с, файл свежий на каждом опросе, а запись - сотни байт
WRITE_EVERY_S = env_float("SAM_VOICE_HEALTH_EVERY_S", 2.0)
# Пять пропущенных записей подряд - процесс повис или умер, а не притормозил на загрузке модели
STALE_S = env_float("SAM_VOICE_HEALTH_STALE_S", 10.0)
VOICE_THREADS = ("say", "vmic-accept")   # имена потоков в voice.layer.voice и voice.layer.vmic


def status(parts: dict[str, bool]) -> str:
    return OK if all(parts.values()) else DEGRADED


def voice_parts(alive_threads: set[str]) -> dict[str, bool]:
    """Части слуха по живым потокам процесса."""
    return {name: name in alive_threads for name in VOICE_THREADS}


def record(parts: dict[str, bool], at: float, **extra) -> dict:
    return {"at": at, "status": status(parts), "parts": parts, **extra}


def state(doc: dict | None, now: float, stale_s: float = STALE_S) -> str | None:
    """Состояние для надзора: файла нет - None (процесс только поднялся); старый или не ok - down."""
    if not doc:
        return None
    if now - float(doc.get("at") or 0) > stale_s:
        return DOWN
    return UP if doc.get("status") == OK else DOWN


def loop_alive(last_tick: float | None, now: float, stale_s: float = STALE_S) -> bool:
    """Цикл событий отзывается: его метка свежее stale_s."""
    return last_tick is not None and now - last_tick < stale_s


def clear(path: Path) -> None:
    """Старый файл прошлого запуска - долой: процесс только поднимается."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        print(f"[health] {path.name} не удалён: {e}", flush=True)


# Windows не заменяет файл, пока его держит открытым читатель (модули стенда читают живость надзора): отказ в доступе
# на замене (15 окт, logs/stand_watch.log). Чтение - доли миллисекунды, пять повторов через 20 мс его переждут
REPLACE_TRIES = 5
REPLACE_PAUSE_S = 0.02


def write(path: Path, doc: dict, replace: Callable[[Path, Path], None] = os.replace,
          sleep: Callable[[float], None] = time.sleep) -> None:
    """Целиком или никак: временный файл и замена; занятый читателем файл - повтор замены."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    for attempt in range(REPLACE_TRIES):
        try:
            replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            sleep(REPLACE_PAUSE_S)


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_safely(path: Path, doc: dict) -> None:
    """Сбой записи не роняет процесс: строка в лог, надзор увидит старый файл."""
    try:
        write(path, doc)
    except OSError as e:
        print(f"[health] {path.name} не записан: {e}", flush=True)


def beat_once(path: Path, parts: Callable[[], dict[str, bool]], extra: Callable[[], dict],
              clock: Callable[[], float] = time.time) -> None:
    write_safely(path, record(parts(), clock(), **extra()))


def start_beat(path: Path, parts: Callable[[], dict[str, bool]], extra: Callable[[], dict] = dict,
               every_s: float = WRITE_EVERY_S) -> None:
    """Поток живости: части и поля - в файл раз в every_s."""
    def beat() -> None:
        while True:
            beat_once(path, parts, extra)
            time.sleep(every_s)
    threading.Thread(target=beat, daemon=True, name="health").start()


def alive_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.is_alive()}


def start_voice_beat(bus: Path) -> None:
    """Слух: части по живым потокам процесса."""
    start_beat(bus / VOICE_HEALTH, lambda: voice_parts(alive_threads()))


def tick_loop(loop, mark: dict, every_s: float = WRITE_EVERY_S, clock: Callable[[], float] = time.time) -> None:
    """Метка цикла событий: ставит её сам цикл раз в every_s; заблокированный цикл метку не обновит."""
    mark["tick"] = clock()
    loop.call_later(every_s, tick_loop, loop, mark, every_s, clock)


def bridge_parts(mark: dict, now: float, stale_s: float = STALE_S) -> dict[str, bool]:
    return {"loop": loop_alive(mark.get("tick"), now, stale_s)}


def start_bridge_beat(bus: Path, mark: dict, connected: Callable[[], bool]) -> None:
    """Мост: отзывается ли цикл событий (метка tick_loop); связь со шлюзом - поле gateway."""
    start_beat(bus / BRIDGE_HEALTH, lambda: bridge_parts(mark, time.time()), lambda: {"gateway": connected()})
