"""Coding agents tools — backend-agnostic делегирование коду/git/файлам.

**Это escape hatch, не основной путь.** SAM как агентная платформа сам
умеет работать с MCP-tools и решать большинство задач. Делегирование
сюда — только когда задача реально специфична для coding-CLI:
  - сложное многошаговое редактирование с пониманием AST
  - git операции с конфликтами
  - bash-скрипты которые требуют итераций
  - анализ незнакомой кодовой базы

Под капотом — registry coding-agent backends. Каждый backend = CLI которое
принимает текстовое описание задачи, выполняет, возвращает результат.

Сейчас поддерживаются:
  - claude   — Claude Code: `claude -p <задача> --output-format json`
  - opencode — OpenCode: `opencode run <задача>` (`opencode run --help`, 1.14; `-p` у него - пароль сервера)

SAM-60 (9 окт):
  - CLI ищется в PATH (shutil.which: на ПК это claude.exe и opencode.exe, а не .cmd, которые искал прежний
    код) или берётся из SAM_CLAUDE_CLI / SAM_OPENCODE_CLI;
  - папка - только внутри корней SAM_CODING_ROOTS (через os.pathsep); по умолчанию корень один - папка
    репозиториев, где лежит SAM. workdir - путь внутри корня или имя репозитория («SAM»); не задан - первый корень;
  - агент: явный параметр, иначе слово в задаче («через OpenCode»), иначе SAM_CODING_AGENT, иначе первый доступный;
  - таймаут SAM_CODING_TIMEOUT_S с потолком SAM_CODING_MAX_TIMEOUT_S; по таймауту CLI убивается вместе с
    дочерними процессами (taskkill /T на Windows, группа процессов на POSIX);
  - ошибка - одна строка: что не так и что делать.

TOOL API:
  delegate_impl(params, registry, roots) → str
    params:
      task: str (required)            — описание задачи
      workdir: str (optional)         — папка внутри корней или имя репозитория
      agent: str (default="auto")     — "claude" | "opencode" | "auto"
      timeout: int (optional)         — таймаут в секундах

Backend interface:
  name: str
  is_available() -> bool
  run(task, workdir, timeout) -> str
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from ..env import env_int
from ..proc_tree import NEW_GROUP, kill_tree

_SAM_REPO = Path(__file__).resolve().parents[2]
ROOTS_ENV = "SAM_CODING_ROOTS"
DEFAULT_ROOTS = str(_SAM_REPO.parent)        # папка репозиториев, где лежит SAM
AGENT_ENV = "SAM_CODING_AGENT"
AUTO = "auto"
# Прежний таймаут tool; задача агента кода - правка, тесты, коммит, минуты
DEFAULT_TIMEOUT_S = 300
TIMEOUT_ENV = "SAM_CODING_TIMEOUT_S"
# Потолок для таймаута от модели: «timeout: 99999» не должен держать CLI сутки
MAX_TIMEOUT_ENV = "SAM_CODING_MAX_TIMEOUT_S"
DEFAULT_MAX_TIMEOUT_S = 1800
# Модель агента кода Claude Code (Opus - только для сложного программирования); пусто - по
# умолчанию самого CLI. Ведущий разговора берёт другую модель: LEAD_MODEL_ENV в gateway/llm_router.py
CLAUDE_MODEL_ENV = "SAM_CLAUDE_CODING_MODEL"
DEFAULT_CLAUDE_MODEL = "opus"
VERSION_TIMEOUT_S = 5                        # `--version` отвечает мгновенно
RESULT_MAX_CHARS = 5000                      # итог в контекст воркера, как было
# Адрес шлюза для канала агента к создателю (SAM-164): процесс агента на той же машине
GATEWAY_URL_ENV, DEFAULT_GATEWAY_URL = "SAM_SERVER", "http://localhost:8000"
ERROR_MAX_CHARS = 300
# Как агента называют в просьбе. Выбирает только оборот «через X» / «с помощью X» / «via X»: голое слово
# бывает предметом задачи («поправь CLAUDE.md»)
AGENT_WORDS: dict[str, tuple[str, ...]] = {
    "opencode": ("opencode", "open code", "опенкод", "опен код"),
    "claude": ("claude code", "claude", "клод код", "клод"),
}
AGENT_BY = ("через", "с помощью", "via", "with", "using")
CASE_ENDINGS = ("ом", "а", "у", "е")        # «через Клода», «Опенкодом»
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ─── Запуск CLI: единственное место, которое трогает процессы ────────────────

@dataclass(frozen=True)
class CliResult:
    code: int | None
    out: str
    err: str
    timed_out: bool = False


def run_cli(argv: list[str], cwd: str | None, timeout: float, env: dict | None = None) -> CliResult:
    """Запустить CLI без stdin; по таймауту убить его вместе с дочерними процессами."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace", **NEW_GROUP)
    try:
        out, err = proc.communicate(timeout=timeout)
        return CliResult(proc.returncode, out or "", err or "")
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        out, err = proc.communicate()
        return CliResult(None, out or "", err or "", timed_out=True)


