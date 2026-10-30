"""Песочница (SAM-83, ADR-016): код, который пишет SAM, выполняется в одноразовом контейнере.

Каждый запуск - новый `docker run --rm` без сети, с лимитами памяти, процессора, числа процессов и времени,
от uid 1000 без capabilities и с корнем только для чтения. В контейнер монтируется одна папка задачи
`SAM_SANDBOX_DIR/<id>` как /work; переменные окружения gateway и данные SAM туда не попадают. Результат -
код выхода, обрезанный вывод и файлы папки задачи; HTML и картинки из неё gateway отдаёт по
`/sandbox/<id>/<файл>` с CSP sandbox (main.py), монитор показывает их во фрейме.

Пакеты (`packages`) проверяет и описывает `sandbox_pkgs.py`: точная версия из PyPI или npm, OSV до установки;
ставятся отдельным запуском с сетью песочницы, команда задачи после этого идёт со своей сетью (по умолчанию без).

Каждый запуск и отказ оставляют строку JSONL в журнале задачи `SAM_SANDBOX_DIR/<id>.log` - рядом с папкой задачи,
а не в ней: код в контейнере видит только /work и журнал не перепишет.

Решения (пути, команда docker, разбор параметров, строка журнала) отделены от запуска процесса: runner и kill
приходят параметрами, по умолчанию - запуск CLI агентов кода (`tools/coding_agents.run_cli`, убивает дерево
процессов) и `docker kill`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import sandbox_clone, sandbox_export, sandbox_import, sandbox_net, sandbox_pkgs
from .tools.coding_agents import CliResult, run_cli
from .env import env_int

_REPO = Path(__file__).resolve().parent.parent
SANDBOX_DIR = Path(os.getenv("SAM_SANDBOX_DIR", str(_REPO / "data" / "sandbox")))
DOCKER = os.getenv("SAM_SANDBOX_DOCKER", "docker")
CONTAINER_PREFIX = "sam-sandbox-"
WORKDIR_IN = "/work"
SANDBOX_USER = "1000:1000"          # не root; uid без имени в образе - так и задумано
JOB_ID = re.compile(r"^[0-9a-f]{12}$")
# Запуск короткий: скрипт, график, страница. Потолок - чтобы модель не держала контейнер часами
DEFAULT_TIMEOUT_S = 60
TIMEOUT_ENV = "SAM_SANDBOX_TIMEOUT_S"
MAX_TIMEOUT_ENV = "SAM_SANDBOX_MAX_TIMEOUT_S"
DEFAULT_MAX_TIMEOUT_S = 600
OUTPUT_MAX_CHARS = 4000             # вывод в контекст воркера
FILES_LISTED = 50                   # столько файлов папки задачи называется в ответе
FILE_MAX_BYTES = 2_000_000          # один файл, записанный tool-ом
KILL_TIMEOUT_S = 15
PREVIEW_SUFFIXES = (".html", ".htm", ".svg", ".png", ".jpg", ".jpeg", ".gif")
# Свой образ: Python, Node, npm, git, matplotlib - сети в контейнере нет, поставить внутри нельзя (docker/Dockerfile.sandbox)
DEFAULT_IMAGE = "sam-sandbox:2"
IMAGE_BUILD = f"docker build -f docker/Dockerfile.sandbox -t {DEFAULT_IMAGE} docker"
# docker run выходит с 125, когда не запустил контейнер сам; код команды пользователя - другой, и её stderr не
# путается с ошибкой docker. Образ берётся только локальный (--pull=never): ответ один - «no such image»
DOCKER_RUN_ERROR = 125
IMAGE_MISSING_MARKERS = ("no such image", "unable to find image")
DOCKER_DOWN_MARKERS = ("cannot connect to the docker daemon", "error during connect", "docker daemon is not running")
# Журнал задачи: файл рядом с папкой задачи; виды строк - запуск и сеть, дальше import/clone/packages/export
JOURNAL_SUFFIX = ".log"
KIND_RUN, KIND_NETWORK, KIND_PACKAGES, KIND_IMPORT, KIND_CLONE = "run", "network", "packages", "import", "clone"
KIND_EXPORT, KIND_EXPORT_PC = "export", "export_pc"
# Установка пакетов - отдельный запуск с сетью; pandas с зависимостями колёсами - десятки секунд, с запасом
PKGS_TIMEOUT_ENV, DEFAULT_PKGS_TIMEOUT_S = "SAM_SANDBOX_PKGS_TIMEOUT_S", 300
# Поле osv строки packages: запрошенные версии чистые, OSV отказал (или не ответил), до OSV не дошло (разбор)
OSV_CLEAN, OSV_REFUSED, OSV_NOT_ASKED = "clean", "refused", "not_asked"
# Папки пакетов в папке задачи: в списке файлов и в выборе показа их нет - там тысячи файлов чужих библиотек
PACKAGE_DIRS = (sandbox_pkgs.PKGS_DIR, sandbox_pkgs.NPM_DIR)
TMP_PREFIX = ".sam-"                # временный файл шлюза в папке задачи перед os.replace
# Показ файла задачи читается в память целиком: потолок выше FILE_MAX_BYTES - страница с графиками внутри бывает
# больше файла, который пишет модель; тот же порядок, что у прокси приложений (SAM_APPS_PROXY_MAX_BYTES)
SERVE_MAX_ENV, DEFAULT_SERVE_MAX_BYTES = "SAM_SANDBOX_SERVE_MAX_BYTES", 20_000_000
# Один запуск задачи за раз: второй sandbox_run той же задачи ждёт, пока первый выйдет вместе с контейнером
JOB_WAIT_ENV, DEFAULT_JOB_WAIT_S = "SAM_SANDBOX_JOB_WAIT_S", 60
JOURNAL_COMMAND_MAX_CHARS = 500     # команда в строке журнала; вывод в журнал не идёт, он в ответе tool-а
# Секреты в команде (git clone с токеном, curl с заголовком) в журнал не попадают: шаблон и замена. Маскируется
# уже обрезанный текст, а все квантификаторы ограничены: шаблон с префиксом без потолка на длинной команде без «=»
# работает квадратично (40 000 символов - 62 с). Лучше скрыть лишнее (monkey=banana), чем пропустить секрет;
# имя во множественном числе (--keys 3, keys=3) секретом не считается
MASK = "***"
# api_key, X-Auth-Token, DB_PASSWORD; имя начинается после не-буквы - шаблон пробует только начала слов
_SECRET = r"(?:token|key|password|passwd|secret)"
_NAME = rf"(?<![\w-])[\w-]{{0,64}}{_SECRET}"
_VALUE = r"""(?:'[^']{0,512}'|"[^"]{0,512}"|[^\s&'"]{1,512})"""   # в кавычках - целиком, с пробелами
SECRET_PATTERNS = (
    (re.compile(r"(://)[^/\s:@]{1,256}:[^/\s@]{1,512}@"), rf"\1{MASK}:{MASK}@"),        # ://user:pass@
    (re.compile(r"(://)[^/\s:@]{1,512}@"), rf"\1{MASK}@"),                               # ://token@
    (re.compile(r"""((?:^|\s)(?:-u|--user)\s{1,8}['"]?)[^\s'":]{1,256}:[^\s'"]{1,512}"""),
     rf"\1{MASK}:{MASK}"),                                                                # curl -u user:pass
    (re.compile(r"(?i)(authorization\s{0,8}:\s{0,8}(?:(?:bearer|basic|token)\s{1,8})?)[^\s'\"]{1,512}"),
     rf"\1{MASK}"),
    (re.compile(r"(?i)(\bcookie\s{0,8}:\s{0,8})[^'\"\r\n]{1,1024}"), rf"\1{MASK}"),   # Cookie: a=1; b=2
    (re.compile(rf"(?i)(\"{_NAME}\"\s{{0,8}}:\s{{0,8}}\")[^\"]{{0,512}}"), rf"\1{MASK}"),  # JSON "api_key": "..."
    (re.compile(rf"(?i)({_NAME}\s{{0,8}}:\s{{0,8}})[^\s'\"]{{1,512}}"), rf"\1{MASK}"),     # X-Api-Key: ...
    (re.compile(rf"(?i)({_NAME}=){_VALUE}"), rf"\1{MASK}"),                               # token=, PASSWORD="x"
    (re.compile(rf"(?i)((?<![\w-])--[\w-]{{0,64}}{_SECRET}\s{{1,8}})(?!-){_VALUE}"), rf"\1{MASK}"),  # --token X
    (re.compile(r"((?:^|\s)-p)[^\s'\"]{1,512}"), rf"\1{MASK}"),                           # mysql -pS3cret
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|sk-[A-Za-z0-9_-]{16,255}"
                r"|xox[bpas]-[A-Za-z0-9-]{10,255}|AKIA[0-9A-Z]{16})\b"), MASK),          # голые токены
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limits:
    image: str
    network: str
    memory: str
    cpus: str
    pids: str


