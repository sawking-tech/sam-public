"""Перезапуск компонента стенда из хода (SAM-195): SAM перезапускает любой модуль сама, без вопроса.

Решение (plan) - что делать с именем: команда компонента, перезапуск самого шлюза, закрытие процессов Cortex или
строка, почему нельзя. Исполнение (Restarter) - запуск команды, выход шлюза после хода, закрытие Cortex; процессы,
часы и выход приходят параметрами, поэтому решение и порядок проверяются без них.

Команда компонента - SAM_RESTART_CMD_<ИМЯ>; по умолчанию на Windows - ключ start_sam.ps1 (вкладка компонента гаснет
и открывается заново) или start_radar.ps1 для радара. На другой ОС без переменной перезапуск не настроен - одна
строка об этом. Выключенного радара (gateway/radar_switch.py) среди компонентов нет: перезапуск отвечает, как на чужое имя.

Шлюз перезапускает сам себя: ждёт конца идущих ходов (ответ модели уходит), затем выходит; вкладка стенда поднимает
его через 3 с (scripts/sam_process.ps1). С SAM_RESTART_CMD_GATEWAY вместо выхода после ходов идёт эта команда
(например, systemctl на сервере). Команда, которая сама гасит процесс шлюза вместе со своим деревом (`start_sam.ps1
-Gateway`), сюда не годится: она умрёт вместе со шлюзом, не подняв новый.

Cortex живёт процессами сессий ACP внутри шлюза: перезапуск ждёт конца их идущих ходов (ход основной модели при
LLM_SOURCE=acp тоже идёт через Cortex) не дольше CORTEX_MAX_WAIT_S, затем закрывает процессы и сразу поднимает их
заново: Cortex чинит стенд и не ждёт первого хода. Ход, не кончившийся за предел, рвётся, и его создатель видит неудачу.

Один компонент перезапускается один раз за раз: второй вызов, пока идёт первый, получает «уже идёт».

Перед перезапуском - health check компонента (gateway/stand_checks.py, те же проверки, что у надзора): вкладка
поднимает упавшее сама за 3 с, и уже поднявшийся компонент не перезапускается - «уже поднялся», без действия. force -
перезапуск без проверки: после правки кода компонента, когда он завис, а health ok (Cortex с живым процессом,
шлюз), или по прямой просьбе.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from . import radar_switch
from .core_paths import REPO
from .env import env_float
from .proc_tree import NEW_GROUP

GATEWAY, CORTEX, RADAR = "gateway", "cortex", radar_switch.COMPONENT
CMD_ENV_PREFIX = "SAM_RESTART_CMD_"
START_SCRIPT = REPO / "start_sam.ps1"
RADAR_SCRIPT = REPO / "start_radar.ps1"
POWERSHELL = "powershell -NoProfile -ExecutionPolicy Bypass -File"
# Компонент -> ключ start_sam.ps1; tts - синтез Silero. Те же имена у надзора (scripts/stand_watch.py)
START_SWITCHES = {
    "mcp": "-Mcp", "voice": "-Voice", "bridge": "-Bridge", "tts": "-Tts", "embedder": "-Embedder",
    "client": "-Client", "cosy": "-Cosy",
}
NAMES = (GATEWAY, *START_SWITCHES, RADAR, CORTEX)
# Ключ шлюза - только для подъёма снаружи (cold_start_command): изнутри шлюза он погасил бы и свою команду
GATEWAY_SWITCH = "-Gateway"
COMMAND, SELF, CLOSE_CORTEX, REFUSED = "command", "self", "cortex", "refused"
# Состояние живого компонента у health check - то же, что stand_report.UP; импортом не взять: stand_report сам
# импортирует restart (restart_name), обратный импорт замкнул бы цикл
UP_STATE = "up"
# Ключ start_sam.ps1: гасит вкладку, открывает новую и через 5 с проверяет дубли - 7-10 с. Дольше минуты - команда
# повисла: ход не ждёт её дальше, модель проверяет /health сама
COMMAND_WAIT_S = env_float("SAM_RESTART_WAIT_S", 60.0)
# Шлюз ждёт конца идущих ходов, но не дольше: голосовой ход с инструментами идёт до минуты, агентов шлюз не ждёт
SELF_MAX_WAIT_S = env_float("SAM_RESTART_SELF_MAX_WAIT_S", 120.0)
# Cortex ждёт конца ходов своих агентов так же: дольше - ход рвётся
CORTEX_MAX_WAIT_S = env_float("SAM_RESTART_CORTEX_MAX_WAIT_S", 120.0)
# После последнего хода: ответ уходит в монитор и мост, ответ REST - вызывающему
SELF_GRACE_S = env_float("SAM_RESTART_SELF_GRACE_S", 2.0)
# Мягкий выход (SIGINT uvicorn) закрывает соединения и гасит тёплые контейнеры; не вышел за столько - выход жёстко
FORCE_EXIT_S = 10.0
POLL_S = 0.5
OUTPUT_LINE_CHARS = 300
LOG_DIR = REPO / "logs"       # вывод команды перезапуска: logs/restart-<компонент>.log, последний запуск


@dataclass(frozen=True)
class Plan:
    kind: str                 # COMMAND, SELF, CLOSE_CORTEX, REFUSED
    name: str = ""
    command: str = ""         # для COMMAND; для SELF - команда вместо выхода, пусто - выход процесса
    reason: str = ""          # для REFUSED


def names(environ: Mapping[str, str] = os.environ) -> tuple[str, ...]:
    """Компоненты, которые можно перезапустить на этой машине: выключенного радара среди них нет."""
    return NAMES if radar_switch.enabled(environ) else tuple(n for n in NAMES if n != RADAR)


def cmd_env(name: str) -> str:
    return f"{CMD_ENV_PREFIX}{name.upper()}"


def default_command(name: str, windows: bool) -> str:
    """Команда по умолчанию: на Windows - скрипты стенда; на другой ОС её нет."""
    if not windows:
        return ""
    if name == RADAR:
        return f'{POWERSHELL} "{RADAR_SCRIPT}"'
    switch = START_SWITCHES.get(name)
    return f'{POWERSHELL} "{START_SCRIPT}" {switch}' if switch else ""


def cold_start_command(environ: Mapping[str, str], windows: bool) -> str:
    """Подъём лежащего шлюза снаружи - надзором (scripts/stand_watch.py): SAM_RESTART_CMD_GATEWAY, на Windows по
    умолчанию ключ start_sam.ps1 -Gateway (гасит вкладку шлюза и открывает новую); на другой ОС без переменной - ""."""
    command = (environ.get(cmd_env(GATEWAY)) or "").strip()
    if command or not windows:
        return command
    return f'{POWERSHELL} "{START_SCRIPT}" {GATEWAY_SWITCH}'


def plan(name: str, environ: Mapping[str, str], windows: bool) -> Plan:
    name = (name or "").strip().lower()
    known = names(environ)
    if name not in known:
        return Plan(REFUSED, name, reason=f"нет компонента {name!r}; есть: {', '.join(known)}")
    if name == CORTEX:
        return Plan(CLOSE_CORTEX, name)
    command = (environ.get(cmd_env(name)) or "").strip()
    if name == GATEWAY:
        if command or windows:   # на Windows шлюз поднимает его вкладка
            return Plan(SELF, name, command)
        return Plan(REFUSED, name, reason=f"перезапуск {name} здесь не настроен: {cmd_env(name)}")
    command = command or default_command(name, windows)
    if not command:
        return Plan(REFUSED, name, reason=f"перезапуск {name} здесь не настроен: {cmd_env(name)}")
    return Plan(COMMAND, name, command)


def _last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")[:OUTPUT_LINE_CHARS]


async def run_command(command: str, log_path: Path, timeout_s: float = COMMAND_WAIT_S) -> tuple[int | None, str]:
    """Команда перезапуска: (код, последняя строка вывода); не кончилась за срок - (None, ""), процесс не трогается:
    он может быть посреди подъёма компонента. Вывод - в файл, не в канал: вкладка и мост, которые команда запускает,
    наследуют его и живут дальше, а чтение канала ждало бы их конца."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as out:
        proc = await asyncio.create_subprocess_shell(command, stdin=asyncio.subprocess.DEVNULL, stdout=out,
                                                     stderr=out, cwd=str(REPO), **NEW_GROUP)
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except asyncio.TimeoutError:
        return None, ""
    return proc.returncode, _last_line(log_path.read_text(encoding="utf-8", errors="replace"))


