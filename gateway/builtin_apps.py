"""Встроенные приложения SAM: приложения, монитор и радар в списке приложений экосистемы (SAM-199, SAM-204).

Встроенное приложение - процесс вне контейнерного рантайма `gateway/apps.py`: страница приложений - страница шлюза
`/apps` (`gateway/apps_api.py`), монитор - страница шлюза `/monitor`, радар - сервис `radar.presence_service`
(:8010, `start_radar.ps1`). Манифест лежит рядом с манифестами живых приложений, в `SAM_APPS_DIR/<slug>/app.json`,
slug из `apps.RESERVED_SLUGS`: контейнерному приложению он не выдаётся, а папка не из 12 hex не читается как
контейнерное приложение. Манифест поставляет SAM, не человек: `ensure` при старте шлюза пишет его, если файла нет
или он разошёлся с поставляемым.

Приложения и монитор поставлены всегда и не удаляются: это страницы самого шлюза, их health check - его. Радар -
опция: удаление (`remove`) снимает его манифест и кладёт в его папку метку `removed`; `ensure` его больше не пишет,
агрегатор MCP не поднимает сервер `radar` (`mcp/mcp_aggregator.py`, та же метка), данные радара `data/presence_*`
остаются. Вернуть - `install` (страница приложений: «поставить радар»). Выключенный радар (`gateway/radar_switch.py`)
не поставляется и не читается с диска: манифест, оставшийся с прошлого включения, не показывается.

Функции радара для SAM - методы его OpenAPI с меткой x-sam-tool (`radar/presence_sam_api.py`), как у живых
приложений: `app_list` читает их с адреса радара, `app_call(radar, where, {person})` вызывает
(`gateway/tools/apps.py`). В манифесте их нет: список живёт в самом сервисе.

Решения (манифесты, адрес под хост запроса, команда старта и остановки) отделены от сети и процессов: проверка
здоровья и запуск команды приходят параметрами.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from . import apps, radar_client, radar_switch, restart
from .core_paths import REPO

KIND = "builtin"                    # вид в манифесте: процесс снаружи контейнерного рантайма
MONITOR_PATH, APPS_PATH, HEALTH_PATH, RADAR_PAGE = "/monitor", "/apps", "/health", "/"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})
# Остановка радара: по умолчанию на Windows - start_radar.ps1 -Stop; на другой ОС - из переменной
STOP_ENV_PREFIX = "SAM_APPS_STOP_CMD_"
RADAR_STOP_SWITCH = "-Stop"
ACTION_LOG = REPO / "logs" / "builtin-apps.log"
START, STOP = "start", "stop"
SURFACE_COMPONENT = "mcp"           # агрегатор MCP: функции встроенных приложений живут в его серверах
REMOVED = "removed"                 # метка в папке встроенного приложения: человек его удалил, не ставить снова
# Без монитора SAM не видно, без страницы приложений их не открыть: не удаляются ни кнопкой, ни API
PERMANENT = frozenset({apps.APPS_SLUG, apps.MONITOR_SLUG})

Health = Callable[[str], bool]
Run = Callable[[str, Path], Awaitable[tuple[int | None, str]]]


@dataclass(frozen=True)
class Builtin:
    slug: str
    name: str
    description: str
    url: str                        # где приложение живёт на самом деле
    health: str                     # его health check
    control: str = ""               # имя компонента restart_component; пусто - отдельно не стартует и не стоит
    kind: str = KIND
    pinned: bool = True             # встроенные закреплены всегда: верхний ряд плиток, навигация
    removable: bool = True
    icon: str = ""                  # лого плитки: SVG строкой (apps.icon_error); пусто - знак страницы приложений


FIELDS = frozenset(f.name for f in fields(Builtin))


def visible(slug: str, environ: Mapping[str, str] = os.environ) -> bool:
    """Есть ли встроенное приложение на этой машине: всё, кроме выключенного радара."""
    return slug != apps.RADAR_SLUG or radar_switch.enabled(environ)


def slugs(environ: Mapping[str, str] = os.environ) -> list[str]:
    return [slug for slug in sorted(apps.RESERVED_SLUGS) if visible(slug, environ)]


def defaults(environ: Mapping[str, str] = os.environ) -> list[Builtin]:
    """Поставляемые манифесты: адреса - как у клиентов стенда (SAM_SERVER, SAM_RADAR_URL)."""
    gateway = (environ.get(apps.GATEWAY_ENV) or apps.DEFAULT_GATEWAY).rstrip("/")
    radar = (environ.get(radar_client.URL_ENV) or radar_client.DEFAULT_URL).rstrip("/")
    shipped = [
        Builtin(slug=apps.APPS_SLUG, name="Приложения",
                description="Плитки приложений SAM: открыть, приостановить, настроить, удалить; страница шлюза",
                url=gateway + APPS_PATH, health=gateway + HEALTH_PATH, removable=False),
        Builtin(slug=apps.MONITOR_SLUG, name="Монитор",
                description="Лента SAM: ходы, голос, агенты, приложения; страница шлюза",
                url=gateway + MONITOR_PATH, health=gateway + HEALTH_PATH, removable=False),
        Builtin(slug=apps.RADAR_SLUG, name="Радар",
                description="Где кто дома: комнаты, места, история, устройства; отдельный сервис",
                url=radar + RADAR_PAGE, health=radar + HEALTH_PATH, control=restart.RADAR),
    ]
    return [b for b in shipped if visible(b.slug, environ)]


def _path(root: Path, slug: str) -> Path:
    return Path(root) / slug / apps.MANIFEST


def ensure(root: Path, shipped: list[Builtin] | None = None) -> list[str]:
    """Записать поставляемые манифесты, которых нет или которые разошлись с поставляемыми. Строки ошибок."""
    errors = []
    for b in shipped if shipped is not None else defaults():
        if is_removed(root, b.slug):
            continue
        path, text = _path(root, b.slug), json.dumps(asdict(b), ensure_ascii=False, indent=2)
        try:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                apps.write_atomic(path, text)
        except OSError as e:
            errors.append(f"{b.slug}: манифест встроенного приложения не записан: {e}")
    return errors


def scan(root: Path) -> tuple[list[Builtin], list[str]]:
    """Встроенные приложения с диска и строки о битых манифестах."""
    found, errors = [], []
    for slug in slugs():
        path = _path(root, slug)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # поле прошлой версии манифеста не мешает прочесть его до перезаписи в ensure
            b = Builtin(**{k: v for k, v in data.items() if k in FIELDS})
        except (ValueError, TypeError, AttributeError) as e:
            errors.append(f"{slug}: манифест встроенного приложения не прочитан: {type(e).__name__}")
            continue
        if b.slug != slug or b.kind != KIND:
            errors.append(f"{slug}: в манифесте встроенного приложения чужой slug или вид")
            continue
        found.append(b)
    return found, errors


def is_removed(root: Path, slug: str) -> bool:
    return slug not in PERMANENT and (Path(root) / slug / REMOVED).is_file()


def removed(root: Path) -> list[str]:
    """Встроенные приложения, которые человек удалил: их можно поставить снова."""
    return [slug for slug in slugs() if is_removed(root, slug)]


def remove(root: Path, slug: str) -> str | None:
    """Снять встроенное приложение: метка, затем манифест; ошибка одной строкой, None - снято."""
    if slug in PERMANENT:
        return f"{slug} не удаляется: это страница самого шлюза"
    folder = Path(root) / slug
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / REMOVED).write_text("", encoding="utf-8")
        (folder / apps.MANIFEST).unlink(missing_ok=True)
    except OSError as e:
        return f"{slug}: не снято: {e}"
    return None


def install(root: Path, slug: str, shipped: list[Builtin] | None = None) -> str | None:
    """Поставить снятое встроенное приложение снова."""
    if slug not in slugs():
        return f"встроенного приложения {slug} нет: есть {', '.join(slugs())}"
    (Path(root) / slug / REMOVED).unlink(missing_ok=True)
    errors = ensure(root, [b for b in (shipped if shipped is not None else defaults()) if b.slug == slug])
    return errors[0] if errors else None


def find(root: Path, slug: str) -> Builtin | None:
    return next((b for b in scan(root)[0] if b.slug == (slug or "").strip()), None)


def for_host(url: str, host: str | None) -> str:
    """Адрес на этой машине (127.0.0.1, localhost) - под хост, с которого пришёл запрос: с другого устройства
    браузер должен идти на эту машину, а не к себе."""
    parts = urlsplit(url)
    if not host or parts.hostname not in LOOPBACK:
        return url
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit(parts._replace(netloc=netloc))


def is_proxied(b: Builtin) -> bool:
    """Отдельный процесс (есть компонент перезапуска) - шлюз отдаёт его по /apps/<slug>/ (gateway/builtin_proxy.py);
    монитор - страница самого шлюза."""
    return bool(b.control)


def page_path(b: Builtin) -> str:
    """Адрес страницы на шлюзе: у всех встроенных один порт и slug в пути, как у живых приложений."""
    return f"/apps/{b.slug}/" if is_proxied(b) else (urlsplit(b.url).path or "/")


def is_up(b: Builtin, health: Health | None = None) -> bool:
    return (health or default_health)(b.health)


def default_health(url: str) -> bool:
    """Ответ /health со status ok - те же проверки, что у надзора стенда."""
    from . import stand_checks       # тянет слой голоса: только когда проверка и правда нужна
    return stand_checks.health_state(stand_checks.fetch_health(url)) == stand_checks.UP


def command(b: Builtin, action: str, environ: Mapping[str, str] = os.environ,
            windows: bool = sys.platform == "win32") -> tuple[str, str | None]:
    """(команда, отказ одной строкой). Старт - перезапуск компонента (restart.plan); остановка -
    SAM_APPS_STOP_CMD_<ИМЯ>, на Windows для радара - start_radar.ps1 -Stop."""
    if not b.control:
        return "", (f"{b.name} - часть шлюза: отдельно не запускается и не останавливается; перезапуск шлюза - "
                    f"restart_component {restart.GATEWAY}")
    if action == START:
        p = restart.plan(b.control, environ, windows)
        return (p.command, None) if p.kind == restart.COMMAND else ("", p.reason or f"{b.name}: запуск не настроен")
    stop_env = f"{STOP_ENV_PREFIX}{b.control.upper()}"
    stop = (environ.get(stop_env) or "").strip()
    if not stop and windows and b.control == restart.RADAR:
        stop = f'{restart.POWERSHELL} "{restart.RADAR_SCRIPT}" {RADAR_STOP_SWITCH}'
    return (stop, None) if stop else ("", f"{b.name}: остановка здесь не настроена: {stop_env}")


async def restart_surface(run: Run | None = None, environ: Mapping[str, str] = os.environ,
                          windows: bool = sys.platform == "win32") -> str | None:
    """Перезапустить агрегатор MCP: он заново читает метки снятых приложений и поднимает или не поднимает их
    серверы (переходник радара - radar_*). Перезапуск не настроен - строка, что сделать."""
    p = restart.plan(SURFACE_COMPONENT, environ, windows)
    if p.kind != restart.COMMAND:
        return f"инструменты radar_* у моделей вне шлюза обновятся после перезапуска MCP: {p.reason}"
    code, last = await (run or restart.run_command)(p.command, ACTION_LOG)
    return None if code == 0 else f"MCP не перезапущен ({code}): {last or 'без вывода'}; restart_component mcp"


async def control(b: Builtin, action: str, run: Run | None = None, environ: Mapping[str, str] = os.environ,
                  windows: bool = sys.platform == "win32") -> str | None:
    """Запустить или остановить встроенное приложение его командой; ошибка - одной строкой, None - готово."""
    cmd, refused = command(b, action, environ, windows)
    if refused:
        return refused
    code, last = await (run or restart.run_command)(cmd, ACTION_LOG)
    if code is None:
        return f"{b.name}: команда не кончилась за срок, проверь его страницу позже"
    return None if code == 0 else f"{b.name}: команда вернула {code}: {last or 'без вывода'}"