def limits_from_env() -> Limits:
    """Безопасные значения по умолчанию; ослабить изоляцию - явная строка в .env."""
    return Limits(
        image=os.getenv("SAM_SANDBOX_IMAGE", DEFAULT_IMAGE),
        network=os.getenv("SAM_SANDBOX_NETWORK", "none"),
        memory=os.getenv("SAM_SANDBOX_MEMORY", "512m"),
        cpus=os.getenv("SAM_SANDBOX_CPUS", "1"),
        pids=os.getenv("SAM_SANDBOX_PIDS", "128"),
    )


def container_name(job_id: str) -> str:
    return f"{CONTAINER_PREFIX}{job_id}"


def hardening(limits: Limits) -> list[str]:
    """Ограничения контейнера кода SAM, общие для песочницы и живых приложений (apps.py): лимиты, корень только
    для чтения, без capabilities и root, только локальный образ. Без -e и --env-file - окружение gateway в
    контейнер не передаётся."""
    return [
        "--memory", limits.memory, "--cpus", limits.cpus, "--pids-limit", limits.pids,
        "--read-only", "--tmpfs", "/tmp",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", SANDBOX_USER, "--pull=never",
    ]


def docker_argv(job_id: str, host_dir: Path, command: str, limits: Limits, docker: str = DOCKER) -> list[str]:
    """Команда одноразового запуска: папка задачи в /work, сеть из настройки."""
    return [
        docker, "run", "--rm", "--name", container_name(job_id),
        "--network", limits.network, *hardening(limits),
        "-v", f"{host_dir}:{WORKDIR_IN}", "-w", WORKDIR_IN,
        limits.image, "sh", "-c", command,
    ]


def job_path(job_id: str, root: Path) -> Path | None:
    return root / job_id if JOB_ID.match(job_id or "") else None


def inside(base: Path, name: str) -> Path | None:
    """Путь файла внутри папки задачи; абсолютный путь и выход наружу через .. - None."""
    if not name or Path(name).is_absolute() or Path(name).drive:
        return None
    try:            # симлинк, который сделал код задачи (на Windows - чужая точка повторного разбора), не роняет
        base = base.resolve()
        path = (base / name).resolve()
    except (OSError, RuntimeError):
        return None
    return path if path != base and path.is_relative_to(base) else None


def regular_file(path: Path) -> bool:
    """Обычный файл без ссылки: папку задачи пишет код задачи, а симлинк, FIFO или устройство увели бы шлюз на
    файл хоста или повесили бы его. На Windows симлинк и FIFO из контейнера Linux - точки повторного разбора, lstat
    показывает их обычными файлами, отличает только st_reparse_tag."""
    try:
        return is_regular(os.lstat(path))
    except OSError:
        return False


def is_regular(st) -> bool:
    return stat.S_ISREG(st.st_mode) and not getattr(st, "st_reparse_tag", 0)


def _plain_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not getattr(st, "st_reparse_tag", 0)


def write_files(base: Path, files: dict) -> str | None:
    """Записать файлы задачи; ошибка - одной строкой, ничего не записано."""
    planned = []
    for name, content in files.items():
        path = inside(base, str(name))
        if path is None:
            return f"Файл {name!r} вне папки задачи: нужен относительный путь без .."
        if os.path.lexists(path) and not regular_file(path):     # папка, FIFO, устройство: запись упала бы, повисла
            return f"Файл {name!r} в задаче - не обычный файл: удалите его командой или возьмите другое имя"
        text = content if isinstance(content, str) else str(content)
        if len(text.encode("utf-8")) > FILE_MAX_BYTES:
            return f"Файл {name!r} больше {FILE_MAX_BYTES} байт"
        planned.append((path.relative_to(base.resolve()).as_posix(), text))
    for name, text in planned:
        try:    # проверка выше - для понятного отказа; безопасность записи - в replace_job_file
            replace_job_file(base, name, text)
        except OSError as e:
            return f"Файл {name!r} не записан ({e.strerror or e}): путь в задаче занят ссылкой или не папкой"
    return None


def list_files(base: Path) -> list[str]:
    """Файлы задачи без поставленных пакетов (PACKAGE_DIRS) и без симлинков: ссылка из папки задачи на папку хоста
    не обходится, на файл хоста - не называется."""
    names = []
    if not _plain_dir(base):
        return names
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False, onerror=lambda e: None):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if _plain_dir(here / d) and not (here == base and d in PACKAGE_DIRS)]
        names += [(here / f).relative_to(base).as_posix() for f in filenames
                  if regular_file(here / f) and not f.startswith(TMP_PREFIX)]     # не временные файлы шлюза
    return sorted(names)


def preview_url(job_id: str, files: list[str]) -> str | None:
    """Что показать в мониторе: index.html, иначе первая страница или картинка."""
    if "index.html" in files:
        return f"/sandbox/{job_id}/index.html"
    shown = next((f for f in files if f.lower().endswith(PREVIEW_SUFFIXES)), None)
    return f"/sandbox/{job_id}/{shown}" if shown else None


def journal_path(job_id: str, root: Path) -> Path | None:
    """Журнал задачи - файл рядом с её папкой: в /work он не монтируется."""
    return root / f"{job_id}{JOURNAL_SUFFIX}" if JOB_ID.match(job_id or "") else None


def journal_line(kind: str, at: datetime, **what) -> str:
    """Строка JSONL журнала задачи: время, вид и что произошло (команда, режим сети, код выхода, отказ)."""
    return json.dumps({"time": at.isoformat(timespec="seconds"), "kind": kind, **what}, ensure_ascii=False)


def mask_secrets(text: str) -> str:
    """Скрыть секреты в команде для журнала: данные входа в URL, заголовок Authorization, token=, key=,
    password=, secret= (значение до пробела или &)."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def append_journal(path: Path, line: str) -> None:
    """Дописать строку; журнал не мешает запуску - сбой записи одной строкой в лог gateway."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("Журнал песочницы %s не записан (%s): проверьте права на SAM_SANDBOX_DIR", path, e)


def _now() -> datetime:
    return datetime.now().astimezone()


def docker_kill(name: str) -> None:
    subprocess.run([DOCKER, "kill", name], capture_output=True, timeout=KILL_TIMEOUT_S)


@dataclass(frozen=True)
class RunOutcome:
    job_id: str
    text: str                       # ответ tool-а: одна строка ошибки или итог
    files: tuple[str, ...] = ()
    preview: str | None = None
    code: int | None = None
    network: str = sandbox_net.NONE     # режим, который просила задача
    network_granted: bool = False       # сеть с интернетом выдана (файрвол встал)


