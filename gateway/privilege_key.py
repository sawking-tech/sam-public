"""Ключ привилегий API агентов (SAM-133, ревью 12 окт): ручки, которые создают агентов, выдают права и меняют модель.

Ключ доступа SAM_API_KEY закрывает шлюз от сети, но на этой машине без него пускается всё, и агент с командой
оболочки создал бы через `POST /sam/agents` агента с любыми правами в обход своих. Эти ручки требуют отдельный ключ
даже для запросов с этой машины. Ключ держат клиенты, которым он положен: ведущий через MCP sam
(mcp/sam_server.py). Лежит он только в файле data/secrets (не в git, не в .env и не в окружении процессов: окружение
наследуют MCP-серверы и агенты); читать файл агентам запрещает политика команд (gateway/tool_policy.json). Файла
нет - шлюз создаёт его при первом обращении. На той же учётке ОС это не граница, а лишь закрытые простые пути:
граница - отдельный пользователь ОС для агентов (SAM-178).
Решение (decide) отделено от файлов и протокола и проверяется без них.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from pathlib import Path

FILE_ENV = "SAM_PRIVILEGE_FILE"
DEFAULT_FILE = Path(__file__).resolve().parents[1] / "data" / "secrets" / "privilege.key"
HEADER_NAME = "X-SAM-Privilege"
HEADER = HEADER_NAME.lower()
KEY_BYTES = 32   # как у токенов сессий: 256 бит, перебором не подобрать
FILE_MODE = 0o600   # читает только своя учётная запись (на проде Ubuntu umask дал бы 0644)
READ_TRIES, READ_PAUSE_S = 20, 0.05   # файл создаёт соседний процесс: дождаться записи, секунда в сумме
# Привилегированные ручки: метод и путь целиком
ROUTES = (
    ("POST", re.compile(r"/sam/agents")),
    ("POST", re.compile(r"/sam/agents/[^/]+/grant")),
    ("POST", re.compile(r"/sam/agents/[^/]+/steps")),   # шаги агенту (SAM-212): иначе агент выдал бы их себе сам
    ("POST", re.compile(r"/sam/agents/[^/]+/effort")),  # силы агенту (SAM-258): иначе агент поднял бы себе модель сам
    ("POST", re.compile(r"/sam/main-model")),
    ("POST", re.compile(r"/sam/llm/switch")),
    ("POST", re.compile(r"/sam/sandbox/exec")),     # тёплый контейнер голосовой беседы (sandbox_warm)
)
NEED = "нужен ключ привилегий: заголовок X-SAM-Privilege (он у ведущего и MCP sam, не у агентов)"


def needs(method: str, path: str) -> bool:
    return any(method.upper() == m and rx.fullmatch(path) for m, rx in ROUTES)


def decide(method: str, path: str, presented: str | None, key: str) -> str | None:
    """None - пустить; иначе причина отказа одной строкой."""
    if not needs(method, path):
        return None
    if presented and key and hmac.compare_digest(presented.encode(), key.encode()):
        return None
    return NEED


def key_file() -> Path:
    return Path(os.getenv(FILE_ENV, "").strip() or DEFAULT_FILE)


_cached: list[str] = []


def key() -> str:
    """Ключ из файла; файла нет - создать атомарно (O_EXCL), опоздавший читает созданное. Пустой не кешируется."""
    if _cached:
        return _cached[0]
    value = _read_or_create(key_file())
    if value:
        _cached.append(value)
    return value


def forget() -> None:
    """Сбросить кеш: файл пересоздан (ответ 401 у клиента), прочитать заново."""
    _cached.clear()


def _read_or_create(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(KEY_BYTES)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    except FileExistsError:
        for _ in range(READ_TRIES):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
            time.sleep(READ_PAUSE_S)
        return ""
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def headers() -> dict[str, str]:
    """Заголовок для клиента с правом на привилегированные ручки."""
    return {HEADER_NAME: key()}


def presented(scope: dict) -> str | None:
    for name, value in scope.get("headers", []):
        if name.decode("latin-1").lower() == HEADER:
            return value.decode("latin-1")
    return None
