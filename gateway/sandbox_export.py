"""Выгрузка из задачи песочницы в папку экспорта на ПК (SAM-83, ADR-016).

Параметр `export` у sandbox_run: [{"from": "<путь в задаче>", "to": "<имя в папке экспорта>"}], выполняется после
команды. Папка экспорта - SAM_SANDBOX_EXPORT_DIR; пусто - экспорт выключен. Её нельзя задать корнем диска, домашней
папкой или папкой над ней, папкой задач песочницы, данными или репозиторием SAM - те же проверки, что у корней
импорта (`sandbox_import.unsafe_root`). Копирует шлюз байтами под замком задачи, без ссылок, `.git`, пакетов
(`.pkgs`, `node_modules`) и секретов по маскам импорта. Перезаписи нет: имя в папке экспорта занято - отказ, а не
суффикс: модель называет имя сама, тихое `report (2).html` она бы не нашла, а отказ просит другое имя явно.
Модели путь называется как `export:<путь от папки экспорта>`, без абсолютных путей.

Здесь - настройка, разбор и проверка места; обход задачи и запись - в sandbox.py (безопасное чтение и замок).
"""
from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import sandbox_import

IS_WINDOWS = os.name == "nt"
# Файлы, которые Проводник разбирает сам при показе папки (ярлыки, ссылки, иконки, автозапуск): такой файл из
# песочницы действует уже тем, что лежит в папке, - не выгружается
SHELL_PARSED_SUFFIXES = (".lnk", ".url", ".scf", ".library-ms", ".searchconnector-ms")
SHELL_PARSED_NAMES = frozenset({"desktop.ini", "autorun.inf"})
# Исполнимое и документы с макросами - выгружается, но называется отдельной строкой в итоге
RISKY_SUFFIXES = (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".hta", ".msi", ".scr", ".com", ".docm", ".xlsm")
# Имена устройств Windows: запись в «NUL.txt» или «com1» уходит в устройство, а не в файл
_WIN_DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)
# Mark-of-the-Web: файл «из интернета» - Windows предупреждает перед запуском и открывает Office в защищённом виде
MOTW_STREAM, MOTW_TEXT = ":Zone.Identifier", "[ZoneTransfer]\r\nZoneId=3\r\n"
DIR_ENV = "SAM_SANDBOX_EXPORT_DIR"
MAX_FILES_ENV, DEFAULT_MAX_FILES = "SAM_SANDBOX_EXPORT_MAX_FILES", 2000
MAX_BYTES_ENV, DEFAULT_MAX_BYTES = "SAM_SANDBOX_EXPORT_MAX_BYTES", 100_000_000    # отчёты, графики, сборки страниц
MAX_ITEMS = 10                          # пунктов export за один запуск
SKIP_DIRS = frozenset({".git", ".pkgs", "node_modules"})    # история и пакеты на ПК не нужны, их и так ставит песочница
PREFIX = "export:"                      # как путь в папке экспорта называется модели

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    source: str             # путь в задаче (posix)
    to: str                 # путь в папке экспорта (posix)


def export_dir(protected: tuple[Path, ...]) -> tuple[Path | None, str | None]:
    """Папка экспорта из env: выключено, запрещённая папка или её нет - отказ одной строкой (причина - и в лог)."""
    raw = os.getenv(DIR_ENV, "").strip()
    if not raw:
        return None, f"Выгрузка на ПК выключена: задайте папку экспорта в {DIR_ENV}"
    folder = Path(raw).expanduser().resolve()   # junction или симлинк в самой настройке раскрывается до цели
    if why := sandbox_import.unsafe_root(folder, [p.resolve() for p in protected]) or _autorun_folder(folder):
        log.warning("%s: папка %s не используется - %s", DIR_ENV, folder, why)
        return None, f"Папку экспорта {DIR_ENV} нельзя использовать ({why}) - задайте отдельную папку"
    if not plain(folder, stat.S_IFDIR):
        return None, f"Папки экспорта {DIR_ENV} нет на ПК - создайте её"
    return folder, None


def autorun_folders() -> list[Path]:
    """Папки, файл в которых запускается сам или по имени команды: автозагрузка (своя и общая), PATH процесса
    (на Windows в нём пути пользователя и системы), WindowsApps."""
    found = []
    for env, *tail in (("APPDATA", "Microsoft", "Windows", "Start Menu", "Programs", "Startup"),
                       ("PROGRAMDATA", "Microsoft", "Windows", "Start Menu", "Programs", "StartUp"),
                       ("LOCALAPPDATA", "Microsoft", "WindowsApps")):
        if os.getenv(env):
            found.append(Path(os.environ[env]).joinpath(*tail))
    found += [Path(p) for p in os.getenv("PATH", "").split(os.pathsep) if p.strip() and Path(p).is_absolute()]
    return found