def run(params: dict, root: Path | None = None, limits: Limits | None = None,
        runner: Callable[..., CliResult] = run_cli, kill: Callable[[str], None] = docker_kill,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
        net_ensure: Callable[[Callable], str | None] = sandbox_net.ensure,
        now: Callable[[], datetime] = _now,
        osv: Callable[[list], str | None] = sandbox_pkgs.osv_check,
        owner: Owner | None = None) -> RunOutcome:
    """Оркестратор: проверки, папка задачи, файлы, пакеты, запуск, итог; запуск, пакеты и отказ - строкой в
    журнал задачи. owner - сессия разговора, от имени которой ход: задача другой сессии - отказ (None - без
    проверки, вызов изнутри шлюза)."""
    root = Path(root or SANDBOX_DIR)
    command = str(params.get("command") or "").strip()
    job_id = str(params.get("job") or "").strip() or new_id()
    base = job_path(job_id, root)
    if base is None:      # id не из песочницы - имени журнала у такой задачи нет
        return RunOutcome(job_id, f"job {job_id!r} не из песочницы: нужен id прошлого запуска или пусто")
    journal = journal_path(job_id, root)

    def record(kind: str, **what) -> None:
        append_journal(journal, journal_line(kind, now(), **what))

    # Пока идёт запуск, его контейнер жив и пишет в папку задачи; второй запуск той же задачи писал бы файлы рядом
    # с ним. Один запуск задачи за раз: запись шлюза (файлы, lock-файлы) идёт, когда контейнеров задачи нет
    held = _job_lock(root, job_id)
    wait = env_int(JOB_WAIT_ENV, DEFAULT_JOB_WAIT_S)
    if not held.lock.acquire(timeout=wait):
        error = f"Задача {job_id} занята другим запуском дольше {wait} с: повторите позже или начните новую задачу"
        record(KIND_RUN, command=mask_secrets(command[:JOURNAL_COMMAND_MAX_CHARS]), code=None, error=error)
        return RunOutcome(job_id, error)
    try:
        if error := check_owner(root, job_id, owner, record):
            record(KIND_RUN, command=mask_secrets(command[:JOURNAL_COMMAND_MAX_CHARS]), code=None, error=error)
            return RunOutcome(job_id, error)
        return _run_job(params, job_id, base, command, limits, runner, kill, net_ensure, osv, record)
    finally:
        held.lock.release()


# ─── сессия задачи ───────────────────────────────────────────────────────────
# Задача принадлежит сессии разговора, в которой её начали: id задачи виден в мониторе, событиях и истории, и
# без записи сессии агент другой сессии (воркер, агент с урезанными правами) работал бы в чужой папке. Сессия - файл
# <job>.owner.json рядом с журналом, вне /work: код задачи его не перепишет; отдельный файл, а не строка журнала,
# - создаётся атомарно (O_EXCL), и первый занявший выигрывает гонку. Задача без файла (начата до записи сессий)
# достаётся первой сессии, которая к ней обратится: все такие задачи создала домашняя SAM одного ПК, а
# отказ потерял бы их; привязка пишется в журнал строкой owner. Агенты основной модели вне шлюза (POST /sam/agents,
# MCP sam) идут от сессии SAM_AGENT_PARENT_SESSION (по умолчанию voice) и делят задачи с голосовой беседой: так
# задумано - их создаёт основная модель, которая ведёт эту беседу, и путь закрыт ключом привилегий.
OWNER_SUFFIX = ".owner.json"
KIND_OWNER = "owner"


@dataclass(frozen=True)
class Owner:
    session: str            # сессия разговора; у воркера - сессия его создателя
    by: str = ""            # кто внутри сессии: id задачи воркера, пусто - основной агент


def owner_path(job_id: str, root: Path) -> Path:
    return root / f"{job_id}{OWNER_SUFFIX}"


def check_owner(root: Path, job_id: str, owner: Owner | None, record: Callable[..., None]) -> str | None:
    """Задача этой сессии или ничья (тогда становится этой) - None; другой сессии - отказ одной строкой."""
    if owner is None:
        return None
    path = owner_path(job_id, root)
    for _ in range(2):          # второй круг - если файл заняли между чтением и созданием
        try:
            found = json.loads(path.read_text(encoding="utf-8")).get("session")
        except FileNotFoundError:
            found = None
        except (OSError, ValueError, AttributeError):
            return f"Сессия задачи {job_id} не прочитан - начните новую задачу без job"
        if found is not None:
            return None if found == owner.session else (
                f"Задача {job_id} другой сессии - начните новую задачу без job")
        try:
            root.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, JOB_FILE_MODE), "w",
                           encoding="utf-8") as f:
                json.dump({"session": owner.session, "by": owner.by}, f, ensure_ascii=False)
        except FileExistsError:
            continue
        except OSError as e:
            return f"Сессия задачи {job_id} не записан ({e.strerror or e}) - проверьте права на SAM_SANDBOX_DIR"
        record(KIND_OWNER, session=owner.session, by=owner.by)
        return None
    return f"Сессия задачи {job_id} не определён - повторите"


class _JobLock:
    """Замок задачи; слабая ссылка в словаре - замок живёт, пока его держит или ждёт запуск."""
    __slots__ = ("lock", "__weakref__")

    def __init__(self):
        self.lock = threading.Lock()


_JOB_LOCKS: "weakref.WeakValueDictionary[str, _JobLock]" = weakref.WeakValueDictionary()
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(root: Path, job_id: str) -> _JobLock:
    key = f"{root.resolve()}/{job_id}"
    with _JOB_LOCKS_GUARD:
        held = _JOB_LOCKS.get(key)
        if held is None:
            held = _JOB_LOCKS[key] = _JobLock()
        return held


def _run_job(params: dict, job_id: str, base: Path, command: str, limits: Limits | None,
             runner: Callable[..., CliResult], kill: Callable[[str], None],
             net_ensure: Callable[[Callable], str | None], osv: Callable[[list], str | None],
             record: Callable[..., None]) -> RunOutcome:
    """Один запуск задачи под её замком: проверки, файлы, пакеты, команда, строка run в журнале."""
    network = str(params.get("network") or sandbox_net.NONE).strip().lower()
    asked = params.get("packages") or []
    asked = asked if isinstance(asked, list) else [asked]      # строка или мусор - разбор ответит одной строкой
    packages: list = []
    copy = None
    outcome = _check_network(job_id, network, command)
    if outcome is None and asked:      # разбор и OSV - до записи файлов: отказ ничего не оставляет
        packages, outcome = _vet_packages(job_id, asked, osv, record)
    if outcome is None and params.get("import"):    # копия с ПК - тоже проверяется вся до записи
        copy, outcome = _vet_import(job_id, base, params["import"], bool(packages), record)
    clone = None
    if outcome is None and params.get("clone"):
        clone, outcome = _vet_clone(job_id, base, params["clone"], record)
    export = None
    if outcome is None and params.get("export"):    # настройка и места на ПК - до записи; из задачи - после команды
        export, outcome = _vet_export(job_id, params["export"], record)
    outcome = outcome or _prepare(params, job_id, base, command, bool(packages), bool(copy or clone or export))
    notes = []
    if outcome is None and copy is not None:
        done = _copy_import(job_id, base, copy, record)
        outcome = done if isinstance(done, RunOutcome) else outcome
        notes += [] if isinstance(done, RunOutcome) else [done]
    if outcome is None and clone is not None:
        cloned = _clone(job_id, base, clone, limits, runner, kill, net_ensure, record)
        outcome = cloned if isinstance(cloned, RunOutcome) else outcome
        notes += [] if isinstance(cloned, RunOutcome) else [cloned]
    if outcome is None and packages:
        installed = _install(job_id, base, packages, limits, runner, kill, net_ensure, record, osv)
        outcome = installed if isinstance(installed, RunOutcome) else outcome
        notes += [] if isinstance(installed, RunOutcome) else [installed]
    if outcome is None and command:
        outcome = _launch(params, job_id, base, command, network, limits, runner, kill, net_ensure, record)
    tail = []
    if export is not None and outcome is not None and outcome.code not in (0, None):
        tail.append(f"Выгрузка на ПК не делалась: команда завершилась с кодом {outcome.code}")
    elif export is not None and (outcome is None or outcome.code == 0):
        sent = _export_pc(job_id, base, export, record)
        if isinstance(sent, RunOutcome) and outcome is None:
            outcome = sent
        else:
            tail.append(sent.text if isinstance(sent, RunOutcome) else sent)
    if outcome is None:   # только файлы, копия, пакеты или выгрузка, без команды: не запуск
        names = list_files(base)
        text = f"Файлы записаны в задачу {job_id}: {', '.join(names[:FILES_LISTED])}"
        return RunOutcome(job_id, "\n".join([*notes, text, *tail]), tuple(names), preview_url(job_id, names))
    if notes or tail:
        outcome = replace(outcome, text="\n".join([*notes, outcome.text, *tail]))
    # обрезать до маскировки: длина команды не ограничена, а маскировать - по обрезанному
    entry = {"command": mask_secrets(command[:JOURNAL_COMMAND_MAX_CHARS]), "network": network, "code": outcome.code}
    if outcome.code is None:
        entry["error"] = mask_secrets(outcome.text.splitlines()[0][:JOURNAL_COMMAND_MAX_CHARS]) if outcome.text else ""
    record(KIND_RUN, **entry)
    return outcome