def resolve_cli(env_var: str, name: str) -> str | None:
    """Путь к CLI: из env_var, иначе поиск в PATH с учётом PATHEXT (claude.exe, claude.cmd)."""
    return os.getenv(env_var, "").strip() or shutil.which(name)


def first_line(text: str) -> str:
    line = next((s.strip() for s in _ANSI.sub("", text or "").splitlines() if s.strip()), "")
    return line[:ERROR_MAX_CHARS]


# ─── Backend interface ────────────────────────────────────────────────────────

class CodingAgentBackend(Protocol):
    name: str

    def is_available(self) -> bool:
        """Проверить что CLI доступен (есть в PATH, авторизован, etc.)."""
        ...

    def run(self, task: str, workdir: str | None, timeout: int, task_id: str = "") -> str:
        """Выполнить задачу, вернуть строку с результатом или сообщение об
        ошибке. Не бросает исключения наружу. task_id - задача воркера: канал к создателю (SAM-164)."""
        ...


class CliBackend:
    """Общее у агентов кода: найти CLI, запустить, превратить итог или сбой в строку.
    Что запускать и как читать вывод - параметры (argv, parse), а не подклассы."""

    def __init__(self, name: str, title: str, cli_env: str, cli_name: str,
                 argv: Callable[[str, str], list[str]], parse: Callable[[str], tuple[str, bool]],
                 runner: Callable[..., CliResult] = run_cli, env: Callable[[], dict] | None = None,
                 channel_args: Callable[[str], list[str]] | None = None) -> None:
        self.name, self.title = name, title
        self._cli_env, self._cli_name = cli_env, cli_name
        self._argv, self._parse, self._runner, self._env = argv, parse, runner, env
        # Аргументы CLI, подключающие канал к создателю по id задачи (SAM-164); None - каркас его не умеет
        self._channel_args = channel_args

    def _cli(self) -> str | None:
        return resolve_cli(self._cli_env, self._cli_name)

    def is_available(self) -> bool:
        cli = self._cli()
        if not cli:
            return False
        try:
            return self._runner([cli, "--version"], None, VERSION_TIMEOUT_S).code == 0
        except OSError:
            return False

    def run(self, task: str, workdir: str | None, timeout: int, task_id: str = "") -> str:
        cli = self._cli()
        if not cli:
            return f"{self.title}: CLI {self._cli_name} не найден - установите его или задайте {self._cli_env}"
        argv = self._argv(cli, task) + (self._channel_args(task_id) if task_id and self._channel_args else [])
        try:
            res = self._runner(argv, workdir, timeout, self._env() if self._env else None)
        except OSError as e:
            return f"{self.title}: не запустился ({e}); проверьте {self._cli_env}"
        if res.timed_out:
            return (f"{self.title}: не уложился в {timeout} с и остановлен; разбейте задачу или поднимите "
                    f"{TIMEOUT_ENV}")
        if res.code != 0:
            return f"{self.title}: код {res.code}: {first_line(res.err or res.out) or 'без вывода'}"
        text, is_error = self._parse(res.out)
        if is_error:
            return f"{self.title}: {first_line(text) or 'сообщил об ошибке без текста'}"
        return text[:RESULT_MAX_CHARS]


