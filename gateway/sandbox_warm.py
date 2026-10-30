"""Быстрая песочница (ADR-016 «Быстрая проверка»): тёплый контейнер на сессию разговора и исполнитель по каналу.

`sandbox_run` поднимает контейнер на каждый запуск - 0,8 с. Здесь у сессии один тёплый контейнер с той же изоляцией
(sandbox.hardening: uid 1000, корень только для чтения, tmpfs /tmp, без capabilities, лимиты) и всегда без сети:
сеть под файрволом выдаётся только запуску sandbox_run. Внутри живёт исполнитель `sandbox_repl.py` (`docker run -i`),
запуск - кадр JSON в его stdin и кадр с итогом из stdout, единицы миллисекунд. Переменные Python живут между
запусками. Кадр принимается, только если он ответ на этот запуск: со случайным nonce вызова, которого код в
контейнере не видит (устройство исполнителя - в sandbox_repl.py); кадр без запроса рвёт канал.

Основной агент ждёт итог в ходе разговора (исключение из ADR-005): весь его вызов, с ожиданием замка и стартом
контейнера, не дольше SAM_FAST_SANDBOX_MAIN_TIMEOUT_S; воркер и MCP - по таймауту кода.

Контейнер принадлежит сессии разговора, как задача песочницы (sandbox.Owner): контейнер ищется по сессии, другой сессии он не
достаётся; воркер идёт от сессии создателя и делит контейнер с ним. Запуски одного контейнера идут по одному.

Зависший код гасит только свой контейнер (docker kill); следующий вызов поднимает новый, и ответ честно говорит, что
прежнее состояние потеряно. Контейнер без вызовов дольше SAM_FAST_SANDBOX_IDLE_S гасит фоновая уборка шлюза; тёплых
контейнеров не больше SAM_FAST_SANDBOX_MAX - новому уступает давний свободный.

Папка /work контейнера - `SAM_SANDBOX_DIR/warm-<id>`, журнал и запись сессии - рядом, как у задачи. Папка живёт, пока
живёт шлюз: после остановки контейнера сессия получает ту же папку. Имя с `warm-` - не id задачи (sandbox.JOB_ID):
sandbox_run, показ в мониторе и перенос в приложение её не берут - шлюз пишет в папку задачи под замком задачи,
когда её контейнера нет, а тёплый контейнер живёт между вызовами.

Решения (команда docker, выбор контейнера на вытеснение и уборку, текст итога) отделены от процессов: канал
открывает функция-параметр, время - тоже параметр.
"""
from __future__ import annotations

import json
import logging
import queue
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from . import sandbox, sandbox_net
from .env import env_int
from .sandbox_repl import LANGS, PYTHON

WARM_PREFIX = "warm-"
CONTAINER_PREFIX = "sam-warm-"
REPL_SOURCE = Path(__file__).with_name("sandbox_repl.py")
IDLE_ENV, DEFAULT_IDLE_S = "SAM_FAST_SANDBOX_IDLE_S", 600
MAX_ENV, DEFAULT_MAX = "SAM_FAST_SANDBOX_MAX", 3          # тёплый контейнер держит до SAM_SANDBOX_MEMORY памяти
TIMEOUT_ENV, DEFAULT_TIMEOUT_S = "SAM_FAST_SANDBOX_TIMEOUT_S", 30
MAX_TIMEOUT_ENV, DEFAULT_MAX_TIMEOUT_S = "SAM_FAST_SANDBOX_MAX_TIMEOUT_S", 300   # дольше - работа для sandbox_run
# Старт контейнера - 0,5-0,8 с (замер 14 окт); запас на холодный Docker Desktop
START_ENV, DEFAULT_START_S = "SAM_FAST_SANDBOX_START_S", 60
SWEEP_ENV, DEFAULT_SWEEP_S = "SAM_FAST_SANDBOX_SWEEP_S", 30
# Основной агент ждёт итог в ходе разговора (ADR-005): весь вызов не дольше этого, долгое - воркеру
MAIN_TIMEOUT_ENV, DEFAULT_MAIN_TIMEOUT_S = "SAM_FAST_SANDBOX_MAIN_TIMEOUT_S", 10
MAIN_HINT = "отдай воркеру (spawn_agent)"
NONCE_BYTES = 16
BROKEN_REASON = "исполнитель прислал кадр без запроса"
# Кадр итога: вывод обрезан исполнителем до OUTPUT_MAX_CHARS, в JSON знак - до 6 байт (\uXXXX); длиннее - кадр
# не от исполнителя, канал рвётся
FRAME_MAX_BYTES = 64_000
STDERR_KEPT_BYTES = 4000        # хвост stderr docker для разбора сбоя старта
CLOSE_WAIT_S = 5                # процесс docker CLI после docker kill выходит за доли секунды
KIND_START, KIND_EXEC, KIND_STOP = "warm_start", "exec", "warm_stop"
MS_PER_S = 1000

