"""Ядро SAM и периферия (SAM-194): правка ядра - только с ключом команд, периферию SAM правит сама.

Ядро - весь репозиторий SAM, кроме папок периферии: там код шлюза, голоса и Cortex, хуки и скрипты ведущей сессии,
`data/secrets` с ключом привилегий. Периферия - папки из SAM_PERIPHERY_DIRS (по умолчанию приложения `data/apps`) и
всё вне репозитория SAM (другие репозитории). Папка агента кода, содержащая репозиторий SAM (папка репозиториев), -
тоже ядро: агент дотянется до него.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PERIPHERY_DIRS_ENV = "SAM_PERIPHERY_DIRS"
DEFAULT_PERIPHERY_DIRS = "data/apps,data/worktrees"   # через запятую, от корня репозитория SAM; радар - после выноса (SAM-198); рабочие места агентов - ADR-026


def periphery_dirs(repo: Path = REPO, environ=None) -> list[Path]:
    """Папки периферии внутри репозитория SAM из SAM_PERIPHERY_DIRS."""
    raw = (environ if environ is not None else os.environ).get(PERIPHERY_DIRS_ENV) or DEFAULT_PERIPHERY_DIRS
    return [(repo / name.strip()).resolve() for name in raw.split(",") if name.strip()]


def touches_core(workdir: Path | None, repo: Path, periphery: list[Path]) -> bool:
    """Работа в этой папке трогает ядро. Папка не определилась - ядро."""
    if workdir is None:
        return True
    path, repo = workdir.resolve(), repo.resolve()
    if any(path == p or path.is_relative_to(p) for p in periphery):
        return False
    return path == repo or path.is_relative_to(repo) or repo.is_relative_to(path)