def _check_network(job_id: str, network: str, command: str) -> RunOutcome | None:
    """Режим сети - до записи файлов: отказ ничего не оставляет в папке задачи.

    network=internet без command - отказ, а не тихая запись файлов: сеть выдаётся на запуск команды, и модель,
    которая её попросила, должна узнать, что сети не было, а не решить, что файлы скачались по сети."""
    if network not in sandbox_net.MODES:
        return RunOutcome(job_id, f"network - {' или '.join(sandbox_net.MODES)}")
    if network == sandbox_net.INTERNET and not command:
        return RunOutcome(job_id, "network=internet нужен вместе с command: сеть выдаётся на запуск команды",
                          network=network)
    return None


def _prepare(params: dict, job_id: str, base: Path, command: str, has_packages: bool = False,
             has_import: bool = False) -> RunOutcome | None:
    """Папка задачи и файлы; отказ - RunOutcome с одной строкой, готово - None."""
    files = params.get("files") or {}
    if not isinstance(files, dict):
        return RunOutcome(job_id, "files - объект {имя файла: текст}")
    if not command and not files and not has_packages and not has_import:
        return RunOutcome(job_id, "Нужна command (например, python main.py), files, import, clone и/или packages")
    if has_packages and (name := _reserved_file(base, files)):
        return RunOutcome(job_id, f"Файл {name!r} при packages не передаётся: lock-файлы, конфиги pip и npm и "
                                  "папки пакетов задачи пишет установка")
    base.mkdir(parents=True, exist_ok=True)
    if error := write_files(base, files):
        return RunOutcome(job_id, error)
    return None


def _vet_import(job_id: str, base: Path, items, has_packages: bool,
                record: Callable[..., None]) -> tuple[sandbox_import.Plan | None, RunOutcome | None]:
    """Источники внутри корней, план копии, потолки и места в задаче - до записи; отказ - строка import в журнале."""
    asked = [str(i.get("from") if isinstance(i, dict) else i)[:JOURNAL_COMMAND_MAX_CHARS]
             for i in (items if isinstance(items, list) else [items])]
    # корень, который задевает папку задач или данные SAM, не берётся: код задачи писал бы в источник импорта
    parsed, error = sandbox_import.parse(items, sandbox_import.roots(protected=(SANDBOX_DIR, _REPO / "data")))
    copy = None
    if not error:
        try:
            copy, error = sandbox_import.plan(parsed, lambda to: _import_dest_error(base, to, has_packages))
        except OSError as e:
            error = f"import: источник не прочитан ({e.strerror or e}) - проверьте путь"
    if error:
        record(KIND_IMPORT, sources=asked, error=error)
        return None, RunOutcome(job_id, error)
    return copy, None


def _import_dest_error(base: Path, to: str, has_packages: bool) -> str | None:
    path = inside(base, to)
    if path is None or _parts(to) is None:
        return f"import: {to!r} вне папки задачи - нужен относительный путь без .."
    if os.path.lexists(path) and not regular_file(path):
        return f"import: {to!r} в задаче занят папкой или спецфайлом - возьмите другое to"
    if has_packages and sandbox_pkgs.reserved(path.relative_to(base.resolve()).as_posix()):
        return f"import: {to!r} при packages не копируется - lock-файлы и папки пакетов пишет установка"
    return None


def _copy_import(job_id: str, base: Path, copy: sandbox_import.Plan, record: Callable[..., None]) -> RunOutcome | str:
    """Байты источников в папку задачи безопасной записью; строка import в журнале; итог - строка для ответа.
    Сбой посреди копии оставляет скопированное до него - ответ это называет."""
    skipped = copy.skipped[:sandbox_import.SKIPPED_LISTED]
    noted = {"files": len(copy.files), "bytes": copy.bytes,
             "sources": sorted({str(f.source) for f in copy.files})[:sandbox_import.SKIPPED_LISTED],
             **({"skipped": skipped, "skipped_total": len(copy.skipped)} if copy.skipped else {})}
    for done, planned in enumerate(copy.files):
        try:
            replace_job_bytes(base, planned.to, sandbox_import.read_source(planned))
        except OSError as e:
            error = (f"import: {planned.shown} не скопирован ({e.strerror or e}); скопировано до него - {done} "
                     "файлов, повторите")
            record(KIND_IMPORT, **noted, error=error)
            return RunOutcome(job_id, error)
    record(KIND_IMPORT, **noted, code=0)
    tail = f", пропущено {len(copy.skipped)} (ссылки, спецфайлы, секреты по маскам)" if copy.skipped else ""
    return f"Скопировано с ПК: {len(copy.files)} файлов, {copy.bytes} байт{tail}"


def _vet_clone(job_id: str, base: Path, spec,
               record: Callable[..., None]) -> tuple[sandbox_clone.Clone | None, RunOutcome | None]:
    """URL, ref и папка клона до записи; папка - свободное имя внутри задачи. Отказ - строка clone в журнале."""
    clone, error = sandbox_clone.parse(spec)
    if not error:
        path = inside(base, clone.dir)
        if path is None:
            error = f"clone: dir {clone.dir!r} вне папки задачи"
        elif os.path.lexists(path):
            error = f"clone: папка {clone.dir!r} в задаче уже есть - возьмите другой dir"
    if error:
        url = spec.get("url") if isinstance(spec, dict) else spec
        record(KIND_CLONE, url=mask_secrets(str(url)[:JOURNAL_COMMAND_MAX_CHARS]), error=error)
        return None, RunOutcome(job_id, error)
    return clone, None


