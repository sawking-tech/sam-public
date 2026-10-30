"""Надзор поднят? Нет - поднять (SAM-195, холодный старт).

Любой долгоживущий модуль стенда (шлюз, слух, мост, агрегатор MCP, радар) раз в KEEP_S проверяет, что надзор
(scripts/stand_watch.py) жив, и поднимает его; надзор поднимает шлюз, а с ним Cortex, и Cortex чинит остальное.
start_sam.ps1 зовёт ту же проверку один раз в конце полного старта: `python -m gateway.stand_watch_keeper`.

Надзор жив, пока свеж его файл живости `stand_watch.health` в шине (пишет раз в voice/layer/health.WRITE_EVERY_S).
Файла нет или он старше HEALTH_STALE_S - надзор запускается. Надзор в одном экземпляре: замок `stand_watch.lock` в
шине держит процесс надзора, ОС снимает его со смертью процесса; второй экземпляр выходит одной строкой. Поэтому
модули, которые заметили пропажу одновременно, лишних надзоров не оставят. Повисший надзор (файл свежий, `loop`
не ok) заново не запускается: замок у него.

Пока лежит флаг остановки стенда `.sam_stop` (start_sam.ps1 -Stop), надзор не поднимается.

Надзор запускается отдельным процессом без окна, отвязанным от модуля: через промежуточный процесс (`--launch`),
который запускает надзор и сразу выходит. Дерево модуля надзор не держит, и `taskkill /T` по вкладке шлюза
(start_sam.ps1 -Gateway) его не заденет. Вывод надзора - logs/stand_watch.log.

Решение (should_start) отделено от файла, флага, часов и процессов.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from voice.layer import health as voice_health
from voice.layer.config import BUS_DIR

from .core_paths import REPO
from .env import env_float
from .proc_tree import NEW_GROUP

MODULE = "gateway.stand_watch_keeper"
LAUNCH_ARG = "--launch"
HEALTH_NAME = "stand_watch.health"
LOCK_NAME = "stand_watch.lock"
WATCH_SCRIPT = REPO / "scripts" / "stand_watch.py"
LOG = REPO / "logs" / "stand_watch.log"
STOP_FLAG = REPO / ".sam_stop"         # start_sam.ps1, $STOPFLAG
# Проверка раз в 30 с: надзор опрашивает раз в 15 с, его пропажа заметна за пару опросов; чтение файла ничего не
# стоит. 0 - модуль надзор не поднимает (машина без стенда, тесты)
KEEP_S = env_float("SAM_STAND_WATCH_KEEP_S", 30.0)
# Файл пишется раз в 2 с; пять пропущенных записей - процесса надзора нет, как у слуха (SAM_VOICE_HEALTH_STALE_S)
HEALTH_STALE_S = env_float("SAM_STAND_WATCH_HEALTH_STALE_S", 10.0)
# Без окна: консольный процесс получает скрытую консоль, и powershell, который надзор зовёт за таблицей процессов,
# не открывает окно на каждом опросе (у DETACHED_PROCESS консоли нет, и окно открывал бы каждый дочерний процесс)
WINDOWS_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0


def health_path(bus: Path = BUS_DIR) -> Path:
    return bus / HEALTH_NAME


def lock_path(bus: Path = BUS_DIR) -> Path:
    return bus / LOCK_NAME


def should_start(doc: dict | None, now: float, stopped: bool, stale_s: float = HEALTH_STALE_S) -> bool:
    """Поднимать надзор: стенд не остановлен, а файла живости нет или он старше stale_s."""
    if stopped:
        return False
    return not doc or now - float(doc.get("at") or 0) > stale_s


def acquire_lock(path: Path | None = None):
    """Замок надзора или None, если его держит другой процесс. Замок держится, пока жив объект и процесс."""
    from filelock import FileLock, Timeout
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return None
    return lock


def _popen(args: list[str], out) -> None:
    subprocess.Popen(args, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                     creationflags=WINDOWS_FLAGS, **NEW_GROUP)


def spawn(python: str = sys.executable) -> None:
    """Промежуточный процесс, который запустит надзор и выйдет."""
    _popen([python, "-m", MODULE, LAUNCH_ARG], subprocess.DEVNULL)


def launch(python: str = sys.executable, log: Path = LOG) -> None:
    """Надзор с выводом в лог; вызывающий процесс после этого выходит."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        _popen([python, "-u", str(WATCH_SCRIPT)], out)


def ensure(read: Callable[[], dict | None] = lambda: voice_health.read(health_path()),
           stopped: Callable[[], bool] = STOP_FLAG.exists, start: Callable[[], None] = spawn,
           clock: Callable[[], float] = time.time) -> bool:
    """Поднять надзор, если он не жив; True - запущен."""
    if not should_start(read(), clock(), stopped()):
        return False
    start()
    return True


def _ensure_safely(who: str) -> None:
    """Сбой проверки не роняет модуль: строка в его лог, следующая проверка через KEEP_S."""
    try:
        if ensure():
            print(f"[{who}] надзор стенда не отвечал - запущен, лог {LOG}", flush=True)
    except Exception as e:
        print(f"[{who}] надзор стенда не поднят: {e}", flush=True)


def start(who: str, every_s: float = KEEP_S) -> None:
    """Поток модуля: раз в every_s поднять надзор, если он не жив. Первая проверка - через every_s: стенд при полном
    старте поднимает надзор сам, модулю незачем опережать его."""
    if every_s <= 0:
        return

    def keep() -> None:
        while True:
            time.sleep(every_s)
            _ensure_safely(who)
    threading.Thread(target=keep, daemon=True, name="stand-watch-keeper").start()


def main(argv: list[str]) -> None:
    if LAUNCH_ARG in argv:
        launch()
        return
    started = ensure()
    print(f"  Stand watch {'started, log ' + str(LOG) if started else 'already running'}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