def _autorun_folder(folder: Path) -> str | None:
    for p in autorun_folders():
        try:
            p = p.resolve()
        except OSError:
            continue
        if folder == p or folder.is_relative_to(p) or p.is_relative_to(folder):
            return f"пересекается с папкой автозапуска или PATH {p}"
    return None


def norm_part(part: str) -> str:
    """Имя так, как его видит файловая система: на Windows без регистра и без точек и пробелов в конце."""
    return part.lower().rstrip(". ") if IS_WINDOWS else part


def device_name(name: str) -> bool:
    return IS_WINDOWS and bool(_WIN_DEVICE.fullmatch(norm_part(name)))


def shell_parsed(name: str) -> bool:
    low = name.lower().rstrip(". ")
    return low in SHELL_PARSED_NAMES or low.endswith(SHELL_PARSED_SUFFIXES)


def risky(name: str) -> bool:
    return name.lower().rstrip(". ").endswith(RISKY_SUFFIXES)


def skip_reason(name: str, is_dir: bool, patterns: tuple[str, ...]) -> str | None:
    """Почему имя из задачи не выгружается; None - выгружается."""
    if is_dir and name in SKIP_DIRS:
        return "история git или пакеты"
    if sandbox_import.skipped_name(name, patterns):
        return "секрет по маске"
    if device_name(name):
        return "имя устройства Windows"
    if not is_dir and shell_parsed(name):
        return "файл, который разбирает Проводник"
    return None


def clash(targets: list[str]) -> str | None:
    """Места назначения всей выгрузки не накладываются: одно имя дважды (на Windows и `A.txt`/`a.txt`,
    `report.`/`report`) или файл на месте папки другого (`r` и `r/x`)."""
    keys = ["/".join(norm_part(p) for p in t.split("/")) for t in targets]
    if len(set(keys)) != len(keys):
        return "export: два файла в одно место папки экспорта - разведите to"
    seen = set(keys)
    for k in keys:
        parts = k.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            return f"export: {shown(k)} лежит внутри файла другого пункта - разведите to"
    return None


def mark_of_the_web(path: Path) -> None:
    """На Windows - поток Zone.Identifier: файл помечен как пришедший из интернета."""
    if IS_WINDOWS:
        with open(f"{path}{MOTW_STREAM}", "w", encoding="ascii", newline="") as f:
            f.write(MOTW_TEXT)


def plain(path: Path, kind: int) -> bool:
    """Обычный файл или папка без ссылки (на Windows ссылка и junction - точки повторного разбора)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_IFMT(st.st_mode) == kind and not getattr(st, "st_reparse_tag", 0)


def parse(items) -> tuple[list[Item], str | None]:
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        return [], 'export - список объектов {"from": "путь в задаче", "to": "имя в папке экспорта"}'
    if len(items) > MAX_ITEMS:
        return [], f"export: не больше {MAX_ITEMS} пунктов за раз"
    parsed = []
    for item in items:
        source = _relative(str(item.get("from") or ""))
        if source is None:
            return [], f"export: from {item.get('from')!r} - путь внутри задачи без .."
        to = _relative(str(item.get("to") or source.rsplit("/", 1)[-1]))
        if to is None:
            return [], f"export: to {item.get('to')!r} - путь внутри папки экспорта без .. и диска"
        if any(device_name(p) for p in to.split("/")):
            return [], f"export: to {to!r} - имя устройства Windows (CON, NUL, COM1...), возьмите другое"
        if shell_parsed(to.rsplit("/", 1)[-1]):
            return [], f"export: to {to!r} - такие файлы разбирает Проводник, не выгружаются"
        parsed.append(Item(source, to))
    if error := clash([i.to for i in parsed]):
        return [], error
    return parsed, None


def _relative(text: str) -> str | None:
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
    absolute = Path(text).is_absolute() or text.strip().startswith(("/", "\\"))   # «/x» на Windows - от корня диска
    if not parts or ".." in parts or any(":" in p for p in parts) or absolute:
        return None
    return "/".join(parts)


def place_error(folder: Path, to: str) -> str | None:
    """Место в папке экспорта свободно, а папки на пути к нему - настоящие папки без ссылок."""
    path = folder
    parts = to.split("/")
    for part in parts[:-1]:
        path = path / part
        if os.path.lexists(path) and not plain(path, stat.S_IFDIR):
            return f"export: путь {shown(to)} идёт через файл или ссылку - возьмите другое to"
    if os.path.lexists(folder.joinpath(*parts)):
        return f"export: {shown(to)} уже есть - перезаписи нет, возьмите другое to"
    return None


def shown(to: str) -> str:
    return f"{PREFIX}{to}"