def _clone(job_id: str, base: Path, clone: sandbox_clone.Clone, limits: Limits | None,
           runner: Callable[..., CliResult], kill: Callable[[str], None],
           net_ensure: Callable[[Callable], str | None], record: Callable[..., None]) -> RunOutcome | str:
    """Клон отдельным запуском с сетью песочницы; итог - строка для ответа, отказ - RunOutcome."""
    noted = {"url": mask_secrets(clone.url), "ref": clone.ref, "dir": clone.dir}

    def refuse(error: str) -> RunOutcome:
        record(KIND_CLONE, **noted, error=error)
        return RunOutcome(job_id, error, network=sandbox_net.INTERNET)

    limits = limits or limits_from_env()
    net = _network_limits(job_id, sandbox_net.INTERNET, limits, runner, net_ensure, record)
    if isinstance(net, RunOutcome):
        return refuse(net.text)
    res = _container(job_id, base, sandbox_clone.command(clone), net,
                     env_int(sandbox_clone.TIMEOUT_ENV, sandbox_clone.DEFAULT_TIMEOUT_S), runner, kill)
    size = None if isinstance(res, RunOutcome) else sandbox_clone.size_kb(res.out)
    ceiling = sandbox_clone.max_kb()
    if isinstance(res, RunOutcome) or res.code != 0 or size is None or size > ceiling:
        # недоклон или клон больше потолка в задаче не остаётся; убрать - без сети
        cleaned = _container(job_id, base, sandbox_clone.cleanup(clone), limits, KILL_TIMEOUT_S, runner, kill)
        left = "" if not isinstance(cleaned, RunOutcome) and cleaned.code == 0 else (
            f"; убрать папку {clone.dir} не удалось - удалите её командой или начните новую задачу")
        if isinstance(res, RunOutcome):
            return refuse(f"Клон не сделан ({res.text}){left}")
        if res.code != 0:
            return refuse(f"Клон не сделан (код {res.code}: {mask_secrets(sandbox_clone.git_error(res.err))}); "
                          f"репозиторий больше {ceiling} КБ обрывается так же - потолок {sandbox_clone.MAX_KB_ENV}"
                          f"{left}")
        return refuse(f"Клон {size if size is not None else '?'} КБ больше потолка {ceiling} КБ "
                      f"({sandbox_clone.MAX_KB_ENV}): считается пакет git и рабочее дерево вместе, примерно вдвое "
                      f"больше сжатого репозитория{left or ' - убран'}")
    record(KIND_CLONE, **noted, code=0, size_kb=size)
    ref = f" ({clone.ref})" if clone.ref else ""
    return f"Склонировано {clone.url}{ref} в {clone.dir}: {size if size is not None else '?'} КБ"


def _vet_export(job_id: str, items, record: Callable[..., None]) -> tuple[tuple | None, RunOutcome | None]:
    """Папка экспорта и свободные места в ней - до записи в задачу. (папка, пункты) или отказ одной строкой."""
    folder, error = sandbox_export.export_dir(protected=(SANDBOX_DIR, _REPO / "data", _REPO))
    parsed = []
    if not error:
        parsed, error = sandbox_export.parse(items)
    for item in parsed if not error else ():
        if error := sandbox_export.place_error(folder, item.to):
            break
    if error:
        record(KIND_EXPORT_PC, error=error)
        return None, RunOutcome(job_id, error)
    return (folder, parsed), None


def _export_pc(job_id: str, base: Path, export: tuple, record: Callable[..., None]) -> RunOutcome | str:
    """После команды: план всех пунктов (без ссылок, .git, пакетов и секретов, с потолками), снова свободно ли
    место, затем копия байтами. Итог - строка, отказ - RunOutcome; строка export_pc в журнале."""
    folder, items = export
    files, skipped, total = [], [], 0
    ceilings = (env_int(sandbox_export.MAX_FILES_ENV, sandbox_export.DEFAULT_MAX_FILES),
                env_int(sandbox_export.MAX_BYTES_ENV, sandbox_export.DEFAULT_MAX_BYTES))
    error = None
    for item in items:
        error = sandbox_export.place_error(folder, item.to) or _export_pc_plan(base, item, files, skipped, ceilings)
        if error:
            break
    total = sum(size for _, _, size in files)
    if not error and not files:
        error = "export: копировать нечего - источник пуст или всё в нём пропущено"
    error = error or sandbox_export.clash([out for _, out, _ in files])     # и имена внутри папок пунктов
    risky = []
    for rel, out, size in files if not error else ():
        try:
            data = read_regular(base, rel, size)
            if data is None:
                skipped.append(f"{rel} (сменился во время копии)")
                continue
            sandbox_export.mark_of_the_web(replace_job_bytes(folder, out, data, exclusive=True))
            risky += [sandbox_export.shown(out)] if sandbox_export.risky(out) else []
        except FileExistsError:
            error = f"export: {sandbox_export.shown(out)} уже есть - перезаписи нет, возьмите другое to"
            break
        except OSError as e:
            error = f"export: {rel} не выгружен ({e.strerror or e}) - повторите"
            break
    noted = {"to": [sandbox_export.shown(i.to) for i in items], "files": len(files), "bytes": total,
             **({"skipped": skipped[:EXPORT_SKIPPED_LISTED], "skipped_total": len(skipped)} if skipped else {}),
             **({"risky": risky[:EXPORT_SKIPPED_LISTED]} if risky else {})}
    record(KIND_EXPORT_PC, **noted, **({"error": error} if error else {"code": 0}))
    if error:
        return RunOutcome(job_id, error)
    tail = f", пропущено {len(skipped)} (ссылки, .git, пакеты, секреты, файлы Проводника)" if skipped else ""
    lines = [f"Выгружено на ПК: {', '.join(noted['to'])} - {len(files)} файлов, {total} байт{tail}"]
    if risky:
        lines.append(f"Среди них исполнимые или с макросами: {', '.join(risky[:EXPORT_SKIPPED_LISTED])}")
    return "\n".join(lines)


def _export_pc_plan(base: Path, item: sandbox_export.Item, files: list, skipped: list,
                    ceilings: tuple[int, int]) -> str | None:
    """Файлы пункта: (путь в задаче, путь в папке экспорта, размер); потолки - на все пункты сразу."""
    parts = _parts(item.source)
    patterns = sandbox_import.skip_patterns()
    if parts is None:
        return f"export: from {item.source!r} - путь внутри задачи без .."
    for i, part in enumerate(parts):
        if sandbox_export.skip_reason(part, i < len(parts) - 1 or _plain_dir(base.joinpath(*parts)), patterns):
            return (f"export: from {item.source!r} - путь через .git, пакеты, секреты или файлы Проводника "
                    "не выгружается")
        if i < len(parts) - 1 and not _plain_dir(base.joinpath(*parts[:i + 1])):   # ссылка по пути увела бы обход
            return f"export: from {item.source!r} - путь идёт через ссылку или не папку"
    source = base.joinpath(*parts)
    if regular_file(source):
        files.append((item.source, item.to, os.lstat(source).st_size))
        return _export_ceiling(files, ceilings)
    if not _plain_dir(source):
        return f"export: from {item.source!r} - нет такого файла или папки в задаче (или это ссылка)"
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False, onerror=lambda e: None):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            rel = (here / d).relative_to(base).as_posix()
            why = sandbox_export.skip_reason(d, True, patterns) or (None if _plain_dir(here / d) else "ссылка")
            if why:
                skipped.append(f"{rel} ({why})")
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            rel = (here / f).relative_to(base).as_posix()
            if f.startswith(TMP_PREFIX):
                continue
            why = sandbox_export.skip_reason(f, False, patterns) or (
                None if regular_file(here / f) else "ссылка или спецфайл")
            if why:
                skipped.append(f"{rel} ({why})")
                continue
            out = f"{item.to}/{(here / f).relative_to(source).as_posix()}"
            files.append((rel, out, os.lstat(here / f).st_size))
            if error := _export_ceiling(files, ceilings):
                return error
    return None


