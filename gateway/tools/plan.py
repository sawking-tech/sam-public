"""plan - план агента одним вызовом: where, check, take, done, add (SAM-235).

Скилл plan-and-iterate живёт в `plan.mjs`; агент интерпретатор и путь не пишет, их берёт инструмент. Файл плана - из
`SAM_PLAN_FILE`, скрипт - из `SAM_PLAN_TOOL` или из глобальных скиллов. Запуск процесса приходит снаружи (Runner), чтобы
логику проверять без node.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
PLAN_FILE_ENV = "SAM_PLAN_FILE"
WITHIN_ENV = "SAM_PLAN_WITHIN"          # слаг узла задачи агента в плане: правка только внутри него (plan.mjs --within)
DEFAULT_PLAN_FILE = ".claude/session-context/plan.kdl"        # от корня репозитория
TOOL_ENV = "SAM_PLAN_TOOL"
DEFAULT_TOOL = Path.home() / ".claude" / "skills" / "plan-and-iterate" / "plan.mjs"
NODE = "node"
RUN_TIMEOUT_S = 30
WHERE_COUNT_DEFAULT = 6

Runner = Callable[[list[str], dict], tuple[int, str]]   # (argv, окружение) -> (код выхода, вывод)

ACTIONS = ("where", "check", "take", "done", "add")
NEEDS_SLUG = ("take", "done", "add")
WRITES = ("take", "done", "add")     # правят файл плана: без своего SAM_PLAN_FILE агенту закрыты
PLAN_NOT_SET = (f"Своего плана у задачи нет ({PLAN_FILE_ENV} или {WITHIN_ENV} не заданы): take, done и add правят общий план "
                "и закрыты; where и check доступны. Прогресс пиши report_status")


def run_process(argv: list[str], env: dict) -> tuple[int, str]:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=RUN_TIMEOUT_S)
    return done.returncode, (done.stdout + done.stderr).strip()


def plan_file(repo: Path = REPO, environ=None) -> Path:
    raw = (environ if environ is not None else os.environ).get(PLAN_FILE_ENV) or DEFAULT_PLAN_FILE
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def tool_path(environ=None) -> Path:
    raw = (environ if environ is not None else os.environ).get(TOOL_ENV)
    return Path(raw) if raw else DEFAULT_TOOL


def build_args(params: dict) -> list[str] | str:
    """Аргументы plan.mjs после имени скрипта; строка - если вызов составлен неверно."""
    action = str(params.get("action") or "").strip()
    if action not in ACTIONS:
        return f"Действие плана: {' | '.join(ACTIONS)}; получено {action!r}"
    slug = str(params.get("slug") or "").strip()
    if action in NEEDS_SLUG and not slug:
        return f"{action}: укажи slug пункта плана (слаг в квадратных скобках из where)"
    if action == "where":
        return ["where", str(int(params.get("count") or WHERE_COUNT_DEFAULT))]
    if action == "add":
        title, anchor = str(params.get("title") or "").strip(), str(params.get("anchor") or "").strip()
        position = "--after" if params.get("position") == "after" else "--before"
        if not title or not anchor:
            return "add: нужны title (название пункта) и anchor (слаг пункта, рядом с которым завести)"
        return ["add", slug, title, position, anchor]
    return [action, slug] if slug else [action]


def plan_impl(params: dict, run: Runner = run_process, repo: Path = REPO, environ=None) -> str:
    args = build_args(params)
    if isinstance(args, str):
        return args
    env_now = environ if environ is not None else os.environ
    within = env_now.get(WITHIN_ENV)
    if args[0] in WRITES and not (env_now.get(PLAN_FILE_ENV) or within):
        return PLAN_NOT_SET
    if within:   # агент видит и правит только узел своей задачи в общем плане (SAM-257)
        args = [*args, "--within", within]
    script = tool_path(environ)
    if not script.exists():
        return f"plan.mjs не найден: {script}. Задай {TOOL_ENV} путём к скрипту плана"
    env = dict(os.environ if environ is None else environ)
    env["PLAN_FILE"] = str(plan_file(repo, environ))
    try:
        code, out = run([NODE, str(script), *args], env)
    except FileNotFoundError:
        return "node не найден: плану нужен Node.js в PATH"
    except subprocess.TimeoutExpired:
        return f"plan.mjs не ответил за {RUN_TIMEOUT_S} с: повтори вызов"
    return out if code == 0 else f"План вернул код {code}:\n{out}"
