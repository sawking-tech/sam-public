"""workspace - рабочее место задачи агента: git worktree от dev, без копий репо (SAM-234, ADR-026).

Агент открывает место одним вызовом, правит там, коммитит явными путями в ветку `agent/<имя>` и закрывает после
слияния. Основное дерево и ветка dev не меняются: место лежит в `data/worktrees` (периферия, ключ на правку не нужен,
gateway/core_paths.py). Только стандартная библиотека и сам git; запуск git приходит снаружи (Runner), чтобы логику
проверять на временных репозиториях и подменять раннер.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
ROOT_ENV = "SAM_WORKSPACES_DIR"
DEFAULT_ROOT = "data/worktrees"          # от корня репозитория, в периферии SAM_PERIPHERY_DIRS
BASE_ENV = "SAM_WORKSPACE_BASE"
DEFAULT_BASE = "dev"
BRANCH_PREFIX = "agent/"
SLUG_MAX = 40
GIT_TIMEOUT_S = 60
SLUG_BAD = re.compile(r"[^a-z0-9._-]+")
COAUTHOR = re.compile(r"^[ \t]*co-authored-by:.*(?:\r?\n|$)", re.IGNORECASE | re.MULTILINE)   # skill git-style: без соавторства

Runner = Callable[[list[str], Path], tuple[int, str]]   # (аргументы git, каталог) -> (код выхода, вывод)

ACTIONS = ("open", "status", "diff_stat", "commit", "close")


def run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GIT_TIMEOUT_S)
    return done.returncode, (done.stdout + done.stderr).strip()


def slugify(name: str) -> str:
    slug = SLUG_BAD.sub("-", str(name or "").strip().lower()).strip("-.")[:SLUG_MAX].strip("-.")
    if not slug:
        raise ValueError("имя рабочего места - латиница, цифры и дефис, например monitor-badge")
    return slug


def workspaces_root(repo: Path = REPO, environ=None) -> Path:
    raw = (environ if environ is not None else os.environ).get(ROOT_ENV) or DEFAULT_ROOT
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _base(environ=None) -> str:
    return (environ if environ is not None else os.environ).get(BASE_ENV) or DEFAULT_BASE


def inside(workspace: Path, relative: str) -> bool:
    """Путь относительный и после разбора остаётся внутри рабочего места; ключи git (-x) не пропускаются."""
    if not relative or relative.startswith("-") or Path(relative).is_absolute():
        return False
    return (workspace / relative).resolve().is_relative_to(workspace.resolve())


def clean_message(message: str) -> str:
    return COAUTHOR.sub("", str(message or "")).strip()


def open_workspace(name: str, repo: Path, root: Path, base: str, run: Runner) -> str:
    slug = slugify(name)
    path, branch = root / slug, BRANCH_PREFIX + slug
    if path.exists():
        code, out = run(["rev-parse", "--abbrev-ref", "HEAD"], path)
        if code != 0:
            return f"Каталог {path} занят и не рабочее место git: {out or 'не git'}"
        return f"Рабочее место уже открыто: {path}, ветка {out}"
    exists = run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)[0] == 0
    args = ["worktree", "add", str(path), branch] if exists else ["worktree", "add", "-b", branch, str(path), base]
    code, out = run(args, repo)
    if code != 0:
        return f"Рабочее место не открыто: {out}"
    return f"Рабочее место открыто: {path}, ветка {branch} от {base}" if not exists else \
        f"Рабочее место открыто: {path}, ветка {branch} продолжена"


def _workspace(name: str, root: Path) -> Path | str:
    try:
        path = root / slugify(name)
    except ValueError as e:
        return str(e)
    return path if path.exists() else f"Рабочего места {path.name} нет: открой его действием open"


def commit_workspace(path: Path, paths: list, message: str, run: Runner) -> str:
    text = clean_message(message)
    if not text:
        return "Нужно сообщение коммита: что изменено в итоге, без строк соавторства"
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and inside(path, p) for p in paths):
        return "paths - список путей внутри рабочего места, относительных, без .. и без ключей git"
    for args in (["add", "--", *paths], ["commit", "-m", text]):
        code, out = run(args, path)
        if code != 0:
            return f"Коммит не сделан: {out}"
    return f"Коммит {run(['rev-parse', '--short', 'HEAD'], path)[1]} в ветке {run(['rev-parse', '--abbrev-ref', 'HEAD'], path)[1]}"


def close_workspace(path: Path, branch: str, repo: Path, run: Runner) -> str:
    code, out = run(["worktree", "remove", "--force", str(path)], repo)
    if code != 0:
        return f"Рабочее место не закрыто: {out}"
    code, out = run(["branch", "-d", branch], repo)   # -d, не -D: несмещённую ветку git не удаляет
    return f"Рабочее место закрыто, ветка {branch} удалена" if code == 0 else \
        f"Рабочее место закрыто, ветка {branch} осталась: не слита в {_base()}"


def workspace_impl(params: dict, run: Runner = run_git, repo: Path = REPO, root: Path | None = None,
                   base: str | None = None) -> str:
    """action: open | status | diff_stat | commit | close; name - имя места; commit: paths и message."""
    action = str(params.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return f"action - один из: {', '.join(ACTIONS)}"
    root = root or workspaces_root(repo)
    base = base or _base()
    if action == "open":
        try:
            return open_workspace(params.get("name"), repo, root, base, run)
        except ValueError as e:
            return str(e)
    path = _workspace(params.get("name"), root)
    if isinstance(path, str):
        return path
    if action == "status":
        code, out = run(["status", "--short", "--branch"], path)
        return out or "Изменений нет"
    if action == "diff_stat":
        code, out = run(["diff", "--stat", f"{base}...HEAD"], path)
        return out if code == 0 and out else (f"Расхождений с {base} нет" if code == 0 else f"diff не получен: {out}")
    if action == "commit":
        return commit_workspace(path, params.get("paths"), params.get("message"), run)
    return close_workspace(path, BRANCH_PREFIX + path.name, repo, run)
