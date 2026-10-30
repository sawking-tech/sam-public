"""Копия файлов с ПК в папку задачи песочницы (SAM-83, ADR-016).

Параметр `import` у sandbox_run: [{"from": "<путь на ПК>", "to": "<путь в задаче>"}]. Источник - только внутри
корней SAM_SANDBOX_IMPORT_ROOTS (через ; или ,); пусто - импорт выключен. Корень, который совпадает с папкой задач
песочницы или данных SAM, лежит в ней или содержит её, и корень диска не используются. Шлюз копирует байты сам, без
общих монтирований: контейнер получает копию, а не доступ к папке ПК.

Путь от корня до источника не проходит через ссылку (симлинк, junction) и через имя секрета (.git, .ssh, secrets,
...); внутри папки ссылки, спецфайлы и секреты по маскам пропускаются. На Windows имя с «:» (поток NTFS, `.env::$DATA`)
- отказ, короткое имя 8.3 сверяется с масками по длинному. Потолки - число файлов и общий размер, обход прерывается
сразу на превышении. Проверки идут до первой записи: отказ проверки ничего не оставляет в задаче; файл, который
сменился между планом и чтением (другой файл на диске или размер), прерывает копию, и скопированное до него
остаётся. В ответах модели пути ПК - относительно корня с его номером, без абсолютных путей.

Решения (корни, разбор, план копии) отделены от записи: запись в папку задачи и проверка места назначения приходят
параметрами от sandbox.py (там безопасная запись и замок задачи).
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .env import env_int

ROOTS_ENV = "SAM_SANDBOX_IMPORT_ROOTS"
LIST_SPLIT = re.compile(r"[;,]")        # «;» и «,»: двоеточие занято буквой диска Windows
MAX_FILES_ENV, DEFAULT_MAX_FILES = "SAM_SANDBOX_IMPORT_MAX_FILES", 2000
MAX_BYTES_ENV, DEFAULT_MAX_BYTES = "SAM_SANDBOX_IMPORT_MAX_BYTES", 50_000_000   # проект с данными, не диск целиком
MAX_ITEMS = 10                          # пунктов import за один запуск
SKIP_ENV = "SAM_SANDBOX_IMPORT_SKIP"    # свои маски через ; или , - к списку ниже
# Что не копируется никогда, по имени файла или папки без учёта регистра: окружение, ключи и сертификаты, конфиги с
# токенами (ssh, облака, реестры, git), хранилища паролей, состояние terraform
SKIP_PATTERNS = (
    ".env", ".env.*", "*.env", ".envrc", ".git", ".git-credentials", ".ssh", ".aws", ".azure", ".gnupg", ".docker",
    ".kube", ".npmrc", ".pypirc", ".netrc", "_netrc", ".htpasswd", ".pgpass",
    ".credentials*", "*credentials*.json", ".claude.json", "gh",     # токены Claude Code, MCP, GitHub CLI (gh/hosts.yml)
    "secrets", "secrets*", "secret*", "credentials", "credentials*", "service-account*.json",
    # токены, но не tokenizer.json моделей Hugging Face
    "token.json", "tokens.json", "token_*.json", "*_token.json",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.asc", "*.ppk", "*.tfstate",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
)
SKIPPED_LISTED = 20                     # столько пропущенных путей называет журнал
IS_WINDOWS = os.name == "nt"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    source: Path            # путь на ПК, проверенный внутри корня
    to: str                 # путь в задаче (posix)
    shown: str              # как называть источник модели: «#номер корня:путь от корня»


@dataclass(frozen=True)
class Planned:
    source: Path
    to: str
    size: int
    ident: tuple[int, int]  # (st_dev, st_ino) из плана: файл при чтении должен быть тем же
    shown: str


@dataclass
class Plan:
    """Что копировать и что пропущено (с причиной)."""
    files: list[Planned] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes: int = 0


class _Ceiling(Exception):
    pass


# ─── корни ────────────────────────────────────────────────────────────────────

def roots(protected: tuple[Path, ...] = ()) -> list[Path]:
    """Корни из env. Корень диска и корень, который пересекается с защищённой папкой (папка задач песочницы,
    данные SAM), не используются - строка в лог."""
    found = []
    for raw in LIST_SPLIT.split(os.getenv(ROOTS_ENV, "")):
        if not raw.strip():
            continue
        root = Path(raw.strip()).expanduser().resolve()
        if why := unsafe_root(root, [p.resolve() for p in protected]):
            log.warning("%s: корень %s не используется - %s", ROOTS_ENV, root, why)
            continue
        found.append(root)
    return found


def unsafe_root(root: Path, protected: list[Path]) -> str | None:
    """Почему папку ПК нельзя открыть песочнице (корень импорта, папка экспорта); None - можно."""
    if root.parent == root:
        return "это корень диска"
    home = Path.home().resolve()
    if root == home or home.is_relative_to(root):
        return "это домашняя папка или папка над ней - открыла бы всё сразу"
    for p in protected:
        if root == p or root.is_relative_to(p) or p.is_relative_to(root):
            return f"пересекается с папкой SAM {p}"
    return None


def skip_patterns() -> tuple[str, ...]:
    extra = tuple(p.strip().lower() for p in LIST_SPLIT.split(os.getenv(SKIP_ENV, "")) if p.strip())
    return SKIP_PATTERNS + extra


def skipped_name(name: str, patterns: tuple[str, ...]) -> bool:
    """Имя под маской; на Windows сравнивается имя до первого «:» - `.env:поток` то же, что `.env`."""
    base = name.split(":", 1)[0] if IS_WINDOWS else name
    return any(fnmatch.fnmatchcase(base.lower(), p) for p in patterns)


def _plain(st, kind: int) -> bool:
    """Обычный файл или папка без ссылки; на Windows ссылка и junction - точки повторного разбора."""
    return stat.S_IFMT(st.st_mode) == kind and not getattr(st, "st_reparse_tag", 0)


def _shown(index: int, rel) -> str:
    return f"#{index + 1}:{'/'.join(rel) or '.'}"


# ─── разбор ───────────────────────────────────────────────────────────────────

def parse(items, allowed: list[Path]) -> tuple[list[Item], str | None]:
    """Список {from, to} модели → пункты с проверенным источником; ошибка - одной строкой."""
    if not allowed:
        return [], f"Копия с ПК выключена: задайте папки, откуда можно брать файлы, в {ROOTS_ENV}"
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        return [], 'import - список объектов {"from": "путь на ПК", "to": "путь в задаче"}'
    if len(items) > MAX_ITEMS:
        return [], f"import: не больше {MAX_ITEMS} пунктов за раз"
    parsed = []
    for item in items:
        found, error = resolve_source(str(item.get("from") or "").strip(), allowed)
        if error:
            return [], error
        source, shown = found
        to = str(item.get("to") or source.name).strip().replace("\\", "/")
        parsed.append(Item(source, to, shown))
    return parsed, None


def resolve_source(text: str, allowed: list[Path]) -> tuple[tuple[Path, str] | None, str | None]:
    """Источник внутри корня: без «..», ссылок и имён секретов на пути от корня; есть на диске. Пути в ошибках -
    как их дала модель или относительно корня."""
    outside = f"import: путь вне разрешённых папок (их {len(allowed)}, список - {ROOTS_ENV})"
    if not text:
        return None, "import: у пункта нет from - путь на ПК"
    raw = Path(text).expanduser()
    if ".." in raw.parts:
        return None, "import: путь с «..» - нужен прямой путь внутри разрешённой папки"
    candidates = [raw] if raw.is_absolute() else [root / raw for root in allowed]
    # относительный путь ищется в каждом корне: найденный, но запрещённый (ссылка, секрет) важнее «нет на ПК»
    refused = missing = None
    for cand in candidates:
        for index, root in enumerate(allowed):
            rel = _relative(Path(os.path.abspath(cand)), root)
            if rel is None:
                continue
            error, absent = _check_way(root, rel, _shown(index, rel))
            if error is None:
                return (root.joinpath(*rel), _shown(index, rel)), None
            if absent:
                missing = missing or error
            else:
                refused = refused or error
    return None, refused or missing or outside


def _relative(path: Path, root: Path) -> tuple[str, ...] | None:
    norm, base = os.path.normcase(str(path)), os.path.normcase(str(root))
    if norm == base:
        return ()
    if not norm.startswith(base.rstrip("\\/") + os.sep):
        return None
    return path.parts[len(root.parts):]


def _check_way(root: Path, rel: tuple[str, ...], shown: str) -> tuple[str | None, bool]:
    """Каждая часть пути от корня: без «:» на Windows, не имя секрета (на Windows - и по длинному имени), не ссылка.
    (отказ или None, отказ ли это «нет на ПК»)."""
    patterns, path = skip_patterns(), root
    for part in rel:
        if IS_WINDOWS and ":" in part:
            return f"import: {shown} - имя с «:» (поток NTFS) нельзя", False
        if skipped_name(part, patterns):
            return f"import: {shown} идёт через папку или файл секретов - такие не копируются", False
        path = path / part
        try:
            st = os.lstat(path)
        except OSError:
            return f"import: {shown} нет на ПК", True
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0):
            return f"import: {shown} идёт через ссылку - укажите настоящую папку", False
        if skipped_name(_long_name(path), patterns):
            return f"import: {shown} идёт через папку или файл секретов - такие не копируются", False
    return None, False


def _long_name(path: Path) -> str:
    """Настоящее имя последней части: на Windows короткое 8.3 (CREDEN~1) раскрывается через resolve."""
    if not IS_WINDOWS:
        return path.name
    try:
        return path.resolve().name
    except OSError:
        return path.name


# ─── план копии ───────────────────────────────────────────────────────────────

def plan(items: list[Item], dest_error: Callable[[str], str | None]) -> tuple[Plan, str | None]:
    """Обойти источники без перехода по ссылкам, отсеять секреты и спецфайлы, проверить потолки, места в задаче и
    пересечения мест между собой. `dest_error(путь в задаче)` - отказ, если туда писать нельзя."""
    patterns, result = skip_patterns(), Plan()
    limits = (env_int(MAX_FILES_ENV, DEFAULT_MAX_FILES), env_int(MAX_BYTES_ENV, DEFAULT_MAX_BYTES))
    try:
        for item in items:
            _collect(item, patterns, result, limits)
    except _Ceiling as e:
        return result, str(e)
    if not result.files:
        return result, "import: копировать нечего - источник пуст или всё в нём пропущено по маскам"
    if error := _clash([f.to for f in result.files]):
        return result, error
    for f in result.files:
        if error := dest_error(f.to):
            return result, error
    return result, None


def _clash(targets: list[str]) -> str | None:
    """Два пункта в одно место или файл на месте папки другого пункта (`a` и `a/b`)."""
    fold = str.lower if IS_WINDOWS else str        # регистр различает только Linux: Makefile и makefile - два файла
    seen = {fold(t.strip("/")) for t in targets}
    if len(seen) != len(targets):
        return "import: два источника пишут в одно место задачи - разведите to"
    for t in seen:
        parts = t.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            return f"import: {t} лежит внутри файла другого пункта - разведите to"
    return None


def _add(result: Plan, planned: Planned, limits: tuple[int, int]) -> None:
    max_files, max_bytes = limits
    result.files.append(planned)
    result.bytes += planned.size
    if len(result.files) > max_files:
        raise _Ceiling(f"import: больше {max_files} файлов ({MAX_FILES_ENV}) - возьмите папку уже")
    if result.bytes > max_bytes:
        raise _Ceiling(f"import: больше {max_bytes} байт ({MAX_BYTES_ENV}) - возьмите папку уже")


def _collect(item: Item, patterns: tuple[str, ...], result: Plan, limits: tuple[int, int]) -> None:
    st = os.lstat(item.source)
    if _plain(st, stat.S_IFREG):
        _add(result, Planned(item.source, item.to, st.st_size, (st.st_dev, st.st_ino), item.shown), limits)
        return
    if not _plain(st, stat.S_IFDIR):
        result.skipped.append(f"{item.shown} (ссылка или спецфайл)")
        return
    for dirpath, dirnames, filenames in os.walk(item.source, followlinks=False, onerror=lambda e: None):
        here = Path(dirpath)
        rel_dir = here.relative_to(item.source).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        keep = []
        for d in dirnames:
            if skipped_name(d, patterns):
                result.skipped.append(f"{item.shown}/{prefix}{d} (маска)")
            elif not _plain(os.lstat(here / d), stat.S_IFDIR):
                result.skipped.append(f"{item.shown}/{prefix}{d} (ссылка)")
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if skipped_name(f, patterns):
                result.skipped.append(f"{item.shown}/{prefix}{f} (маска)")
                continue
            st = os.lstat(here / f)
            if not _plain(st, stat.S_IFREG):
                result.skipped.append(f"{item.shown}/{prefix}{f} (ссылка или спецфайл)")
                continue
            _add(result, Planned(here / f, f"{item.to.rstrip('/')}/{prefix}{f}", st.st_size,
                                 (st.st_dev, st.st_ino), f"{item.shown}/{prefix}{f}"), limits)


def read_source(planned: Planned) -> bytes:
    """Байты файла ПК без перехода по ссылке; файл тот же, что в плане (устройство и номер), и того же размера -
    иначе OSError с путём от корня."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(planned.source, flags), "rb") as f:
        st = os.fstat(f.fileno())
        if not _plain(st, stat.S_IFREG) or (st.st_dev, st.st_ino) != planned.ident:
            raise OSError(f"{planned.shown} подменили после проверки")
        data = f.read(planned.size + 1)
    if len(data) != planned.size:
        raise OSError(f"{planned.shown} изменился во время копии")
    return data
