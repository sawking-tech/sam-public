"""Дерево процессов: запуск в своей группе и убийство вместе с детьми.

Команда через оболочку или CLI агента запускает дочерние процессы (shell, тесты, node); по сроку они не должны
пережить родителя и держать его каналы. На Windows дерево снимает `taskkill /T`, на POSIX - сигнал всей группе,
поэтому процесс стартует в своей сессии.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

# Аргументы запуска для subprocess.Popen и asyncio.create_subprocess_*: своя группа процессов на POSIX
NEW_GROUP: dict = {} if sys.platform == "win32" else {"start_new_session": True}


def kill_tree(pid: int) -> None:
    """Процесс и все его дети; уже завершённый - не ошибка."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
