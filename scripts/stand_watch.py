"""Надзор за стендом SAM для основного агента: строка на каждое изменение состояния компонентов.

  python -u scripts/stand_watch.py        (поднимают модули стенда, gateway/stand_watch_keeper.py; лог
                                          logs/stand_watch.log)

Надзор в одном экземпляре (SAM-195, холодный старт): замок в шине; второй запуск выходит одной строкой. Живость
надзора - файл stand_watch.health в шине, по нему модули решают, поднимать ли надзор; часть `loop` - отзывается ли
цикл опроса. Шлюз лежит дольше GATEWAY_DOWN_S - надзор поднимает его сам командой холодного старта
(gateway/restart.py cold_start_command), с ним встаёт Cortex. Cortex вышел при живом шлюзе дольше CORTEX_DOWN_S -
надзор просит шлюз поднять его (POST /sam/stand/restart cortex), хода модели не ждёт; остальное чинит Cortex по событию.

Имена компонентов - те же, что у restart_component; проверки компонентов общие с перезапуском -
gateway/stand_checks.py. Сжатие истории сессии (SAM-97) встало, когда несжатого в базе сверх потолка, а последнее
сжатие старше порога: строка `compress:<сессия> up -> stalled`.
Печатается только смена состояния, поэтому тишина - это «всё как было».

Шлюзу (SAM-195) надзор шлёт отчёт POST gateway.stand_report.REPORT_PATH - состояние всех компонентов: раз в BEAT_S
и сразу, когда оно отличается от последнего принятого шлюзом. Смены считает шлюз. Шлюз недоступен - надзор работает
дальше, строка в stderr не чаще ERROR_EVERY_S, отчёт повторяется следующим опросом.

Опрос (сеть, процессы) отделён от решения (changes, compression_states, report_due), решение проверяется без них.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

# Запуск из любой папки: корень репозитория на пути импорта
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.access import auth_headers                     # noqa: E402
from gateway.stand_report import COMPRESS_PREFIX, GATEWAY, REPORT_PATH, UP   # noqa: E402
from gateway import restart                                 # noqa: E402
from gateway import stand_checks as sc                      # noqa: E402
from gateway import stand_watch_keeper as keeper            # noqa: E402
from gateway.env import env_float                           # noqa: E402
from gateway.stand_checks import DOWN                       # noqa: E402
from voice.layer import health as voice_health              # noqa: E402

POLL_S = float(os.getenv("SAM_STAND_WATCH_POLL_S", "15"))   # реже не заметить падения в разговоре, чаще - нагрузка wmic
# Отбивка шлюзу, когда ничего не менялось: каждый второй опрос. Ворота молчания надзора ждут две-три пропущенные
# отбивки, то есть замечают умерший надзор за полторы минуты; POST в шлюз раз в 30 с ничего не стоит
BEAT_S = float(os.getenv("SAM_STAND_WATCH_BEAT_S", "30"))
# Шлюз лежит - опрос каждые POLL_S не может отправить отчёт; строки в stderr не чаще раза в минуту, а не на каждом
ERROR_EVERY_S = 60.0
HTTP_TIMEOUT_S = 3
REPORT_URL = sc.GATEWAY_URL + REPORT_PATH
JSON_HEADERS = {"Content-Type": "application/json"}
# Сжатие встало: несжатого выше потолка дольше порога. На потолке сжатие идёт сразу, сводка чанка на локальной
# модели - секунды, до минуты; 10 минут без сжатия выше потолка - его не ставят или оно падает каждый раз
COMPRESS_STALE_S = float(os.getenv("SAM_STAND_WATCH_COMPRESS_STALE_S", "600"))
STALLED = "stalled"
# Шлюз лежит дольше - вкладки нет или она не справляется: вкладка поднимает упавший шлюз через 3 с, его старт - до
# полуминуты (импорт, Mongo, MCP), при полном старте шлюз встаёт после Silero и MCP. Следующая попытка - не раньше
# ещё одного такого срока
GATEWAY_DOWN_S = env_float("SAM_STAND_WATCH_GATEWAY_DOWN_S", 90.0)
# Цикл опроса не отметился дольше - повис: опрос до 25 с (таблица процессов 10 с, четыре /health по 3 с), подъём
# шлюза до restart.COMMAND_WAIT_S (60 с), сон POLL_S
LOOP_STALE_S = env_float("SAM_STAND_WATCH_LOOP_STALE_S", 120.0)
# Cortex вышел при живом шлюзе: ход сам поднимает его, но надзор не ждёт хода - два опроса подряд, и шлюз его поднимает
CORTEX_DOWN_S = env_float("SAM_STAND_WATCH_CORTEX_DOWN_S", 30.0)
RESTART_URL = sc.GATEWAY_URL + "/sam/stand/restart"
# Подъём не помог - срок до следующей попытки удваивается, но не дольше стольких сроков: шлюз 90 с -> 12 мин, Cortex
# 30 с -> 4 мин. Сломанный после правки код перестаёт перезапускаться каждые полминуты, а починенный встанет сам
BACKOFF_MAX = env_float("SAM_STAND_WATCH_BACKOFF_MAX", 8.0)


def compression_stalled(session: dict, stale_s: float = COMPRESS_STALE_S) -> bool:
    """Выше потолка, и последнее сжатие старше порога (или его не было), и сейчас не идёт и не ждёт паузы свежее.
    Ожидание паузы в разговоре (до SAM_COMPRESS_IDLE_MAX_S) - работа, не остановка: 15 окт сжатие voice ждало паузы,
    хвост тем временем перерос потолок, и надзор назвал ожидание остановкой, а голос рассказал об этом человеку."""
    if not session.get("above_ceiling"):
        return False
    for busy in (session.get("running_s"), session.get("waiting_s")):
        if busy is not None and busy < stale_s:
            return False
    ago = session.get("last_ok_ago_s")
    return ago is None or ago > stale_s


def compression_states(gateway_health: dict | None, stale_s: float = COMPRESS_STALE_S,
                       previous: dict[str, str] | None = None) -> dict[str, str]:
    """Состояние сжатия по сессиям из раздела compression в /health шлюза. Шлюз не ответил (лежит или не успел под
    нагрузкой) - сжатие из прошлого снимка: молчание /health не значит, что сжатие поднялось."""
    if gateway_health is None:
        return {k: v for k, v in (previous or {}).items() if k.startswith(COMPRESS_PREFIX)}
    sessions = (gateway_health.get("compression") or {}).get("sessions") or {}
    return {f"{COMPRESS_PREFIX}{sid}": STALLED if compression_stalled(s, stale_s) else UP
            for sid, s in sessions.items()}


def snapshot(previous: dict[str, str] | None = None, now: float | None = None) -> dict[str, str]:
    """Состояния компонентов (gateway/stand_checks.py); previous - прошлый снимок, из него сжатие и Cortex, когда шлюз
    не ответил."""
    state, bodies = sc.component_states(now=now)
    gateway_body = bodies.get(GATEWAY)
    state.update(sc.cortex_state(gateway_body, previous))
    state.update(compression_states(gateway_body, previous=previous))
    return state


def changes(before: dict[str, str], now: dict[str, str]) -> list[str]:
    """Что поменялось: «bridge up -> down». Первый снимок - все, что не up."""
    if not before:
        return [f"{k} {v}" for k, v in now.items() if v != UP]
    return [f"{k} {before.get(k)} -> {v}" for k, v in now.items() if before.get(k) != v]


def report_due(reported: dict[str, str] | None, now: dict[str, str], last_sent: float | None, at: float,
               beat_s: float = BEAT_S) -> dict | None:
    """Отчёт шлюзу или None: состояние не то, что шлюз принял, - сразу; иначе отбивка, когда с прошлой прошло beat_s
    (первый отчёт - сразу после старта)."""
    if reported != now or last_sent is None or at - last_sent >= beat_s:
        return {"states": dict(now)}
    return None


class Reporter:
    """Отчёты шлюзу: что принято, когда отправлено, троттлинг строки об ошибке. Отправка и часы - параметрами."""

    def __init__(self, send: Callable[[dict], None], beat_s: float = BEAT_S, error_every_s: float = ERROR_EVERY_S,
                 clock: Callable[[], float] = time.monotonic, warn: Callable[[str], None] | None = None) -> None:
        self._send, self._beat_s, self._error_every_s, self._clock = send, beat_s, error_every_s, clock
        self._warn = warn or (lambda line: print(line, file=sys.stderr, flush=True))
        self.reported: dict[str, str] | None = None   # состояния последнего отчёта, принятого шлюзом
        self._last_sent: float | None = None
        self._last_warn: float | None = None

    def tick(self, now: dict[str, str]) -> bool:
        """Отправить отчёт, если пора; True - шлюз принял."""
        at = self._clock()
        report = report_due(self.reported, now, self._last_sent, at, self._beat_s)
        if report is None:
            return False
        try:
            self._send(report)
        except Exception as e:   # шлюз лежит или отказал: надзор живёт, отчёт повторится следующим опросом
            if self._last_warn is None or at - self._last_warn >= self._error_every_s:
                self._last_warn = at
                self._warn(f"[стенд] шлюз не принял отчёт надзора ({REPORT_URL}): {e}; надзор работает дальше")
            return False
        self.reported, self._last_sent = dict(now), at
        return True


class DownKeeper:
    """Подъём лежащего компонента: down дольше down_s - действие; следующая попытка не раньше ещё down_s. Пока лежит
    флаг остановки стенда - не поднимает и счёт не ведёт. Действие, часы и флаг - параметрами; act() - (код, строка),
    код None - действие идёт дольше срока."""

    def __init__(self, what: str, act: Callable[[], tuple[int | None, str]], stopped: Callable[[], bool],
                 down_s: float, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, unable: str = "", backoff_max: float = BACKOFF_MAX) -> None:
        """unable - почему поднять нечем (нет команды): тогда строка в лог вместо действия. После каждой попытки, за
        которой компонент так и не встал, срок до следующей удваивается до backoff_max сроков: сломанный код не
        перезапускается каждые полминуты бесконечно."""
        self._what, self._act, self._stopped, self._down_s, self._unable = what, act, stopped, down_s, unable
        self._wait_s, self._wait_max = down_s, down_s * backoff_max
        self._clock, self._log = clock, log
        self._down_since: float | None = None

    def tick(self, state: str | None) -> bool:
        """state - состояние компонента в снимке; True - действие подъёма запущено."""
        at = self._clock()
        if state != DOWN or self._stopped():
            self._down_since, self._wait_s = None, self._down_s
            return False
        if self._down_since is None:
            self._down_since = at
        if at - self._down_since < self._wait_s:
            return False
        waited, self._down_since = self._wait_s, at
        if self._unable:
            self._log(f"[стенд] {self._what} лежит дольше {waited:.0f} с, {self._unable}")
            return False
        self._wait_s = min(self._wait_s * 2, self._wait_max)
        self._log(f"[стенд] {self._what} лежит дольше {waited:.0f} с - поднимаю, следующая попытка через "
                  f"{self._wait_s:.0f} с")
        code, line = self._act()
        result = "действие идёт дольше срока" if code is None else f"код {code}"
        self._log(f"[стенд] {self._what}, подъём: {result}" + (f", {line}" if line else ""))
        return True


def GatewayKeeper(command: str, run: Callable[[str], tuple[int | None, str]], stopped: Callable[[], bool],
                  down_s: float = GATEWAY_DOWN_S, clock: Callable[[], float] = time.monotonic,
                  log: Callable[[str], None] = print) -> DownKeeper:
    """Шлюз - командой холодного старта (restart.cold_start_command); команды нет - строка, что задать."""
    unable = "" if command else f"поднять нечем: {restart.cmd_env(GATEWAY)} не задан"
    return DownKeeper("шлюз", lambda: run(command), stopped, down_s, clock, log, unable)


def CortexKeeper(post: Callable[[], tuple[int | None, str]], stopped: Callable[[], bool],
                 down_s: float = CORTEX_DOWN_S, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print) -> DownKeeper:
    """Cortex живёт в шлюзе: его поднимает шлюз по POST /sam/stand/restart cortex, без хода модели."""
    return DownKeeper("Cortex", post, stopped, down_s, clock, log)


def post_cortex_restart(url: str = RESTART_URL) -> tuple[int | None, str]:
    # без force: шлюз сам проверит, лежит ли Cortex, и поднимет только не живые процессы, не трогая идущие ходы
    body = json.dumps({"name": restart.CORTEX, "force": False}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={**JSON_HEADERS, **auth_headers()})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as res:
            raw = res.read()
    except Exception as e:      # шлюз не ответил или отказал - следующая попытка через срок
        return 1, f"шлюз не принял перезапуск Cortex: {e}"
    try:
        return 0, str(json.loads(raw or b"{}").get("result", ""))
    except ValueError:
        return 0, ""


def run_gateway_command(command: str) -> tuple[int | None, str]:
    return asyncio.run(restart.run_command(command, restart.LOG_DIR / f"restart-{GATEWAY}.log"))


def start_health(mark: dict) -> None:
    """Файл живости надзора: пишет поток раз в WRITE_EVERY_S, часть loop - свежа ли отметка цикла опроса."""
    voice_health.start_beat(keeper.health_path(),
                            lambda: {"loop": voice_health.loop_alive(mark.get("tick"), time.time(), LOOP_STALE_S)},
                            lambda: {"pid": os.getpid()})


def post_report(report: dict, url: str = REPORT_URL) -> None:
    """POST JSON в шлюз; не 2xx или нет связи - исключение."""
    body = json.dumps(report, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={**JSON_HEADERS, **auth_headers()})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S):
        pass


def main() -> None:
    lock = keeper.acquire_lock()
    if lock is None:
        print(f"[стенд] надзор уже запущен (замок {keeper.lock_path()}), второй экземпляр выходит", flush=True)
        return
    mark = {"tick": time.time()}
    start_health(mark)
    before: dict[str, str] = {}
    reporter = Reporter(post_report)
    gateway = GatewayKeeper(restart.cold_start_command(os.environ, sys.platform == "win32"), run_gateway_command,
                            keeper.STOP_FLAG.exists, log=lambda line: print(line, flush=True))
    cortex = CortexKeeper(post_cortex_restart, keeper.STOP_FLAG.exists, log=lambda line: print(line, flush=True))
    print(f"[стенд] надзор запущен, PID {os.getpid()}", flush=True)
    while True:
        mark["tick"] = time.time()
        now = snapshot(before)
        for line in changes(before, now):
            print(f"[стенд] {line}", flush=True)
        before = now
        reporter.tick(now)
        gateway.tick(now.get(GATEWAY))
        cortex.tick(now.get(sc.CORTEX) if now.get(GATEWAY) == UP else None)    # шлюз лежит - Cortex поднимет его старт
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
