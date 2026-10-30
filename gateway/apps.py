"""Живые приложения SAM (SAM-182, эпик SAM-181, ADR-016, ADR-021): код SAM работает долго и отвечает странице.

Приложение - папка `SAM_APPS_DIR/<id>/`: манифест `app.json` и код в `src/`. Адрес приложения - `/apps/<slug>/`: slug
из имени, уникальный и неизменный (SAM-205); старый `/apps/<id>/` шлюз перенаправляет на него. В контейнер монтируется только `src/`:
манифест приложению недоступен, а из него шлюз берёт порт. Два долгоживущих контейнера:
- приложение: образ песочницы, те же ограничения (`sandbox.hardening`), `src/` в /work, своя сеть `--internal`
  с маленькой подсетью из `SAM_APPS_SUBNET_BASE` - выхода наружу и соседей в ней нет;
- пограничный: пересыльщик `apps_edge.py` во внутренней сети приложения и в общей сети пограничных, порт
  опубликован только на 127.0.0.1 хоста. Шлюз отдаёт страницу маршрутом `/apps/<slug>/...` с CSP sandbox.
Оба контейнера с `--restart unless-stopped`: переживают перезапуск Docker и ПК; при старте шлюза `revive`
поднимает те, что должны работать и не работают.

Решения (манифест, имена, команды docker, порт, подсеть, что поднимать) отделены от запуска процесса: docker
приходит параметром `run`, по умолчанию - `docker_run`.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from anyascii import anyascii

from . import sandbox
from .env import env_int

_REPO = Path(__file__).resolve().parent.parent
APPS_DIR = Path(os.getenv("SAM_APPS_DIR", str(_REPO / "data" / "apps")))
EDGE_SCRIPT = Path(__file__).resolve().parent / "apps_edge.py"
MANIFEST = "app.json"
SRC = "src"                         # код приложения; монтируется в /work, манифест лежит уровнем выше
SRC_NEW, SRC_OLD = "src.new", "src.old"     # сборка из задачи песочницы и прежний код на время обмена (rebuild)
SRC_PREV = "src.prev"                # прежняя версия после удачного обмена: вернуть руками, если правка испортила
# Данные приложения (SAM-181): своя папка рядом с кодом, монтируется в контейнер отдельно; обмен кода (rebuild,
# app_update) и пересоздание контейнера её не трогают, уходит она только с удалением приложения
DATA = "data"
DATA_IN = "/data"   # путь - договорённость (скилл make-app): переменных окружения у контейнера приложения нет
PREFIX = "sam-app-"
EDGE_NETWORK = "sam-apps-edge"      # общая сеть пограничных: друг друга им видеть не страшно, они только пересылают
EDGE_IN = "/edge/edge.py"
EDGE_PORT_IN = 8080                 # порт пересыльщика внутри его контейнера
APP_ALIAS = "app"                   # имя приложения во внутренней сети, на него ходит пересыльщик
DEFAULT_PORT_IN = 8000              # порт, который слушает приложение, если не сказано другое
# Порты хоста под приложения: 127.0.0.1, вне портов стенда (8000, 3000, 8010, 1020x); 200 приложений с запасом
PORT_BASE_ENV, DEFAULT_PORT_BASE = "SAM_APPS_PORT_BASE", 18400
PORT_SPAN_ENV, DEFAULT_PORT_SPAN = "SAM_APPS_PORT_SPAN", 200
# Подсеть /28 на приложение по номеру порта: пул Docker по умолчанию кончается на ~30 сетях (замер ревью 14 окт)
SUBNET_BASE_ENV, DEFAULT_SUBNET_BASE = "SAM_APPS_SUBNET_BASE", "10.231.0.0/16"
APP_SUBNET_PREFIX = 28
DOCKER_TIMEOUT_S = 60
RUNNING, STOPPED = "running", "stopped"
TMP_SUFFIX = ".tmp"                 # манифест пишется рядом и встаёт на место одним os.replace
# Windows не даёт заменить файл, пока его читают, и прочитать, пока его заменяют (PermissionError): чтение и замена
# манифеста из соседних потоков (create под замком, start и scan вне его) повторяются; замена - доли миллисекунды
SHARING_RETRIES, SHARING_RETRY_S = 20, 0.01
# Выбор порта и slug и запись манифеста - под замком: два app_make в одном шлюзе одновременно (воркеры) иначе
# видят один и тот же список и берут одинаковые порт и slug
_CREATE_LOCK = threading.Lock()
# Действия над одним приложением по очереди (SAM-204): страница шлюза зовёт их в потоках, двойной клик или stop во
# время revive вели два docker run с одними именами контейнеров. Повторно входимый: start зовётся из rebuild под ним
_APP_LOCKS: dict[str, threading.RLock] = {}
_APP_LOCKS_GUARD = threading.Lock()


def app_lock(app_id: str) -> threading.RLock:
    with _APP_LOCKS_GUARD:
        return _APP_LOCKS.setdefault(app_id, threading.RLock())
# Адрес приложения (SAM-205): slug - латиница, цифры и дефис из имени; кириллица - транслитом anyascii
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_JUNK = re.compile(r"[^a-z0-9]+")
SLUG_MAX = 40                       # длина основы без суффикса: читается в адресной строке целиком
SLUG_FALLBACK = "app"               # имя без букв и цифр («!!!», эмодзи)
MONITOR_SLUG, RADAR_SLUG = "monitor", "radar"     # имена страниц стенда: приложению не выдаются
APPS_SLUG = "apps"                  # страница приложений (SAM-204): встроенное приложение шлюза
RESERVED_SLUGS = frozenset({APPS_SLUG, MONITOR_SLUG, RADAR_SLUG})
# Лого приложения (SAM-204): SVG строкой в манифесте, его рисует SAM. Лого - несколько КБ; манифест читается на каждом
# списке, поэтому длиннее - отказ
ICON_MAX_CHARS = 20_000
ICON_START = "<svg"
GATEWAY_ENV, DEFAULT_GATEWAY = "SAM_SERVER", "http://localhost:8000"     # шлюз, который отдаёт страницу


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    command: str
    host_port: int
    port: int = DEFAULT_PORT_IN
    description: str = ""
    created: str = ""
    by: str = ""                    # кто сделал: основная модель, агент
    request: str = ""               # по какой просьбе
    wanted: str = RUNNING           # что стенд поддерживает: running или stopped
    slug: str = ""                  # адрес /apps/<slug>/; пустой у старых - выставляется при чтении
    pinned: bool = False            # закреплено: верхний ряд плиток и навигация каркаса
    settings: list = field(default_factory=list)    # объявленные настройки (gateway/app_settings.py)
    shell: bool | dict = True       # каркас страницы: true - весь, false - без него, {bar, panelw} - свои опции
    tools: list = field(default_factory=list)       # ручки для SAM (gateway/app_tools.py), вызов - app_call
    icon: str = ""                  # лого плитки: SVG строкой; пусто - миниатюра страницы


@dataclass(frozen=True)
class DockerResult:
    code: int
    out: str
    err: str


Runner = Callable[[list[str]], DockerResult]


class ManifestError(ValueError):
    """Манифест не прочитан или не годится: текст - одна строка для лога."""


def docker_run(argv: list[str]) -> DockerResult:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return DockerResult(-1, "", str(e))
    return DockerResult(res.returncode, res.stdout, res.stderr)


# ─── имена, папки, порты ───────────────────────────────────────────────────────

def app_container(app_id: str) -> str:
    return f"{PREFIX}{app_id}"


def edge_container(app_id: str) -> str:
    return f"{PREFIX}edge-{app_id}"


def app_network(app_id: str) -> str:
    return f"{PREFIX}net-{app_id}"


def app_dir(root: Path, app_id: str) -> Path | None:
    return Path(root) / app_id if sandbox.JOB_ID.match(app_id or "") else None


def ports() -> range:
    base = env_int(PORT_BASE_ENV, DEFAULT_PORT_BASE)
    return range(base, base + env_int(PORT_SPAN_ENV, DEFAULT_PORT_SPAN))


def free_port(taken: set[int]) -> int | None:
    return next((p for p in ports() if p not in taken), None)


def app_subnet(host_port: int) -> str:
    """Подсеть /28 приложения по номеру его порта в диапазоне: своя у каждого, без пересечений."""
    base = ipaddress.ip_network(os.getenv(SUBNET_BASE_ENV, DEFAULT_SUBNET_BASE))
    step = 2 ** (APP_SUBNET_PREFIX - base.prefixlen)
    if host_port - ports().start >= step:
        raise ManifestError(f"порт {host_port}: в {base} нет подсети /{APP_SUBNET_PREFIX} под него")
    offset = (host_port - ports().start) * 2 ** (32 - APP_SUBNET_PREFIX)
    return str(ipaddress.ip_network((int(base.network_address) + offset, APP_SUBNET_PREFIX)))


def slugify(name: str) -> str:
    """Основа slug из имени: транслит в латиницу, нижний регистр, всё прочее - дефис; пусто - SLUG_FALLBACK."""
    slug = SLUG_JUNK.sub("-", anyascii(name or "").lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-") or SLUG_FALLBACK


def unique_slug(base: str, taken: set[str]) -> str:
    """base, если свободна и не зарезервирована; иначе base-2, base-3, ..."""
    slug, n = base, 1
    while slug in taken or slug in RESERVED_SLUGS:
        n += 1
        slug = f"{base}-{n}"
    return slug


def page_path(m: Manifest) -> str:
    return f"/apps/{m.slug}/"


def view_path(m: Manifest) -> str:
    """Приложение в каркасе SAM (полоса, панели), страница - во фрейме по page_path."""
    return f"/apps/{m.slug}"


def page_url(m: Manifest) -> str:
    """Полный адрес приложения в каркасе через шлюз (SAM_SERVER): его можно открыть в браузере этой машины."""
    return (os.getenv(GATEWAY_ENV) or DEFAULT_GATEWAY).rstrip("/") + view_path(m)


# ─── манифест ──────────────────────────────────────────────────────────────────

def load(root: Path, app_id: str) -> Manifest | None:
    """Манифест приложения по id; без slug - сначала выдача slug всем таким (`scan`). None и ManifestError - как
    у `_read`."""
    m = _read(root, app_id)
    if m is not None and not m.slug:
        scan(root)
        m = _read(root, app_id)
    return m


def find(root: Path, ref: str) -> Manifest | None:
    """Приложение по id или slug; нет - None; битый манифест по id - ManifestError."""
    ref = (ref or "").strip()
    m = load(root, ref)
    return m or next((a for a in all_apps(root) if a.slug == ref), None)


def _read(root: Path, app_id: str) -> Manifest | None:
    """Манифест как лежит; нет папки или файла - None; битый, чужой id, порт вне диапазона или негодный slug -
    ManifestError."""
    base = app_dir(root, app_id)
    path = base / MANIFEST if base else None
    if path is None or not path.is_file():
        return None
    try:
        m = Manifest(**json.loads(_shared(lambda: path.read_text(encoding="utf-8"))))
    except (ValueError, TypeError) as e:
        raise ManifestError(f"{app_id}: манифест не прочитан: {type(e).__name__}") from e
    if m.id != app_id or m.host_port not in ports():
        raise ManifestError(f"{app_id}: в манифесте чужой id или порт вне {PORT_BASE_ENV}")
    if m.slug and (not SLUG_RE.match(m.slug) or m.slug in RESERVED_SLUGS):
        raise ManifestError(f"{app_id}: slug {m.slug!r} не годится: латиница, цифры, дефис, не "
                            f"{', '.join(sorted(RESERVED_SLUGS))}")
    return m


def icon_error(icon: object) -> str | None:
    """Лого плитки годится - None: строка с разметкой SVG не длиннее ICON_MAX_CHARS; пустая снимает лого."""
    if not isinstance(icon, str):
        return f"icon - строка с SVG: {ICON_START} ...</svg>"
    if icon and not icon.lstrip().lower().startswith(ICON_START):
        return f"icon - разметка SVG, начинается с {ICON_START}"
    if len(icon) > ICON_MAX_CHARS:
        return f"icon длиннее {ICON_MAX_CHARS} знаков: упрости лого"
    return None


def save(root: Path, m: Manifest) -> None:
    base = app_dir(root, m.id)
    if base is None:
        raise ManifestError(f"{m.id!r}: id не из приложений")
    write_atomic(base / MANIFEST, json.dumps(asdict(m), ensure_ascii=False, indent=2))


def _shared(op: Callable[[], object]):
    """op с повтором при PermissionError: файл в этот миг читают или заменяют в соседнем потоке (Windows)."""
    for _ in range(SHARING_RETRIES - 1):
        try:
            return op()
        except PermissionError:
            time.sleep(SHARING_RETRY_S)
    return op()


def write_atomic(path: Path, text: str) -> None:
    """Файл целиком или прежний: оборванная запись не оставляет полманифеста, который потом не читается."""
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8")
    try:
        _shared(lambda: os.replace(tmp, path))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def scan(root: Path) -> tuple[list[Manifest], list[str]]:
    """Все приложения и строки о битых манифестах: один битый не мешает остальным. Старым без slug slug выдаётся и
    записывается в манифест - миграция без ручного шага."""
    root = Path(root)
    found, errors = [], []
    for p in sorted(root.iterdir()) if root.is_dir() else ():
        try:
            m = _read(root, p.name) if p.is_dir() else None
        except ManifestError as e:
            errors.append(str(e))
            continue
        if m:
            found.append(m)
    return _with_slugs(root, found), errors


def _with_slugs(root: Path, found: list[Manifest]) -> list[Manifest]:
    """Slug каждому, у кого его нет, в порядке создания: старшее приложение получает имя без суффикса."""
    taken = {m.slug for m in found if m.slug} | {m.id for m in found}
    fresh = {}
    for m in sorted((m for m in found if not m.slug), key=lambda m: (m.created, m.id)):
        fresh[m.id] = replace(m, slug=unique_slug(slugify(m.name), taken))
        taken.add(fresh[m.id].slug)
        save(root, fresh[m.id])
    return [fresh.get(m.id, m) for m in found]


def all_apps(root: Path) -> list[Manifest]:
    return scan(root)[0]


# ─── команды docker ────────────────────────────────────────────────────────────

def network_argv(name: str, internal: bool, subnet: str | None = None, docker: str = sandbox.DOCKER) -> list[str]:
    return [docker, "network", "create", *(["--internal"] if internal else []),
            *(["--subnet", subnet] if subnet else []), name]


def app_argv(m: Manifest, base: Path, limits: sandbox.Limits, docker: str = sandbox.DOCKER) -> list[str]:
    return [
        docker, "run", "-d", "--name", app_container(m.id), "--restart", "unless-stopped",
        "--network", app_network(m.id), "--network-alias", APP_ALIAS, *sandbox.hardening(limits),
        "-v", f"{Path(base) / SRC}:{sandbox.WORKDIR_IN}", "-w", sandbox.WORKDIR_IN,
        "-v", f"{Path(base) / DATA}:{DATA_IN}",
        limits.image, "sh", "-c", m.command,
    ]


def edge_argv(m: Manifest, limits: sandbox.Limits, docker: str = sandbox.DOCKER) -> list[str]:
    return [
        docker, "run", "-d", "--name", edge_container(m.id), "--restart", "unless-stopped",
        "--network", EDGE_NETWORK, "-p", f"127.0.0.1:{m.host_port}:{EDGE_PORT_IN}",
        *sandbox.hardening(limits), "-v", f"{EDGE_SCRIPT}:{EDGE_IN}:ro",
        limits.image, "python", EDGE_IN, APP_ALIAS, str(m.port), str(EDGE_PORT_IN),
    ]


# ─── жизненный цикл ────────────────────────────────────────────────────────────

def is_running(m: Manifest, run: Runner = docker_run) -> bool:
    for name in (app_container(m.id), edge_container(m.id)):
        res = run([sandbox.DOCKER, "inspect", "-f", "{{.State.Running}}", name])
        if res.code != 0 or res.out.strip() != "true":
            return False
    return True


def _remove(m: Manifest, run: Runner) -> None:
    run([sandbox.DOCKER, "rm", "-f", app_container(m.id), edge_container(m.id)])


def _teardown(m: Manifest, run: Runner) -> None:
    """Контейнеры и своя сеть приложения; общая сеть пограничных остаётся."""
    _remove(m, run)
    run([sandbox.DOCKER, "network", "rm", app_network(m.id)])


def subnet_holders(subnet: str, run: Runner = docker_run) -> list[str]:
    """id приложений, чьи сети занимают эту подсеть (по префиксу имени сети приложения)."""
    listed = run([sandbox.DOCKER, "network", "ls", "--filter", f"name={app_network('')}", "--format", "{{.Name}}"])
    holders = []
    for name in (listed.out.split() if listed.code == 0 else []):
        got = run([sandbox.DOCKER, "network", "inspect", name, "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}"])
        if got.code == 0 and got.out.strip() == subnet:
            holders.append(name[len(app_network('')):])
    return holders


def free_subnet(root: Path, m: Manifest, subnet: str, run: Runner = docker_run) -> str | None:
    """Подсеть приложения занята сетью другого id. Без манифеста в этом корне - сирота (тест или сбой оставили
    контейнеры): убрать её контейнеры и сеть. С манифестом - строка ошибки: два приложения на одном порту."""
    for other in subnet_holders(subnet, run):
        if other == m.id:
            continue
        base = app_dir(root, other)
        if base is not None and (base / MANIFEST).exists():
            return f"Приложение {m.id}: подсеть {subnet} занята приложением {other}"
        for argv in ([sandbox.DOCKER, "rm", "-f", app_container(other), edge_container(other)],
                     [sandbox.DOCKER, "network", "rm", app_network(other)]):
            run(argv)
        print(f"[Apps] сирота {other} без манифеста занимала {subnet} - убрана")
    return None


def start(root: Path, m: Manifest, limits: sandbox.Limits | None = None, run: Runner = docker_run) -> str | None:
    """Поднять приложение заново: старое убрать, сети завести, приложение и пограничный запустить.
    Ошибка - одной строкой, после неё ни контейнеров, ни своей сети не остаётся; None - запущено."""
    with app_lock(m.id):
        return _start(root, m, limits, run)


def _start(root: Path, m: Manifest, limits: sandbox.Limits | None, run: Runner) -> str | None:
    limits = limits or sandbox.limits_from_env()
    base = app_dir(root, m.id)
    (base / DATA).mkdir(parents=True, exist_ok=True)   # папка данных до монтирования: иначе docker создаст её от root
    _teardown(m, run)
    if run([sandbox.DOCKER, "network", "inspect", EDGE_NETWORK]).code != 0:
        res = run(network_argv(EDGE_NETWORK, internal=False))
        if res.code != 0:
            return f"Сеть {EDGE_NETWORK} не создана: {_line(res.err)}"
    try:
        subnet = app_subnet(m.host_port)
    except ManifestError as e:
        return str(e)
    if error := free_subnet(root, m, subnet, run):
        return error
    for argv in (network_argv(app_network(m.id), internal=True, subnet=subnet),
                 app_argv(m, base, limits), edge_argv(m, limits),
                 [sandbox.DOCKER, "network", "connect", app_network(m.id), edge_container(m.id)]):
        res = run(argv)
        if res.code != 0:
            _teardown(m, run)
            return f"Приложение {m.id} не запущено: {_line(res.err)}"
    save(root, replace(m, wanted=RUNNING))
    return None


def stop(root: Path, m: Manifest, run: Runner = docker_run) -> None:
    with app_lock(m.id):
        _teardown(m, run)
        save(root, replace(m, wanted=STOPPED))


MISSING_MARKERS = ("no such container", "no such network", "not found")   # уже нет - это и нужно удалению


def delete(root: Path, m: Manifest, run: Runner = docker_run) -> str | None:
    """Убрать контейнеры и сеть, затем папку целиком: код, данные, манифест. Docker не убрал - папка остаётся
    (иначе контейнеры с политикой рестарта держат порт без манифеста); ошибка - одной строкой, None - удалено."""
    with app_lock(m.id):
        return _delete(root, m, run)


def _delete(root: Path, m: Manifest, run: Runner) -> str | None:
    for argv in ([sandbox.DOCKER, "rm", "-f", app_container(m.id)], [sandbox.DOCKER, "rm", "-f", edge_container(m.id)],
                 [sandbox.DOCKER, "network", "rm", app_network(m.id)]):   # по одному: общий stderr прятал ошибку
        res = run(argv)
        if res.code != 0 and not any(s in res.err.lower() for s in MISSING_MARKERS):
            return f"{m.name}: docker не убрал приложение, папка оставлена: {_line(res.err)}"
    base = app_dir(root, m.id)
    try:   # манифест последним: оборвалось посреди - приложение ещё видно и удаляется повторно
        if (base / SRC).exists():
            shutil.rmtree(base / SRC)
        shutil.rmtree(base)
    except OSError as e:
        return f"{m.name}: контейнеры убраны, папка не удалена: {e}"
    return None


def create(root: Path, name: str, command: str, files: dict, port: int = DEFAULT_PORT_IN, description: str = "",
           by: str = "", request: str = "", limits: sandbox.Limits | None = None, run: Runner = docker_run,
           new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12], from_job: str = "", from_path: str = "",
           sandbox_root: Path | None = None,
           owner: sandbox.Owner | None = None, extras: dict | None = None) -> tuple[Manifest | None, str | None]:
    """Новое приложение из задачи песочницы (from_job, from_path) и/или файлов: папка, копия задачи, files поверх,
    манифест, запуск. Отказ копии или файлов - папки не остаётся. (манифест, ошибка одной строкой)."""
    root = Path(root)
    with _CREATE_LOCK:      # до записи манифеста: после неё порт и slug видны следующему create
        existing = all_apps(root)
        host_port = free_port({m.host_port for m in existing})
        if host_port is None:
            return None, ("Свободных портов под приложения нет: остановите и удалите лишние или расширьте "
                          "SAM_APPS_PORT_SPAN")
        slug = unique_slug(slugify(name), {m.slug for m in existing} | {m.id for m in existing})
        m = Manifest(id=new_id(), name=name, command=command, host_port=host_port, port=port,
                     description=description, created=time.strftime("%Y-%m-%d %H:%M"), by=by, request=request,
                     slug=slug, **(extras or {}))
        src = root / m.id / SRC
        src.mkdir(parents=True, exist_ok=True)
        if error := _fill(src, m.id, files, from_job, from_path, sandbox_root, owner):
            shutil.rmtree(root / m.id, ignore_errors=True)
            return None, error
        save(root, m)
    return m, start(root, m, limits, run)


def _fill(src: Path, app_id: str, files: dict | None, from_job: str, from_path: str,
          sandbox_root: Path | None, owner: sandbox.Owner | None = None) -> str | None:
    """Копия задачи песочницы, затем files поверх неё: правка модели перекрывает скопированное."""
    if from_job:
        _, error = sandbox.export_job(from_job, from_path, src, app_id, sandbox_root, owner)
        if error:
            return error
    return sandbox.write_files(src, files or {})


def update(root: Path, m: Manifest, files: dict | None = None, command: str = "", limits: sandbox.Limits | None = None,
           run: Runner = docker_run, from_job: str = "", from_path: str = "",
           sandbox_root: Path | None = None,
           owner: sandbox.Owner | None = None, clean: bool = False) -> tuple[Manifest, str | None]:
    """Правка по ходу разговора: файлы дописываются или заменяются в `src/`, команда - если дана, приложение
    перезапускается. С from_job - сборка из задачи песочницы поверх текущего кода, clean - только из задачи (rebuild).
    (манифест, ошибка одной строкой)."""
    if from_job:
        return rebuild(root, m, files, command, limits, run, from_job, from_path, sandbox_root, owner, clean)
    if error := sandbox.write_files(Path(root) / m.id / SRC, files or {}):
        return m, error
    m = replace(m, command=command or m.command)
    save(root, m)
    return m, start(root, m, limits, run)


def rebuild(root: Path, m: Manifest, files: dict | None, command: str, limits: sandbox.Limits | None, run: Runner,
            from_job: str, from_path: str, sandbox_root: Path | None,
            owner: sandbox.Owner | None = None, clean: bool = False) -> tuple[Manifest, str | None]:
    """Новый `src/` из задачи песочницы. По умолчанию задача ложится поверх текущего кода: файлы, которых в задаче нет,
    остаются (15 окт задача с одними фото стёрла index.html приложения); clean - `src/` только из задачи, без остатков
    прежнего. Сборка - в соседней `src.new` (вне монтирования): сбой до обмена её удаляет, работающее приложение и его
    `src/` не трогаются. Обмен - при остановленных контейнерах: `src` -> `src.old`, `src.new` -> `src`, запуск; не
    запустилось - прежний `src` возвращается и поднимается (откат); запустилось - прежний становится `src.prev`."""
    base = Path(root) / m.id
    new, old, src = base / SRC_NEW, base / SRC_OLD, base / SRC
    shutil.rmtree(new, ignore_errors=True)          # остаток прошлой оборванной сборки
    if clean or not src.is_dir():
        new.mkdir()
    else:
        shutil.copytree(src, new, symlinks=True)    # ссылки как есть: копия не идёт по ним за пределы приложения
    if error := _fill(new, m.id, files, from_job, from_path, sandbox_root, owner):
        shutil.rmtree(new, ignore_errors=True)
        return m, error
    _teardown(m, run)
    try:
        shutil.rmtree(old, ignore_errors=True)
        os.replace(src, old)
        os.replace(new, src)
    except OSError as e:
        if not src.exists() and old.exists():
            os.replace(old, src)
        shutil.rmtree(new, ignore_errors=True)
        restarted = start(root, m, limits, run)
        return m, f"{m.name}: новая сборка не встала на место ({e.strerror or e}), работает прежняя" + (
            f"; прежняя тоже не поднялась: {restarted}" if restarted else "")
    fresh = replace(m, command=command or m.command)
    if error := start(root, fresh, limits, run):
        shutil.rmtree(src, ignore_errors=True)
        os.replace(old, src)
        restarted = start(root, m, limits, run)
        return m, f"{error}; вернула прежнюю версию" + (f", но и она не поднялась: {restarted}" if restarted else "")
    prev = base / SRC_PREV
    shutil.rmtree(prev, ignore_errors=True)
    os.replace(old, prev)
    return fresh, None


def revive(root: Path, limits: sandbox.Limits | None = None, run: Runner = docker_run) -> list[str]:
    """После старта стенда: приложения, которые должны работать и не работают, поднять. Строки ошибок."""
    found, errors = scan(root)
    for m in found:
        with app_lock(m.id):
            fresh = find(root, m.id)   # под замком: приложение могли остановить, пока шлюз поднимался
            if fresh and fresh.wanted == RUNNING and not is_running(fresh, run):
                if error := start(root, fresh, limits, run):
                    errors.append(error)
    return errors


def upstream(root: Path, ref: str) -> Manifest | None:
    """Приложение (по id или slug) для прокси шлюза: только своё и запущенное; адрес - `local_base`."""
    try:
        m = find(root, ref)
    except ManifestError:
        return None
    return m if m and m.wanted == RUNNING else None


def local_base(m: Manifest) -> str:
    """Адрес пограничного контейнера: 127.0.0.1 и порт из диапазона (манифест с чужим портом не читается)."""
    return f"http://127.0.0.1:{m.host_port}"


def local_location(location: str | None, slug: str, base: str) -> str | None:
    """Редирект приложения под префикс шлюза: относительный - как есть, путь от корня или свой адрес на 127.0.0.1 -
    в `/apps/<slug>/...`; на чужой хост - отброшен, иначе страница уйдёт за пределы приложения."""
    if not location:
        return None
    location = location.replace("\\", "/")      # браузер читает «\» в http-адресе как «/»: «\\host» - чужой хост
    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        if f"{parts.scheme}://{parts.netloc}" != base:
            return None
    elif not location.startswith("/"):
        return location
    tail = parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")
    return f"/apps/{slug}/{tail}"


def _line(text: str) -> str:
    lines = [s for s in (text or "").strip().splitlines() if s.strip()]
    return lines[-1] if lines else "без вывода"
