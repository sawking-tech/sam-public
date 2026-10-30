"""Настройка ACP-агента из окружения (ADR-022). Команда не задана - агента нет, шлюз ведёт ход сам."""
from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..env import parse_float, read

BACKEND_ENV = "SAM_AGENT_BACKEND"      # sam - свой цикл в шлюзе; acp - главный ход ведёт ACP-агент
BACKEND_SAM, BACKEND_ACP = "sam", "acp"
SOURCE = "acp"                          # имя поставщика в роутере и llm_client

COMMAND_ENV = "SAM_ACP_COMMAND"        # исполняемый файл агента (hermes-acp.exe, opencode)
ARGS_ENV = "SAM_ACP_ARGS"              # аргументы строкой, как в shell
CWD_ENV = "SAM_ACP_CWD"                # рабочая папка сессии агента
NAME_ENV = "SAM_ACP_NAME"              # ярлык модели в мониторе и учёте токенов
MCP_URL_ENV = "SAM_ACP_MCP_URL"        # MCP стенда, который агент получает в сессии
MCP_NAME_ENV = "SAM_ACP_MCP_NAME"
START_TIMEOUT_ENV = "SAM_ACP_START_TIMEOUT_S"
# Имена переменных окружения, которые агент получает (через запятую): SDK даёт подпроцессу только безопасный минимум
# (PATH и подобные), ключ модели и домашняя папка агента (HERMES_HOME) передаются явно
ENV_NAMES_ENV = "SAM_ACP_ENV"
# Домашняя папка агента из шаблона: файлы шаблона копируются в неё перед запуском, путь - агенту переменной HOME_VAR
HOME_TEMPLATE_ENV = "SAM_ACP_HOME_TEMPLATE"   # папка шаблона, например gateway/acp/hermes; пусто - без своей папки
HOME_ENV = "SAM_ACP_HOME"                     # куда копировать; состояние агента (база, логи) остаётся в ней
HOME_VAR_ENV = "SAM_ACP_HOME_VAR"             # имя переменной для агента, у Hermes - HERMES_HOME

ROOT = Path(__file__).resolve().parents[2]
# Своя папка, а не корень репозитория: агенты подхватывают AGENTS.md и прочие файлы контекста из рабочей папки
DEFAULT_CWD = ROOT / "data" / "acp"
DEFAULT_NAME = "acp-agent"
DEFAULT_MCP_URL = "http://localhost:3000/mcp"   # агрегатор стенда (ADR-019)
DEFAULT_MCP_NAME = "sam"
DEFAULT_HOME = ROOT / "data" / "acp" / "home"
# Hermes на холодном старте импортирует свой пакет и поднимает провайдера модели: на ПК 10-20 с
DEFAULT_START_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class AcpConfig:
    command: str
    args: tuple[str, ...] = ()
    cwd: Path = DEFAULT_CWD
    name: str = DEFAULT_NAME
    mcp_url: str = DEFAULT_MCP_URL
    mcp_name: str = DEFAULT_MCP_NAME
    start_timeout_s: float = DEFAULT_START_TIMEOUT_S
    env_names: tuple[str, ...] = ()
    home_template: Path | None = None
    home: Path = DEFAULT_HOME
    home_var: str = ""


def backend(environ: dict | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(BACKEND_ENV) or BACKEND_SAM).strip().lower()


def from_env(environ: dict | None = None) -> AcpConfig | None:
    """Конфиг агента или None, если команда не задана."""
    env = os.environ if environ is None else environ
    command = (env.get(COMMAND_ENV) or "").strip()
    if not command:
        return None
    return AcpConfig(
        command=command,
        args=tuple(shlex.split(env.get(ARGS_ENV) or "", posix=False)),
        cwd=Path(env.get(CWD_ENV) or DEFAULT_CWD),
        name=(env.get(NAME_ENV) or DEFAULT_NAME).strip(),
        mcp_url=(env.get(MCP_URL_ENV) or DEFAULT_MCP_URL).strip(),
        mcp_name=(env.get(MCP_NAME_ENV) or DEFAULT_MCP_NAME).strip(),
        start_timeout_s=read(START_TIMEOUT_ENV, DEFAULT_START_TIMEOUT_S, parse_float, environ=env),
        env_names=tuple(n.strip() for n in (env.get(ENV_NAMES_ENV) or "").split(",") if n.strip()),
        home_template=Path(env[HOME_TEMPLATE_ENV]) if (env.get(HOME_TEMPLATE_ENV) or "").strip() else None,
        home=Path(env.get(HOME_ENV) or DEFAULT_HOME),
        home_var=(env.get(HOME_VAR_ENV) or "").strip(),
    )


def agent_env(cfg: AcpConfig, environ: dict | None = None) -> dict[str, str]:
    """Переменные для агента сверх минимума SDK: только перечисленные в SAM_ACP_ENV и заданные."""
    env = os.environ if environ is None else environ
    out = {name: env[name] for name in cfg.env_names if env.get(name)}
    if cfg.home_template is not None and cfg.home_var:
        # абсолютный путь: агент запускается в своей рабочей папке, относительный путь он понял бы от неё
        out[cfg.home_var] = str(cfg.home.resolve())
    return out


def prepare_home(cfg: AcpConfig) -> None:
    """Файлы шаблона - в домашнюю папку агента (поверх прежних); прочее в папке - состояние агента, не трогается."""
    if cfg.home_template is None:
        return
    cfg.home.mkdir(parents=True, exist_ok=True)
    for src in cfg.home_template.iterdir():
        if src.is_file():
            shutil.copyfile(src, cfg.home / src.name)


def llm_config(cfg: AcpConfig) -> dict:
    """Конфиг поставщика для роутера: тот же вид, что у моделей по HTTP."""
    return {"source": SOURCE, "model": cfg.name, "model_server": None, "api_key": None}