# ─── Claude Code ──────────────────────────────────────────────────────────────

def claude_model_args(env_var: str, default: str) -> list[str]:
    """`--model X` из env_var (default, если не задана); пустое значение, заданное явно, - модель CLI."""
    model = os.getenv(env_var)
    model = default if model is None else model.strip()
    return ["--model", model] if model else []


def _claude_argv(cli: str, task: str) -> list[str]:
    return [cli, "-p", task, "--output-format", "json", "--dangerously-skip-permissions",
            *claude_model_args(CLAUDE_MODEL_ENV, DEFAULT_CLAUDE_MODEL)]


def parse_claude(out: str) -> tuple[str, bool]:
    """`--output-format json`: {"result": ..., "is_error": ...}; не JSON - вывод как есть."""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out, False
    if not isinstance(data, dict):
        return out, False
    return str(data.get("result", data.get("content", "")) or ""), bool(data.get("is_error"))


# Секреты SAM в окружении ребёнку не идут: агент кода не получает ключей доступа и привилегий (SAM-177)
SECRET_ENV = re.compile(r"^SAM_.*(KEY|SECRET|PRIVILEGE)", re.IGNORECASE)


def _agent_env() -> dict:
    """Окружение процесса агента кода, общее для всех каркасов: без секретов SAM и без CLAUDECODE (иначе claude
    не запустится вне нашей сессии)."""
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not SECRET_ENV.match(k)}


def _claude_channel_args(task_id: str) -> list[str]:
    """Канал к создателю - MCP-сервер процесса агента (SAM-164): ask_creator, report_status, messages."""
    from .agent_channel import mcp_config
    # --strict-mcp-config: только этот канал, без .mcp.json проекта (там MCP sam с ключом привилегий, SAM-177)
    return ["--mcp-config", mcp_config(task_id, os.getenv(GATEWAY_URL_ENV, DEFAULT_GATEWAY_URL)), "--strict-mcp-config"]


def claude_code_backend(runner: Callable[..., CliResult] = run_cli) -> CliBackend:
    return CliBackend("claude", "Claude Code", "SAM_CLAUDE_CLI", "claude", _claude_argv, parse_claude,
                      runner, _agent_env, channel_args=_claude_channel_args)


# ─── OpenCode ─────────────────────────────────────────────────────────────────

def _opencode_argv(cli: str, task: str) -> list[str]:
    # Без подтверждений, как у Claude Code: спросить некого, у процесса нет stdin
    return [cli, "run", task, "--dangerously-skip-permissions"]


def parse_opencode(out: str) -> tuple[str, bool]:
    """Формат default - текст для терминала: без цветовых кодов."""
    return _ANSI.sub("", out).strip(), False


def opencode_backend(runner: Callable[..., CliResult] = run_cli) -> CliBackend:
    return CliBackend("opencode", "OpenCode", "SAM_OPENCODE_CLI", "opencode", _opencode_argv, parse_opencode,
                      runner, _agent_env)


# ─── Registry & selection ─────────────────────────────────────────────────────

# Порядок - приоритет auto: claude → opencode; SAM_CODING_AGENT или слово в задаче выбирают явно
DEFAULT_BACKENDS: list[Callable[[], CliBackend]] = [claude_code_backend, opencode_backend]


def build_default_registry() -> dict[str, CodingAgentBackend]:
    """Собрать registry из дефолтных backends."""
    return {b.name: b for b in (make() for make in DEFAULT_BACKENDS)}