def _export_ceiling(files: list, ceilings: tuple[int, int]) -> str | None:
    max_files, max_bytes = ceilings
    if len(files) > max_files:
        return f"export: больше {max_files} файлов ({sandbox_export.MAX_FILES_ENV}) - выгрузите папку уже"
    if sum(size for _, _, size in files) > max_bytes:
        return f"export: больше {max_bytes} байт ({sandbox_export.MAX_BYTES_ENV}) - выгрузите папку уже"
    return None


def _vet_packages(job_id: str, asked: list, osv: Callable[[list], str | None],
                  record: Callable[..., None]) -> tuple[list, RunOutcome | None]:
    """Разбор спецификаций и OSV до всего остального; отказ - одна строка и строка `packages` в журнале."""
    packages, error = sandbox_pkgs.parse(asked)
    verdict = OSV_NOT_ASKED if error else OSV_REFUSED
    error = error or osv(packages)
    if error:
        record(KIND_PACKAGES, requested=[str(a)[:JOURNAL_COMMAND_MAX_CHARS] for a in asked], osv=verdict,
               error=error)
        return [], RunOutcome(job_id, error)
    return packages, None


def _reserved_file(base: Path, files: dict) -> str | None:
    """Первый файл из files, который при packages передавать нельзя (sandbox_pkgs.reserved)."""
    root = base.resolve()
    for name in files:
        path = inside(base, str(name))
        if path is not None and sandbox_pkgs.reserved(path.relative_to(root).as_posix()):
            return str(name)
    return None


def _install(job_id: str, base: Path, packages: list, limits: Limits | None, runner: Callable[..., CliResult],
             kill: Callable[[str], None], net_ensure: Callable[[Callable], str | None],
             record: Callable[..., None], osv: Callable[[list], str | None]) -> RunOutcome | str:
    """Установка отдельным контейнером с сетью песочницы, OSV по зависимостям, затем lock-файлы задачи. Итог -
    строка для ответа; отказ - RunOutcome, при отказе после установки поставленное убирается, lock-файлы прежние."""
    requested = [str(p) for p in packages]
    texts = {}
    for name in (sandbox_pkgs.PIP_LOCK, sandbox_pkgs.NPM_MANIFEST):     # прошлые пины - до сети и установки
        texts[name], error = read_job_file(base, name)
        if error:
            record(KIND_PACKAGES, requested=requested, osv=OSV_CLEAN, error=error)
            return RunOutcome(job_id, error)
    plan = sandbox_pkgs.plan(packages, texts.get)
    noted = {"requested": requested, "osv": OSV_CLEAN, **({"dropped": list(plan.dropped)} if plan.dropped else {})}

    def refuse(error: str, **what) -> RunOutcome:
        record(KIND_PACKAGES, **noted, **what, error=error)
        return RunOutcome(job_id, error, network=sandbox_net.INTERNET)

    limits = limits or limits_from_env()
    net = _network_limits(job_id, sandbox_net.INTERNET, limits, runner, net_ensure, record)
    if isinstance(net, RunOutcome):
        return refuse(net.text)
    res = _container(job_id, base, sandbox_pkgs.install_command(plan), net,
                     env_int(PKGS_TIMEOUT_ENV, DEFAULT_PKGS_TIMEOUT_S), runner, kill)
    if isinstance(res, RunOutcome) or res.code != 0:
        why = res.text if isinstance(res, RunOutcome) else f"код {res.code}: {_last_line(res.err or res.out)}"
        return refuse(f"Пакеты не поставились ({why})")
    found, error = sandbox_pkgs.installed(res.out or "", {p.manager for p in packages})
    deps = sandbox_pkgs.unchecked(found, packages)
    if error := error or osv(deps):     # зависимости, которые pip и npm выбрали сами
        cleaned = _container(job_id, base, sandbox_pkgs.CLEANUP, limits, KILL_TIMEOUT_S, runner, kill)
        ok = not isinstance(cleaned, RunOutcome) and cleaned.code == 0
        tail = "поставленные пакеты убраны" if ok else (
            f"убрать поставленное не удалось - удалите {sandbox_pkgs.PKGS_DIR} и {sandbox_pkgs.NPM_DIR} задачи "
            f"{job_id} или начните новую задачу")
        return refuse(f"{error}; {tail}", dependencies=[str(d) for d in deps])
    lock_error = _write_locks(base, sandbox_pkgs.lock_files(plan))
    record(KIND_PACKAGES, **noted, dependencies=[str(d) for d in deps], code=0,
           **({"lock_error": lock_error} if lock_error else {}))
    return f"Пакеты поставлены: {', '.join(requested)}; зависимостей проверено в OSV: {len(deps)}"


def _write_locks(base: Path, files: dict[str, str]) -> str | None:
    """Lock-файлы задачи после успешной установки; сбой записи - строка для журнала, установку он не отменяет."""
    try:
        for name, text in files.items():
            replace_job_file(base, name, text)
    except OSError as e:
        return f"lock-файлы не записаны ({e}): проверьте права на SAM_SANDBOX_DIR"
    return None


def read_job_file(base: Path, name: str) -> tuple[str | None, str | None]:
    """Файл папки задачи, который шлюз читает сам. Папку пишет код задачи, поэтому только обычный файл: симлинк
    увёл бы чтение на файл хоста, FIFO или устройство повесили бы шлюз; не больше FILE_MAX_BYTES. Нет файла -
    (None, None); отказ - (None, строка)."""
    path = base / name
    fix = f"удалите {name} в задаче или начните новую задачу"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"{name} задачи не прочитан ({e.strerror}): {fix}"
    if not is_regular(st):
        return None, f"{name} задачи - не обычный файл (симлинк, папка или устройство): {fix}"
    if st.st_size > FILE_MAX_BYTES:
        return None, f"{name} задачи больше {FILE_MAX_BYTES} байт: {fix}"
    try:
        data = read_regular(base, name, FILE_MAX_BYTES)
    except OSError as e:
        return None, f"{name} задачи не прочитан ({e.strerror or e}): {fix}"
    if data is None:            # между lstat и open путь подменили на FIFO, ссылку или файл больше потолка
        return None, f"{name} задачи - не обычный файл или больше {FILE_MAX_BYTES} байт: {fix}"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{name} задачи не в UTF-8: {fix}"


# Доступ шлюза к папке задачи. Её пишет код задачи, и пока жив контейнер (приложение работает всё время, запуск
# песочницы - до выхода), он может между проверкой и открытием подменить файл или папку в пути на симлинк. На
# POSIX путь проходится от папки задачи дескрипторами: каждая папка открывается с O_NOFOLLOW относительно
# предыдущей, файл - тоже, тип проверяется по уже открытому дескриптору (fstat). На Windows ссылки и FIFO из
# контейнера Linux - точки повторного разбора, Win32 по ним не переходит, а открытие падает; там хватает
# inside() и fstat открытого файла.
# os.replace - тот же renameat, что os.rename, но в supports_dir_fd Python называет только rename
_POSIX_FD = os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
JOB_FILE_MODE = 0o644       # mkstemp даёт 0600: шлюз не под uid 1000 - код в контейнере свой lock не прочитал бы