log = logging.getLogger(__name__)


class ChannelClosed(Exception):
    """Исполнитель ушёл или прислал не кадр; текст - причина для ответа."""


_BROKEN = object()      # в очереди кадров: строка не кадр, кадр без запроса или переполнение
# Кадров в очереди: готовность или итог одного запуска и метка конца; больше - исполнитель шлёт без запроса
FRAMES_QUEUED = 2


class Channel:
    """Процесс `docker run -i` с исполнителем: кадры в stdin; stdout читает поток в очередь, stderr - в хвост.

    Кадр принимается, только пока его ждут (готовность после старта, итог после запроса), очередь короткая: кадр без
    запроса, мусор или переполнение рвут канал - чтение останавливается, память шлюза не растёт, следующий recv
    отвечает ChannelClosed, а уборка гасит такой контейнер."""

    def __init__(self, argv: list[str], kill: Callable[[], None], popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._kill = kill
        self.frames: queue.Queue = queue.Queue(maxsize=FRAMES_QUEUED)
        self.waiting = threading.Event()
        self.waiting.set()              # первым ждём кадр готовности
        self.broken = False
        self._err = bytearray()
        threading.Thread(target=self._read, daemon=True, name="warm-out").start()
        threading.Thread(target=self._read_err, daemon=True, name="warm-err").start()

    def _put(self, item) -> bool:
        """В очередь без ожидания; переполнение - канал сломан."""
        try:
            self.frames.put_nowait(item)
            return True
        except queue.Full:
            self.broken = True
            return False

    def _read(self) -> None:
        try:
            while line := self.proc.stdout.readline(FRAME_MAX_BYTES + 1):
                try:
                    frame = json.loads(line) if line.endswith(b"\n") else _BROKEN
                except ValueError:
                    frame = _BROKEN
                if not isinstance(frame, dict) or not self.waiting.is_set():
                    frame = _BROKEN
                self.waiting.clear()
                if frame is _BROKEN:
                    self.broken = True
                if not self._put(frame) or frame is _BROKEN:
                    return
        except (OSError, ValueError):
            pass
        finally:
            self._put(None)

    def _read_err(self) -> None:
        try:
            while chunk := self.proc.stderr.read1(STDERR_KEPT_BYTES):
                self._err = (self._err + chunk)[-STDERR_KEPT_BYTES:]
        except (OSError, ValueError):
            pass

    def stderr_tail(self) -> str:
        return bytes(self._err).decode("utf-8", errors="replace")

    def send(self, frame: dict, timeout: float) -> None:
        """Кадр в stdin за timeout: запись идёт в потоке - исполнитель, который не читает stdin, не держит запуск
        под замком контейнера дольше таймаута. Не успело - TimeoutError (канал гасится тем, кто ловит)."""
        data = json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n"
        failed = []

        def write() -> None:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                failed.append(e)

        self.waiting.set()          # итог может прийти раньше, чем запись вернётся
        writer = threading.Thread(target=write, daemon=True, name="warm-in")
        writer.start()
        writer.join(timeout)
        if writer.is_alive():
            raise TimeoutError
        if failed:
            raise ChannelClosed("исполнитель не принимает запуск") from failed[0]

    def recv(self, timeout: float) -> dict:
        """Следующий кадр; не пришёл за timeout - TimeoutError, исполнитель ушёл или прислал мусор - ChannelClosed."""
        try:
            frame = self.frames.get(timeout=max(0.0, timeout))
        except queue.Empty:
            raise TimeoutError from None
        if frame is None:
            raise ChannelClosed("исполнитель завершился: код вызвал выход процесса или кончилась память")
        if frame is _BROKEN:
            raise ChannelClosed(f"исполнитель прислал не кадр протокола, кадр без запроса или длиннее "
                                f"{FRAME_MAX_BYTES} байт")
        return frame

    def close(self) -> int | None:
        """Погасить контейнер и процесс CLI; код выхода процесса docker (None - не вышел)."""
        try:
            self._kill()
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("Тёплый контейнер не погашен docker kill (%s): проверьте docker ps", e)
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=CLOSE_WAIT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None


def warm_argv(name: str, host_dir: Path, limits: sandbox.Limits, source: str, docker: str | None = None) -> list[str]:
    """Тёплый контейнер: та же изоляция, что у запуска песочницы, исполнитель из исходника через python -c."""
    return [
        docker or sandbox.DOCKER, "run", "-i", "--rm", "--name", name,
        "--network", limits.network, *sandbox.hardening(limits),
        "-v", f"{host_dir}:{sandbox.WORKDIR_IN}", "-w", sandbox.WORKDIR_IN,
        limits.image, "python", "-u", "-c", source,
    ]


def docker_channel(name: str, host_dir: Path, limits: sandbox.Limits) -> Channel:
    source = REPL_SOURCE.read_text(encoding="utf-8")
    return Channel(warm_argv(name, host_dir, limits, source), kill=lambda: sandbox.docker_kill(name))


OpenChannel = Callable[[str, Path, sandbox.Limits], Channel]


@dataclass(eq=False)
class Warm:
    session: str
    warm_id: str
    last_used: float
    channel: Channel | None = None      # None - контейнера нет, следующий запуск поднимет
    name: str = ""
    users: int = 0                      # запуски, которые держат или ждут контейнер: такой не гасится
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class ExecOutcome:
    text: str                   # ответ инструмента: одна строка отказа или итог
    code: int | None = None
    ms: float | None = None     # время кода внутри исполнителя
    wall_ms: float | None = None    # от отправки кадра до итога в шлюзе
    warm_id: str = ""


# ─── решения ───────────────────────────────────────────────────────────────────

def idle_ones(warms: Iterable[Warm], now: float, idle_s: float) -> list[Warm]:
    """Свободные контейнеры без вызовов дольше idle_s."""
    return [w for w in warms if w.users == 0 and now - w.last_used >= idle_s]


def victim(warms: Iterable[Warm]) -> Warm | None:
    """Кого вытеснить ради нового: давний по последнему вызову из свободных; все заняты - None."""
    return min((w for w in warms if w.users == 0), key=lambda w: w.last_used, default=None)


def exec_timeout(value) -> int:
    """Из параметра или SAM_FAST_SANDBOX_TIMEOUT_S; мусор - по умолчанию; не выше потолка."""
    default = env_int(TIMEOUT_ENV, DEFAULT_TIMEOUT_S)
    ceiling = env_int(MAX_TIMEOUT_ENV, DEFAULT_MAX_TIMEOUT_S)
    try:
        asked = int(value)
    except (TypeError, ValueError):
        asked = default
    return max(1, min(asked if asked > 0 else default, ceiling))


@dataclass(frozen=True)
class Budget:
    run_s: int                  # таймаут кода
    total_s: int | None = None  # потолок всего вызова с замком и стартом контейнера (основной агент), None - нет


def budget(by: str, asked) -> tuple[Budget | None, str | None]:
    """Сроки вызова. Основной агент (by пуст, ADR-005) ждёт итог в ходе разговора: весь вызов, вместе с ожиданием
    замка и стартом контейнера, не дольше SAM_FAST_SANDBOX_MAIN_TIMEOUT_S; просьба дольше - отказ. Воркер и MCP -
    таймаут кода, замок и старт отдельно."""
    timeout = exec_timeout(asked)
    if by:
        return Budget(timeout), None
    cap = max(1, env_int(MAIN_TIMEOUT_ENV, DEFAULT_MAIN_TIMEOUT_S))
    try:
        wanted = int(asked)
    except (TypeError, ValueError):
        wanted = 0
    if wanted > cap:
        return None, f"Дольше {cap} с ({MAIN_TIMEOUT_ENV}) основной агент не ждёт: {MAIN_HINT}"
    return Budget(min(timeout, cap), min(timeout, cap)), None


def remaining(deadline: float | None, default: float) -> float:
    """Сколько ждать шага: default, но не дольше срока вызова."""
    if deadline is None:
        return default
    return max(0.0, min(default, deadline - time.monotonic()))


def check_request(params: dict) -> tuple[str, str, str | None]:
    """(язык, код, отказ одной строкой или None)."""
    lang = str(params.get("lang") or PYTHON).strip().lower()
    code = str(params.get("code") or "")
    if lang not in LANGS:
        return lang, code, f"lang - {', '.join(LANGS)}"
    if not code.strip():
        return lang, code, "Нужен code: текст программы на языке lang"
    if len(code.encode("utf-8")) > sandbox.FILE_MAX_BYTES:
        return lang, code, f"code больше {sandbox.FILE_MAX_BYTES} байт: большой код - файлами в sandbox_run"
    return lang, code, None


def result_text(reply: dict) -> str:
    """Итог запуска из кадра исполнителя."""
    out = str(reply.get("out") or "")
    if reply.get("code") is None:           # исполнитель отказал запуску (язык) - строка отказа
        return out.strip() or "Исполнитель не выполнил запуск"
    ms = reply.get("ms")
    took = f" за {ms:.1f} мс" if isinstance(ms, (int, float)) else ""
    head = f"Код выхода {reply['code']}{took}"
    if not out.strip():
        return f"{head}\nВывода нет"
    label = f"Вывод (последние {sandbox.OUTPUT_MAX_CHARS} знаков)" if reply.get("truncated") else "Вывод"
    return f"{head}\n{label}:\n{out.rstrip()}"


# ─── пул тёплых контейнеров ────────────────────────────────────────────────────

class WarmPool:
    """Тёплые контейнеры по сессиям. Карта под общим замком; запуск - под замком контейнера, docker - без общего."""

    def __init__(self, root: Path | None = None, limits: sandbox.Limits | None = None,
                 open_channel: OpenChannel = docker_channel, clock: Callable[[], float] = time.monotonic,
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12]):
        self.root = Path(root or sandbox.SANDBOX_DIR)
        self._limits = limits
        self._open = open_channel
        self._clock = clock
        self._new_id = new_id
        self._guard = threading.Lock()
        self._warm: dict[str, Warm] = {}       # сессия -> контейнер
        self._ids: dict[str, str] = {}         # сессия -> папка; живёт и после остановки контейнера
        self._lost: dict[str, str] = {}        # сессия -> почему погашен прежний контейнер, для следующего ответа

    def live(self) -> int:
        with self._guard:
            return sum(1 for w in self._warm.values() if w.channel is not None)

    def exec(self, owner: sandbox.Owner, params: dict) -> ExecOutcome:
        """Оркестратор: проверка запроса и срока, контейнер сессии (поднять или вытеснить давний), запуск под его
        замком."""
        lang, code, error = check_request(params)
        plan, refusal = budget(owner.by, params.get("timeout"))
        if error or refusal:
            return ExecOutcome(error or refusal)
        deadline = time.monotonic() + plan.total_s if plan.total_s is not None else None
        warm, evicted, error = self._take(owner.session)
        for gone, reason in evicted:
            self._stop(gone, reason)
        if error:
            return ExecOutcome(error)
        try:
            wait = remaining(deadline, env_int(sandbox.JOB_WAIT_ENV, sandbox.DEFAULT_JOB_WAIT_S))
            if not warm.lock.acquire(timeout=wait):
                busy = f"Тёплый контейнер сессии занят другим запуском дольше {wait:.0f} с"
                return ExecOutcome(f"{busy}: {MAIN_HINT}" if deadline is not None else f"{busy}: повторите позже",
                                   warm_id=warm.warm_id)
            try:
                return self._exec_locked(warm, owner, lang, code, plan, deadline)
            finally:
                warm.lock.release()
        finally:
            with self._guard:
                warm.users -= 1
                warm.last_used = self._clock()

    def sweep(self) -> int:
        """Погасить простаивающие и сломанные (кадр без запроса); сколько погашено."""
        idle_s = env_int(IDLE_ENV, DEFAULT_IDLE_S)
        idle = f"простой дольше {idle_s} с ({IDLE_ENV})"
        with self._guard:
            gone = [(w, idle) for w in idle_ones(self._warm.values(), self._clock(), idle_s)]
            gone += [(w, BROKEN_REASON) for w in self._warm.values()
                     if w.users == 0 and w.channel is not None and w.channel.broken and all(w is not g for g, _ in gone)]
            for w, reason in gone:
                del self._warm[w.session]
                if w.channel is not None:
                    self._lost[w.session] = reason
        for w, reason in gone:
            self._stop(w, reason)
        return sum(1 for w, _ in gone if w.name)

    def stop_all(self, reason: str) -> None:
        """Все контейнеры (остановка шлюза); запуск, который идёт, оборвётся."""
        with self._guard:
            gone = list(self._warm.values())
            self._warm.clear()
        for w in gone:
            self._stop(w, reason)

    def _take(self, session: str) -> tuple[Warm | None, list[tuple[Warm, str]], str | None]:
        """Контейнер сессии (новая запись без процесса, если нет) с отметкой пользователя; при потолке - вытеснение
        давнего свободного. (контейнер, вытесненные с причиной, отказ)."""
        evicted = []
        with self._guard:
            warm = self._warm.get(session)
            if warm is None:
                cap = max(1, env_int(MAX_ENV, DEFAULT_MAX))
                reason = f"вытеснен: тёплых контейнеров не больше {cap} ({MAX_ENV})"
                while len(self._warm) >= cap:
                    gone = victim(self._warm.values())
                    if gone is None:
                        return None, evicted, (f"Все {cap} тёплых контейнеров заняты запусками: повторите позже или "
                                               "возьмите sandbox_run")
                    del self._warm[gone.session]
                    if gone.channel is not None:
                        self._lost[gone.session] = reason
                    evicted.append((gone, reason))
                warm_id = self._ids.setdefault(session, f"{WARM_PREFIX}{self._new_id()}")
                warm = self._warm[session] = Warm(session, warm_id, self._clock())
            warm.users += 1
            return warm, evicted, None

    def _exec_locked(self, warm: Warm, owner: sandbox.Owner, lang: str, code: str, plan: Budget,
                     deadline: float | None) -> ExecOutcome:
        notes = []
        with self._guard:
            gone = self._warm.get(warm.session) is not warm
        if gone:        # stop_all, пока запуск ждал замка: новый контейнер вне карты никто бы не погасил
            return ExecOutcome("Быстрая песочница останавливается вместе со шлюзом: повторите позже",
                               warm_id=warm.warm_id)
        if warm.channel is not None and warm.channel.broken:     # кадр без запроса после прошлого запуска
            self._drop(warm, BROKEN_REASON)
        if warm.channel is None:
            if error := self._start(warm, owner, deadline):
                return ExecOutcome(error, warm_id=warm.warm_id)
            with self._guard:
                lost = self._lost.pop(warm.session, None)
            if lost:
                notes.append(f"Тёплый контейнер поднят заново ({lost}): прежнее состояние потеряно - переменные и "
                             "импорты прежних запусков, файлы в /work на месте")
        channel = warm.channel          # stop_all из другого потока обнуляет поле - запуск держит свой канал
        # nonce на вызов: код в контейнере его не видит (sandbox_repl), заранее записанный кадр не подходит
        nonce = secrets.token_hex(NONCE_BYTES)
        entry = {"command": sandbox.mask_secrets(code[:sandbox.JOURNAL_COMMAND_MAX_CHARS]), "lang": lang,
                 "by": owner.by}
        started = time.perf_counter()
        run_deadline = time.monotonic() + plan.run_s
        run_deadline = min(run_deadline, deadline) if deadline is not None else run_deadline
        try:
            channel.send({"nonce": nonce, "lang": lang, "code": code, "limit": sandbox.OUTPUT_MAX_CHARS},
                         remaining(run_deadline, plan.run_s))
            reply = channel.recv(remaining(run_deadline, plan.run_s))
            if reply.get("nonce") != nonce:
                raise ChannelClosed("исполнитель ответил не на этот запуск")
        except TimeoutError:
            limit = plan.total_s if deadline is not None else plan.run_s
            reason = f"запуск не уложился в {limit:.0f} с"
            self._drop(warm, reason)
            self._record(warm.warm_id, KIND_EXEC, **entry, code=None, error=reason)
            tail = MAIN_HINT if deadline is not None else "долгую работу - в sandbox_run с timeout"
            return ExecOutcome("\n".join([*notes, f"Не уложилось в {limit:.0f} с: тёплый контейнер остановлен, "
                                          f"переменные потеряны, следующий вызов поднимет новый; {tail}"]),
                               warm_id=warm.warm_id)
        except ChannelClosed as e:
            self._drop(warm, str(e))
            self._record(warm.warm_id, KIND_EXEC, **entry, code=None, error=str(e))
            return ExecOutcome("\n".join([*notes, f"Тёплый контейнер оборвался ({e}): переменные потеряны, следующий "
                                          "вызов поднимет новый"]), warm_id=warm.warm_id)
        wall_ms = (time.perf_counter() - started) * MS_PER_S
        self._record(warm.warm_id, KIND_EXEC, **entry, code=reply.get("code"), ms=reply.get("ms"))
        return ExecOutcome("\n".join([*notes, result_text(reply)]), reply.get("code"), reply.get("ms"), wall_ms,
                           warm.warm_id)

    def _start(self, warm: Warm, owner: sandbox.Owner, deadline: float | None = None) -> str | None:
        """Поднять контейнер и дождаться готовности исполнителя (не дольше срока вызова основного агента); отказ -
        одна строка, строка warm_start в журнале."""
        def record(kind: str, **what) -> None:
            self._record(warm.warm_id, kind, **what)

        if error := sandbox.check_owner(self.root, warm.warm_id, owner, record):
            return error
        base = self.root / warm.warm_id
        limits = replace(self._limits or sandbox.limits_from_env(), network=sandbox_net.NONE)
        name = f"{CONTAINER_PREFIX}{self._new_id()}"
        wait = remaining(deadline, env_int(START_ENV, DEFAULT_START_S))
        started = time.perf_counter()
        try:
            base.mkdir(parents=True, exist_ok=True)
            channel = self._open(name, base, limits)
        except OSError as e:
            error = f"Docker не запустился ({e}): установите Docker или задайте SAM_SANDBOX_DOCKER"
            record(KIND_START, container=name, error=error)
            return error
        try:
            ready = channel.recv(wait)
            if not ready.get("ready"):
                raise ChannelClosed("исполнитель не сообщил о готовности")
        except TimeoutError:
            channel.close()
            error = f"Тёплый контейнер не поднялся за {wait:.0f} с " + (
                f"({MAIN_TIMEOUT_ENV}): {MAIN_HINT}" if deadline is not None else f"({START_ENV}): проверьте Docker и повторите")
        except ChannelClosed as e:
            code = channel.close()
            error = sandbox.docker_error(code, channel.stderr_tail(), limits.image) or (
                f"Тёплый контейнер не поднялся ({e}; docker: {sandbox._last_line(channel.stderr_tail())})")
        else:
            warm.channel, warm.name = channel, name
            record(KIND_START, container=name, session=owner.session,
                   ms=round((time.perf_counter() - started) * MS_PER_S))
            return None
        record(KIND_START, container=name, error=error)
        return error

    def _drop(self, warm: Warm, reason: str) -> None:
        """Погасить контейнер, который держит запуск (таймаут, обрыв); запись сессии остаётся без процесса."""
        with self._guard:
            self._lost[warm.session] = reason
        self._stop(warm, reason)

    def _stop(self, warm: Warm, reason: str) -> None:
        channel, warm.channel = warm.channel, None
        if channel is not None:
            channel.close()
            self._record(warm.warm_id, KIND_STOP, container=warm.name, reason=reason)

    def _record(self, warm_id: str, kind: str, **what) -> None:
        """Журнал тёплого контейнера рядом с его папкой, как у задачи: старт, запуск, остановка. Код запуска пишется
        обрезанным и с маской секретов - это то, что SAM выполнила, как команда в журнале sandbox_run; вывод не
        пишется, он в ответе инструмента."""
        path = self.root / f"{warm_id}{sandbox.JOURNAL_SUFFIX}"
        sandbox.append_journal(path, sandbox.journal_line(kind, sandbox._now(), **what))


_pool: WarmPool | None = None
_pool_guard = threading.Lock()


def pool() -> WarmPool:
    """Пул шлюза: один на процесс."""
    global _pool
    with _pool_guard:
        if _pool is None:
            _pool = WarmPool()
        return _pool


def sweep_every_s() -> int:
    return max(1, env_int(SWEEP_ENV, DEFAULT_SWEEP_S))