def spawn_detached(command: str) -> None:
    """Команда, которая переживёт шлюз (перезапуск самого шлюза службой ОС)."""
    import subprocess
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    subprocess.Popen(command, shell=True, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, **NEW_GROUP)


def exit_gateway() -> None:
    """Мягкий выход uvicorn (SIGINT), страховка - жёсткий через FORCE_EXIT_S."""
    timer = threading.Timer(FORCE_EXIT_S, os._exit, args=(0,))
    timer.daemon = True   # иначе мягко вышедший процесс ждёт таймер ещё FORCE_EXIT_S, и вкладка поднимает его позже
    timer.start()
    signal.raise_signal(signal.SIGINT)


async def _no_check(name: str) -> str | None:
    return None


def restart_name(component: str) -> str | None:
    """Имя перезапуска для компонента надзора: имена общие; None - перезапуском не лечится (сжатие compress:*)."""
    return component if component in names() else None


class Restarter:
    """Исполнение плана. turns() - сколько ходов шлюза идёт сейчас, cortex_turns() - ходов агентов Cortex;
    close_cortex(only_dead) перезапускает процессы Cortex шлюза: все или только не живые."""

    def __init__(self, turns: Callable[[], int], close_cortex: Callable[[bool], Awaitable[int]],
                 cortex_turns: Callable[[], int] = lambda: 0,
                 check: Callable[[str], Awaitable[str | None]] = _no_check,
                 run: Callable[[str, Path], Awaitable[tuple[int | None, str]]] = run_command,
                 detach: Callable[[str], None] = spawn_detached, exit_self: Callable[[], None] = exit_gateway,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 environ: Mapping[str, str] = os.environ, windows: bool = sys.platform == "win32",
                 log: Callable[[str], None] = print) -> None:
        self._turns, self._cortex_turns, self._close_cortex, self._check = turns, cortex_turns, close_cortex, check
        self._run, self._detach = run, detach
        self._exit, self._sleep, self._clock, self._environ, self._windows = exit_self, sleep, clock, environ, windows
        self._log = log
        self._busy: set[str] = set()                 # команды, которые идут
        self.tasks: dict[str, asyncio.Task] = {}     # отложенные перезапуски (шлюз, Cortex) до конца ходов

    def _running(self, name: str) -> bool:
        task = self.tasks.get(name)
        return name in self._busy or (task is not None and not task.done())

    async def restart(self, name: str, force: bool = False) -> str:
        """Строка итога для модели. force - без health check: после правки кода или по прямой просьбе."""
        p = plan(name, self._environ, self._windows)
        if p.kind == REFUSED:
            return p.reason
        if self._running(p.name):
            return f"{p.name}: перезапуск уже идёт, дождись его итога и проверь /health"
        self._busy.add(p.name)   # до первого await: второй вызов, пока идёт проверка, получит «уже идёт»
        try:
            return await self._restart(p, force)
        finally:
            self._busy.discard(p.name)

    async def _restart(self, p: Plan, force: bool) -> str:
        if not force and await self._check(p.name) == UP_STATE:
            return (f"{p.name}: уже поднялся, перезапуск не нужен; после правки его кода, если он завис при ok или "
                    f"по прямой просьбе - restart_component с force")
        if p.kind == CLOSE_CORTEX:
            self.tasks[p.name] = asyncio.create_task(self._restart_cortex(force), name="restart-cortex")
            if not force:
                return "cortex: не живые процессы поднимаются заново, живые и их ходы не трогаются"
            return (f"cortex: процессы перезапустятся после конца идущих ходов агентов Cortex (не дольше "
                    f"{CORTEX_MAX_WAIT_S:.0f} с, дальше ход рвётся и создатель видит неудачу)")
        if p.kind == SELF:
            self.tasks[p.name] = asyncio.create_task(self._restart_self(p.command), name="restart-gateway")
            return ("gateway: перезапуск после конца идущих ходов, через несколько секунд шлюз недоступен; "
                    "поднимется сам, проверь /health")
        self._log(f"[Restart] {p.name}: {p.command}")
        code, line = await self._run(p.command, LOG_DIR / f"restart-{p.name}.log")
        if code is None:
            return f"{p.name}: команда перезапуска идёт дольше {COMMAND_WAIT_S:.0f} с, проверь /health компонента"
        if code:
            return f"{p.name}: перезапуск не удался, код {code}: {line or 'без вывода'}"
        return f"{p.name}: перезапущен" + (f" ({line})" if line else "")

    async def _wait_turns(self, turns: Callable[[], int], max_s: float) -> None:
        deadline = self._clock() + max_s
        while turns() > 0 and self._clock() < deadline:
            await self._sleep(POLL_S)

    async def _restart_cortex(self, force: bool) -> None:
        """force - все процессы после конца ходов (правка кода Cortex); без него - только не живые, сразу: живой
        поставщик или агент, у которого идёт задача, не рвётся из-за соседа."""
        if force:
            await self._wait_turns(self._cortex_turns, CORTEX_MAX_WAIT_S)
        closed = await self._close_cortex(not force)
        self._log(f"[Restart] cortex: перезапущено{'' if force else ' не живое'}, было живых процессов {closed}")

    async def _restart_self(self, command: str) -> None:
        """Дождаться конца ходов (не дольше SELF_MAX_WAIT_S), выдержать SELF_GRACE_S, выйти или отдать команде."""
        await self._wait_turns(self._turns, SELF_MAX_WAIT_S)
        await self._sleep(SELF_GRACE_S)
        self._log("[Restart] шлюз перезапускается" + (f": {command}" if command else ", вкладка поднимет его"))
        if command:
            self._detach(command)
        else:
            self._exit()