def _parts(name: str) -> list[str] | None:
    """Части относительного пути без «.» и «..»: проход дескрипторами не должен подняться выше папки задачи."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if os.name == "nt" and any(":" in p for p in parts):    # «D:» сбросил бы путь на диск хоста, «a:b» - поток NTFS
        return None
    return parts if parts and ".." not in parts else None


def _open_dir(base: Path, parts: list[str], create: bool) -> int:
    """Дескриптор папки base/parts, пройденной без перехода по ссылкам; create - недостающие папки создаются."""
    fd = os.open(base, os.O_RDONLY | _DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(base: Path, name: str, limit: int) -> bytes | None:
    """Содержимое обычного файла папки задачи не больше limit байт; ссылка, FIFO, устройство, больше limit - None.
    Ошибка доступа - OSError."""
    parts = _parts(name)
    if parts is None:
        return None
    if _POSIX_FD:
        dfd = _open_dir(base, parts[:-1], create=False)
        try:
            fd = os.open(parts[-1], _READ_FLAGS, dir_fd=dfd)
        finally:
            os.close(dfd)
    else:
        path = inside(base, name)
        if path is None:
            return None
        fd = os.open(path, _READ_FLAGS)
    with os.fdopen(fd, "rb") as f:
        if not is_regular(os.fstat(f.fileno())):
            return None
        data = f.read(limit + 1)
    return data if len(data) <= limit else None


def replace_job_file(base: Path, name: str, text: str) -> None:
    """Текстовый файл задачи в UTF-8 как есть: переводы строк не меняются - на Windows CRLF ломал #!/bin/sh в
    контейнере Linux."""
    replace_job_bytes(base, name, text.encode("utf-8"))


def replace_job_bytes(base: Path, name: str, data: bytes, mode: int | None = None, exclusive: bool = False) -> Path:
    """Записать файл папки задачи заменой самого пути: временный файл в той же папке и os.replace. Симлинк на месте
    файла заменяется, его цель не трогается; папки пути проходятся без ссылок (_open_dir, на Windows - проверка
    каждой части и realpath перед записью). Права - mode, по умолчанию JOB_FILE_MODE. exclusive - без перезаписи:
    имя заняли раньше - FileExistsError (Windows: os.rename, POSIX: link + unlink). Возвращает путь файла."""
    mode = JOB_FILE_MODE if mode is None else mode
    parts = _parts(name)
    if parts is None:
        raise OSError(f"{name!r}: нужен путь внутри папки задачи")
    if not _POSIX_FD:
        folder = base.joinpath(*parts[:-1])
        folder.mkdir(parents=True, exist_ok=True)
        _check_plain_way(base, parts[:-1])
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=TMP_PREFIX, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            # os.rename на Windows не заменяет существующий файл - то, что нужно без перезаписи
            (os.rename if exclusive else os.replace)(tmp, folder / parts[-1])
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return folder / parts[-1]
    dfd = _open_dir(base, parts[:-1], create=True)
    tmp = f"{TMP_PREFIX}{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), mode)     # umask процесса не урезает права
                f.write(data)
            if exclusive:       # link не заменяет существующее имя (EEXIST); временное имя потом убирается
                os.link(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                os.unlink(tmp, dir_fd=dfd)
            else:
                os.replace(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
            return base.joinpath(*parts)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def _check_plain_way(base: Path, parts: list[str]) -> None:
    """Windows без dir_fd: каждая папка пути - настоящая папка, и realpath ведёт туда же, куда путь; junction,
    подложенный после проверки места, - OSError."""
    path = base
    for part in parts:
        path = path / part
        if not _plain_dir(path):
            raise OSError(f"{part}: в пути ссылка или не папка")
    expected = os.path.normcase(str(base.resolve().joinpath(*parts)))
    if os.path.normcase(os.path.realpath(path)) != expected:
        raise OSError("путь подменён ссылкой")


def _last_line(text: str) -> str:
    lines = [s.strip() for s in (text or "").splitlines() if s.strip()]
    return lines[-1][:JOURNAL_COMMAND_MAX_CHARS] if lines else "без вывода"


def _network_limits(job_id: str, network: str, limits: Limits, runner: Callable[..., CliResult],
                    net_ensure: Callable[[Callable], str | None], record: Callable[..., None]) -> Limits | RunOutcome:
    """Сеть по запросу: интернет есть, дом и хост закрыты файрволом; правила не встали - отказ."""
    error = net_ensure(lambda argv: runner(argv, None, KILL_TIMEOUT_S))
    record(KIND_NETWORK, mode=network, granted=error is None, **({"error": error} if error else {}))
    if error:
        return RunOutcome(job_id, error, network=network)
    return replace(limits, network=sandbox_net.net_name())


def _launch(params: dict, job_id: str, base: Path, command: str, network: str, limits: Limits | None,
            runner: Callable[..., CliResult], kill: Callable[[str], None],
            net_ensure: Callable[[Callable], str | None], record: Callable[..., None]) -> RunOutcome:
    """Сеть задачи по запросу и запуск контейнера; режим сети уже проверен (_check_network)."""
    limits = limits or limits_from_env()
    if network != sandbox_net.INTERNET:
        return _execute(job_id, base, command, limits, _timeout(params.get("timeout")), runner, kill)
    net = _network_limits(job_id, network, limits, runner, net_ensure, record)
    if isinstance(net, RunOutcome):
        return net
    outcome = _execute(job_id, base, command, net, _timeout(params.get("timeout")), runner, kill)
    return replace(outcome, network=network, network_granted=True)


def _container(job_id: str, base: Path, command: str, limits: Limits, timeout: int,
               runner: Callable[..., CliResult], kill: Callable[[str], None]) -> CliResult | RunOutcome:
    """Один запуск контейнера: сырой результат или RunOutcome со строкой, если не запустился сам docker."""
    try:
        res = runner(docker_argv(job_id, base, command, limits), None, timeout)
    except OSError as e:
        return RunOutcome(job_id, f"Docker не запустился ({e}): установите Docker или задайте SAM_SANDBOX_DOCKER")
    if res.timed_out:
        kill(container_name(job_id))
        return RunOutcome(job_id, f"Песочница: не уложилась в {timeout} с, контейнер остановлен; "
                                  "сократите работу или поднимите timeout")
    error = docker_error(res.code, res.err, limits.image)
    return RunOutcome(job_id, error) if error else res


def docker_error(code: int | None, err: str | None, image: str) -> str | None:
    """Сбой самого docker по коду выхода и stderr - одной строкой; ошибка команды пользователя - None."""
    err = (err or "").lower()
    if code != 0 and any(m in err for m in DOCKER_DOWN_MARKERS):
        return "Docker не отвечает: запустите Docker Desktop и повторите"
    if code == DOCKER_RUN_ERROR and any(m in err for m in IMAGE_MISSING_MARKERS):
        build = f"соберите из корня SAM - {IMAGE_BUILD}" if image == DEFAULT_IMAGE else "соберите или скачайте его"
        return f"Образа песочницы {image} нет: {build}"
    return None


def _execute(job_id: str, base: Path, command: str, limits: Limits, timeout: int,
             runner: Callable[..., CliResult], kill: Callable[[str], None]) -> RunOutcome:
    started = time.monotonic()
    res = _container(job_id, base, command, limits, timeout, runner, kill)
    if isinstance(res, RunOutcome):
        return res
    names = list_files(base)
    preview = preview_url(job_id, names)
    secs = time.monotonic() - started
    output = (res.out + (f"\n[stderr]\n{res.err}" if res.err.strip() else "")).strip()
    lines = [f"Задача {job_id}: код выхода {res.code} за {secs:.1f} с",
             f"Вывод:\n{output[-OUTPUT_MAX_CHARS:]}" if output else "Вывода нет",
             f"Файлы: {', '.join(names[:FILES_LISTED])}" if names else "Файлов нет"]
    if preview:
        lines.append(f"Показ в мониторе: {preview}")
    return RunOutcome(job_id, "\n".join(lines), tuple(names), preview, res.code)


def _timeout(value) -> int:
    """Из параметра или SAM_SANDBOX_TIMEOUT_S; мусор - значение по умолчанию; не выше потолка."""
    default = env_int(TIMEOUT_ENV, DEFAULT_TIMEOUT_S)
    ceiling = env_int(MAX_TIMEOUT_ENV, DEFAULT_MAX_TIMEOUT_S)
    asked = _int(value, default)
    return max(1, min(asked if asked > 0 else default, ceiling))


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def served_file(root: Path, job_id: str, name: str) -> bytes | None:
    """Содержимое файла для /sandbox/<id>/<файл>: только обычный файл внутри папки задачи, прочитанный сразу из
    открытого дескриптора (read_regular) - путь, отданный на чтение позже, контейнер успел бы подменить."""
    base = job_path(job_id, Path(root))
    if base is None or not _plain_dir(base) or inside(base, name) is None:
        return None
    try:
        return read_regular(base, name, env_int(SERVE_MAX_ENV, DEFAULT_SERVE_MAX_BYTES))
    except OSError:
        return None


# ─── экспорт задачи наружу (в приложение) ─────────────────────────────────────
# Готовое из песочницы - копией в папку приложения (ADR-016 «Файлы», ADR-021). Пакеты .pkgs и node_modules
# переносятся: у приложения нет сети, зависимости - из песочницы; поэтому потолки шире, чем у импорта
EXPORT_MAX_FILES_ENV, DEFAULT_EXPORT_MAX_FILES = "SAM_APPS_EXPORT_MAX_FILES", 20_000   # node_modules - тысячи файлов
EXPORT_MAX_BYTES_ENV, DEFAULT_EXPORT_MAX_BYTES = "SAM_APPS_EXPORT_MAX_BYTES", 300_000_000
EXPORT_SKIP_DIRS = frozenset({".git"})  # история git приложению не нужна
EXPORT_SKIPPED_LISTED = 20              # столько пропущенных путей называет журнал


EXEC_FILE_MODE = 0o755                  # исполняемый в задаче (.pkgs/bin, бинарники npm) - исполняемый и в приложении


@dataclass
class Export:
    files: list[tuple[str, str, int, int]]      # (путь от папки задачи, путь в приложении, размер, права)
    skipped: list[str]
    bytes: int = 0


def export_job(job_id: str, sub: str, dest: Path, app_id: str, root: Path | None = None,
               owner: Owner | None = None) -> tuple[Export | None, str | None]:
    """Копия дерева задачи (или подпапки sub) в dest байтами под замком задачи: контейнер задачи в это время не
    пишет. Без ссылок, спецфайлов и .git, с потолками; запись - replace_job_bytes (папку приложения видит живой
    контейнер). Проверки и план - до записи. Задача другой сессии (owner) - отказ. Строка export в журнале задачи.
    (итог, отказ одной строкой)."""
    root = Path(root or SANDBOX_DIR)
    base = job_path(str(job_id or "").strip(), root)
    if base is None or not _plain_dir(base):
        return None, f"Задачи песочницы {job_id!r} нет: нужен job из ответа sandbox_run"
    parts = _parts(sub) if str(sub or "").strip() else []
    if parts is None:
        return None, f"from_path {sub!r} - путь внутри задачи без .."
    journal = journal_path(base.name, root)
    held = _job_lock(root, base.name)
    wait = env_int(JOB_WAIT_ENV, DEFAULT_JOB_WAIT_S)
    if not held.lock.acquire(timeout=wait):
        return None, f"Задача {base.name} занята запуском дольше {wait} с: повторите позже"
    try:
        def record(kind: str, **what) -> None:
            append_journal(journal, journal_line(kind, _now(), **what))
        if error := check_owner(root, base.name, owner, record):
            record(KIND_EXPORT, app=app_id, error=error)
            return None, error
        plan, error = _export_plan(base, parts)
        if error is None:
            error = _export_copy(base, plan, dest)
        line = {"app": app_id, "from": "/".join(parts) or ".", "files": len(plan.files) if plan else 0,
                "bytes": plan.bytes if plan else 0}
        if plan and plan.skipped:
            line |= {"skipped": plan.skipped[:EXPORT_SKIPPED_LISTED], "skipped_total": len(plan.skipped)}
        outcome = {"error": error} if error else {"code": 0}
        append_journal(journal, journal_line(KIND_EXPORT, _now(), **line, **outcome))
        return (None, error) if error else (plan, None)
    finally:
        held.lock.release()


def _export_plan(base: Path, parts: list[str]) -> tuple[Export | None, str | None]:
    """Что копировать: подпапка задачи (или вся), а при подпапке - ещё пакеты из корня задачи в корень приложения:
    pip и npm ставят их в корень, а приложению без сети они нужны."""
    source = base
    for part in parts:              # подпапка задачи - настоящая папка, не ссылка
        source = source / part
        if not _plain_dir(source):
            return None, f"from_path {'/'.join(parts)!r} - не папка задачи (нет её или это ссылка)"
    try:
        if not source.resolve().is_relative_to(base.resolve()):
            return None, f"from_path {'/'.join(parts)!r} - вне папки задачи"
    except OSError:
        return None, f"from_path {'/'.join(parts)!r} - не папка задачи"
    plan = Export([], [])
    ceilings = (env_int(EXPORT_MAX_FILES_ENV, DEFAULT_EXPORT_MAX_FILES),
                env_int(EXPORT_MAX_BYTES_ENV, DEFAULT_EXPORT_MAX_BYTES))
    trees = [(source, len(parts))]
    if parts and parts[0] not in PACKAGE_DIRS:
        trees += [(base / d, 0) for d in PACKAGE_DIRS if _plain_dir(base / d)]
    for tree, depth in trees:
        if error := _export_walk(base, tree, depth, plan, ceilings):
            return plan, error
    if not plan.files:
        return plan, "Экспорт: в задаче нечего копировать"
    return plan, None


def _export_walk(base: Path, source: Path, depth: int, plan: Export, ceilings: tuple[int, int]) -> str | None:
    """Обход дерева без ссылок; путь в приложении - путь от задачи без первых depth частей."""
    max_files, max_bytes = ceilings
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False, onerror=lambda e: None):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            rel = (here / d).relative_to(base).as_posix()
            if d in EXPORT_SKIP_DIRS or not _plain_dir(here / d):
                plan.skipped.append(f"{rel} ({'история git' if d in EXPORT_SKIP_DIRS else 'ссылка'})")
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f.startswith(TMP_PREFIX):
                continue
            rel = (here / f).relative_to(base).as_posix()
            try:
                st = os.lstat(here / f)
            except OSError:
                plan.skipped.append(f"{rel} (не прочитан)")
                continue
            if not is_regular(st):
                plan.skipped.append(f"{rel} (ссылка или спецфайл)")
                continue
            # исполняемость переносится (S_IXUSR -> 0755), setuid и setgid - нет
            mode = EXEC_FILE_MODE if st.st_mode & stat.S_IXUSR else JOB_FILE_MODE
            plan.files.append((rel, "/".join(rel.split("/")[depth:]), st.st_size, mode))
            plan.bytes += st.st_size
            if len(plan.files) > max_files:
                return f"Экспорт: больше {max_files} файлов ({EXPORT_MAX_FILES_ENV}) - возьмите from_path уже"
            if plan.bytes > max_bytes:
                return f"Экспорт: больше {max_bytes} байт ({EXPORT_MAX_BYTES_ENV}) - возьмите from_path уже"
    return None


def _export_copy(base: Path, plan: Export, dest: Path) -> str | None:
    """Файлы плана в dest; читается дескриптором без ссылок (read_regular)."""
    for rel, out, size, mode in plan.files:
        try:
            data = read_regular(base, rel, size)
            if data is None:
                plan.skipped.append(f"{rel} (сменился во время копии)")
                continue
            replace_job_bytes(dest, out, data, mode)
        except OSError as e:
            return f"Экспорт: {rel} не скопирован ({e.strerror or e}) - повторите"
    return None
