"""git clone в задачу песочницы (SAM-83, ADR-016).

Параметр `clone` у sandbox_run: {"url": "https://...", "ref": "<ветка или тег>", "dir": "<папка в задаче>"}. Клон
идёт отдельным запуском контейнера с сетью песочницы под файрволом, команда задачи после него - со своей сетью.

URL - только https:// без данных входа (user:pass@, token@): ext::, file:, ssh и git:// исполняют команды или ходят
мимо файрвола по своим протоколам. Хост - из списка SAM_SANDBOX_CLONE_HOSTS; по умолчанию известные хостинги кода.
Файрвол закрывает дом и хост, но не выгрузку данных задачи наружу, а клон с произвольного адреса - готовый канал
«запросить URL»; список хостингов сужает его до мест, где лежит код. `*` - любой публичный хост. git идёт с
`protocol.allow=never` и только https, без хуков, fsmonitor, подмодулей и перенаправлений на другой хост, без
вопроса пароля (закрытый репозиторий - ошибка, а не ожидание до таймаута).

Решения (разбор, команда) отделены от запуска: запуск делает sandbox.py.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlsplit

from .env import env_int
from .sandbox_pkgs import reserved

HOSTS_ENV = "SAM_SANDBOX_CLONE_HOSTS"
HOSTS_SPLIT = re.compile(r"[;,]")       # как списки корней импорта
DEFAULT_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org")
ANY_HOST = "*"
TIMEOUT_ENV, DEFAULT_TIMEOUT_S = "SAM_SANDBOX_CLONE_TIMEOUT_S", 120   # --depth 1 небольшого репозитория - секунды
# Потолок клона в КБ: ulimit -f не даёт git записать файл больше (пакет --depth 1 - тоже файл), du после клона
# сверяет итог; превышение - папка удаляется. 200 МБ - исходники с ассетами, не датасет
MAX_KB_ENV, DEFAULT_MAX_KB = "SAM_SANDBOX_CLONE_MAX_KB", 200_000
ULIMIT_BLOCK_BYTES = 512                # единица ulimit -f в dash (sh образа песочницы)
# URL обычного репозитория - десятки символов; потолок отсекает мусор и попытки раздуть команду
URL_MAX_CHARS = 500
SIZE_MARK = "@@SAM-CLONE-KB@@"          # после метки - размер клона в КБ (du -sk)
# Хост - метки DNS по RFC 1035: до 63 символов, буквы, цифры и дефис не по краям
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+", re.ASCII)
_PATH = re.compile(r"/[A-Za-z0-9._~/%+-]{1,400}", re.ASCII)      # в пределах URL_MAX_CHARS
_REF = re.compile(r"[A-Za-z0-9._/+-]{1,100}", re.ASCII)          # имя ветки или тега; 100 - с запасом
_DIR = re.compile(r"[A-Za-z0-9._-]{1,100}", re.ASCII)            # одно имя папки в задаче
GIT_SAFE = ("-c protocol.allow=never -c protocol.https.allow=always -c core.hooksPath=/dev/null "
            "-c core.fsmonitor=false -c http.followRedirects=false -c credential.helper=")


@dataclass(frozen=True)
class Clone:
    url: str
    ref: str | None
    dir: str


def hosts() -> tuple[str, ...]:
    """Список хостов; пустая переменная - как не заданная (gateway/env.py), то есть хостинги по умолчанию."""
    raw = os.getenv(HOSTS_ENV, "").strip()
    return tuple(h.strip().lower() for h in HOSTS_SPLIT.split(raw) if h.strip()) if raw else DEFAULT_HOSTS


def parse(spec) -> tuple[Clone | None, str | None]:
    """{url, ref?, dir?} модели → клон; ошибка - одной строкой."""
    if not isinstance(spec, dict):
        return None, 'clone - объект {"url": "https://...", "ref": "ветка или тег", "dir": "папка в задаче"}'
    url, error = check_url(str(spec.get("url") or "").strip())
    if error:
        return None, error
    ref = str(spec.get("ref") or "").strip() or None
    if ref is not None and (not _REF.fullmatch(ref) or ref.startswith("-") or ".." in ref):
        return None, f"clone: ref {ref!r} - имя ветки или тега из букв, цифр и ._/-"
    folder = str(spec.get("dir") or "").strip() or default_dir(url)
    if not _DIR.fullmatch(folder) or folder in (".", ".."):
        return None, f"clone: dir {folder!r} - одно имя папки в задаче из букв, цифр и ._-"
    if folder.lower().startswith(".git") or reserved(folder):
        return None, f"clone: dir {folder!r} - служебное имя (.git*, папки пакетов, lock-файлы), возьмите другое"
    return Clone(url, ref, folder), None


def check_url(url: str) -> tuple[str | None, str | None]:
    if not url:
        return None, "clone: нужен url вида https://github.com/автор/репозиторий"
    if len(url) > URL_MAX_CHARS or any(c.isspace() or ord(c) < 32 for c in url):
        return None, "clone: url длиннее потолка или с пробелами"
    parts = urlsplit(url)
    if parts.scheme != "https" or url.lower().startswith(("ext::", "file:")):
        return None, "clone: только https:// - ext::, file:, ssh и git:// нельзя"
    if "@" in parts.netloc:
        return None, "clone: url без данных входа (user:pass@, token@) - только публичный репозиторий"
    host = (parts.hostname or "").lower()
    try:
        parts.port
    except ValueError:
        return None, f"clone: хост {parts.netloc!r} - имя публичного хоста без порта"
    if not _HOST.fullmatch(host) or parts.port not in (None, 443):
        return None, f"clone: хост {parts.netloc!r} - имя публичного хоста без порта"
    if _ip_literal(host):       # и при «*»: адрес вместо имени - путь в обход списка и к любой машине
        return None, f"clone: хост {host} - адрес, нужно имя хостинга"
    allowed = hosts()
    if ANY_HOST not in allowed and host not in allowed:
        return None, f"clone: хост {host} не в списке ({', '.join(allowed)}) - список в {HOSTS_ENV}"
    if not _PATH.fullmatch(parts.path) or ".." in parts.path or parts.query or parts.fragment:
        return None, "clone: путь репозитория из букв, цифр и ._-/ без ?, # и .."
    return url, None


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[0-9.]+", host))     # 10.1 и подобные короткие формы git тоже примет


def default_dir(url: str) -> str:
    name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name or "repo"


def max_kb() -> int:
    return env_int(MAX_KB_ENV, DEFAULT_MAX_KB)


def command(c: Clone) -> str:
    """Клон в /work/<dir> под ulimit -f и размер за меткой. URL и ref прошли разбор, shlex.quote - на всякий случай.
    --filter=blob:limit не берётся: пропущенные им файлы git докачивает при checkout, размер он не ограничивает."""
    branch = f"--branch {shlex.quote(c.ref)} " if c.ref else ""
    target = f"/work/{c.dir}"
    blocks = max_kb() * 1024 // ULIMIT_BLOCK_BYTES
    return (f"ulimit -f {blocks} && GIT_TERMINAL_PROMPT=0 git {GIT_SAFE} clone --depth 1 --no-recurse-submodules "
            f"{branch}-- {shlex.quote(c.url)} {target} && echo {SIZE_MARK} && du -sk {target}")


def cleanup(c: Clone) -> str:
    return f"rm -rf /work/{c.dir}"


def size_kb(output: str) -> int | None:
    _, sep, rest = (output or "").partition(SIZE_MARK)
    first = rest.split()[0] if sep and rest.split() else ""
    return int(first) if first.isdigit() else None


def git_error(err: str) -> str:
    """Причина для модели - только строка `fatal:` git; `remote:` с сервера в ответ не идёт."""
    fatal = next((s.strip() for s in (err or "").splitlines() if s.strip().startswith("fatal:")), "")
    return fatal[:URL_MAX_CHARS] or "без подробностей"