def select_backend(
    registry: dict,
    preferred: str = AUTO,
) -> tuple[CodingAgentBackend | None, str]:
    """Выбрать backend по приоритету.

    Returns (backend, error_message). Если backend=None — есть error.
    """
    if preferred and preferred != AUTO:
        backend = registry.get(preferred)
        if backend is None:
            return None, (
                f"Coding agent '{preferred}' не зарегистрирован. "
                f"Доступны: {', '.join(registry.keys())}"
            )
        if not backend.is_available():
            return None, f"Coding agent '{preferred}' не доступен: CLI не найден или не отвечает на --version"
        return backend, ""

    # auto — пробуем по порядку приоритета
    for backend in registry.values():
        try:
            if backend.is_available():
                return backend, ""
        except Exception:
            continue
    return None, (
        "Ни один coding agent не доступен. Установи claude или opencode CLI."
    )


def pick_agent(explicit: str | None, task: str) -> str:
    """Явный параметр, иначе слово в задаче, иначе SAM_CODING_AGENT, иначе auto."""
    explicit = str(explicit or "").strip().lower()
    if explicit and explicit != AUTO:
        return explicit
    low = task.lower()
    by = "|".join(map(re.escape, AGENT_BY))
    endings = "|".join(CASE_ENDINGS)
    for name, words in AGENT_WORDS.items():
        alts = "|".join(map(re.escape, words))
        if re.search(rf"(?<!\w)(?:{by})\s+(?:{alts})(?:{endings})?(?!\w|\.\w)", low):
            return name
    return os.getenv(AGENT_ENV, "").strip().lower() or AUTO


def allowed_roots() -> list[Path]:
    raw = os.getenv(ROOTS_ENV, "").strip() or DEFAULT_ROOTS
    return [Path(r.strip()).expanduser().resolve() for r in raw.split(os.pathsep) if r.strip()]


def resolve_workdir(workdir, roots: list[Path]) -> tuple[Path | None, str]:
    """Папка агента внутри корней. Имя без пути ищется в каждом корне («SAM»)."""
    listed = ", ".join(str(r) for r in roots)
    if not roots:
        return None, f"Нет разрешённых папок для агента кода: задайте {ROOTS_ENV}"
    text = str(workdir or "").strip()
    if not text:
        return roots[0], ""
    raw = Path(text).expanduser()
    candidates = [raw] if raw.is_absolute() else [root / raw for root in roots]
    for cand in candidates:
        path = cand.resolve()
        if any(path == root or path.is_relative_to(root) for root in roots):
            if path.is_dir():
                return path, ""
            return None, f"Папки {path} нет: назови репозиторий внутри {listed}"
    return None, f"Папка {text} вне разрешённых ({listed}): агент кода работает только там, список - {ROOTS_ENV}"


def task_timeout(value) -> int:
    """Таймаут из параметра или SAM_CODING_TIMEOUT_S; мусор - значение по умолчанию; не выше потолка."""
    ceiling = env_int(MAX_TIMEOUT_ENV, DEFAULT_MAX_TIMEOUT_S)
    default = env_int(TIMEOUT_ENV, DEFAULT_TIMEOUT_S)
    asked = _int(value, default)
    return max(1, min(asked if asked > 0 else default, ceiling))


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ─── Tool impl ────────────────────────────────────────────────────────────────

def delegate_impl(
    params: dict,
    registry: dict | None = None,
    roots: list[Path] | None = None,
    task_id: str = "",
) -> str:
    """Делегировать задачу coding-agent'у через CLI.

    Args:
        params: task / workdir / agent / timeout
        registry: dict[str, CodingAgentBackend]; None = default_registry
        roots: разрешённые папки; None - SAM_CODING_ROOTS
    """
    task = str(params.get("task") or "").strip()
    if not task:
        return "Нужен task — описание задачи для coding agent'а"
    workdir, error = resolve_workdir(params.get("workdir"), allowed_roots() if roots is None else roots)
    if workdir is None:
        return error
    preferred = pick_agent(params.get("agent"), task)
    timeout = task_timeout(params.get("timeout"))

    if registry is None:
        registry = build_default_registry()

    backend, error = select_backend(registry, preferred)
    if backend is None:
        return error

    return backend.run(task, str(workdir), timeout, task_id=task_id)
