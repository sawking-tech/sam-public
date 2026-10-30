"""Исполнитель быстрой песочницы (ADR-016 «Быстрая проверка»): живёт внутри тёплого контейнера и выполняет код
по кадрам из stdin.

Шлюз запускает его в контейнере `python -u -c <исходник этого файла>` (gateway/sandbox_warm.py), поэтому здесь
только стандартная библиотека Python образа; DuckDB берётся из образа при первом запросе sql. Нужен Linux: fork,
prctl, сокет SOCK_SEQPACKET с передачей дескрипторов.

Протокол со шлюзом - кадры JSON, по одному на строку (json.dumps экранирует переводы строк внутри):
  исполнитель после старта: {"ready": true, "langs": [...]}
  шлюз:                    {"nonce": "<случайный>", "lang": "python", "code": "...", "limit": 4000}
  исполнитель:             {"nonce": "<тот же>", "code": 0, "out": "...", "truncated": false, "ms": 1.2}

Два процесса, чтобы код не подделал ответ шлюзу:
- Родитель - процесс 1 контейнера, держит канал шлюза и сам код не выполняет. Он ставит себе
  prctl(PR_SET_DUMPABLE, 0): /proc/1/fd и ptrace закрыты для кода, хотя uid тот же. Поэтому без `docker --init`:
  tini стал бы процессом 1 с теми же дескрипторами канала и открытым /proc/1/fd (проверено вживую 14 окт - запись
  в /proc/1/fd/1 доходила до шлюза). Процесс 1 подбирает осиротевшие процессы кода (SIGCHLD), иначе зомби съели бы
  лимит процессов. nonce шлюза в процесс кода не попадает, и кадр родитель собирает сам.
- Процесс кода - fork родителя до prctl, с закрытым каналом шлюза; тоже ставит себе PR_SET_DUMPABLE 0, чтобы
  shell и node не писали в его сокет. Python выполняется в нём, в одном пространстве имён на контейнер (модуль
  __main__ - это пространство имён, а не исполнитель): переменные и импорты живут между запусками, последнее
  выражение печатается, как в REPL. shell (sh -c) и node (node -e) - его дочерние процессы, отдельный на запуск.
  sql - DuckDB, одно соединение в памяти на контейнер.

На каждый запуск родитель делает два новых файла в /tmp и сразу удаляет их имена (mkstemp + unlink): код и вывод.
Оба передаются процессу кода дескрипторами по сокету с запросом и случайным токеном запуска; ответ процесса кода -
токен и код выхода, вывод родитель читает из своего дескриптора. Так фоновый процесс прошлого запуска пишет в свой,
уже ничей файл, а не в вывод следующего, а ответы, записанные в сокет заранее, родитель выбрасывает перед запросом.
Граница: Python-код, который сам переписывает своё пространство имён и потоки, может врать о своих следующих
запусках в этой же сессии - как и о своих переменных; кадр шлюзу и чужие сессии ему недоступны.
"""
from __future__ import annotations

import ast
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import types

PYTHON, SHELL, NODE, SQL = "python", "shell", "node", "sql"
LANGS = (PYTHON, SHELL, NODE, SQL)
COMMANDS = {SHELL: ("sh", "-c"), NODE: ("node", "-e")}    # отдельный процесс на запуск
SOURCE_NAME = "<sam>"           # имя кода в трассировке Python
DEFAULT_LIMIT = 4000            # знаков вывода, если шлюз не назвал свой потолок
BYTES_PER_CHAR = 4              # хвост файла вывода читается с запасом на UTF-8
ERROR_CODE = 1                  # код выхода запуска, который упал исключением
MS_PER_S = 1000
PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4     # linux/prctl.h
MSG_MAX_BYTES = 4096            # сообщение сокета процесса кода: запрос - токен и язык, ответ - токен и код выхода
TOKEN_BYTES = 16
FILE_PREFIX = ".sam-"


# ─── выполнение кода (процесс кода) ────────────────────────────────────────────

def run_python(code: str, scope: dict) -> int:
    """Код в общем пространстве имён; последнее выражение печатается через repr, как в REPL."""
    tree = ast.parse(code, SOURCE_NAME, "exec")
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    exec(compile(tree, SOURCE_NAME, "exec"), scope)
    if last is not None:
        value = eval(compile(ast.Expression(last.value), SOURCE_NAME, "eval"), scope)
        if value is not None:
            print(repr(value))
    return 0


def run_command(lang: str, code: str) -> int:
    return subprocess.run([*COMMANDS[lang], code], stdin=subprocess.DEVNULL).returncode


class State:
    """Что живёт между запусками: пространство имён Python (оно же модуль __main__) и соединение DuckDB."""

    def __init__(self):
        self.module = types.ModuleType("__main__")
        self.scope = self.module.__dict__
        self.duck = None

    def run_sql(self, code: str) -> int:
        if self.duck is None:
            import duckdb           # только в образе песочницы
            self.duck = duckdb.connect()
        result = self.duck.sql(code)
        if result is not None:
            print(result)
        return 0


def execute(lang: str, code: str, state: State) -> int:
    """Один запуск; исключение - трассировка в вывод без кадров исполнителя и код ERROR_CODE."""
    try:
        if lang == PYTHON:
            return run_python(code, state.scope)
        if lang == SQL:
            return state.run_sql(code)
        return run_command(lang, code)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else ERROR_CODE)
    except BaseException as e:   # noqa: BLE001 - любой сбой кода - вывод запуска, исполнитель живёт дальше
        traceback.print_exception(type(e), e, own_frames(e.__traceback__))
        return ERROR_CODE


def own_frames(tb):
    """Трассировка с первого кадра кода запуска: кадры исполнителя модели не нужны."""
    head = tb
    while head is not None and head.tb_frame.f_code.co_filename != SOURCE_NAME:
        head = head.tb_next
    return head or tb


def _flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass


def serve_code(sock: socket.socket) -> None:
    """Цикл процесса кода: запрос (токен, язык) с дескрипторами кода и вывода, ответ - токен и код выхода."""
    state = State()
    sys.modules["__main__"] = state.module      # код видит своё пространство имён, а не исполнитель
    null = os.open(os.devnull, os.O_RDWR)
    text = {"encoding": "utf-8", "errors": "replace", "newline": "\n", "buffering": 1, "closefd": False}
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.stdout, sys.stderr = os.fdopen(1, "w", **text), os.fdopen(2, "w", **text)
    while True:
        msg, fds, _, _ = socket.recv_fds(sock, MSG_MAX_BYTES, 2)
        if not msg:
            return
        try:
            request = json.loads(msg)
            code_fd, out_fd = fds
        except (ValueError, TypeError):
            for fd in fds:
                os.close(fd)
            continue
        with os.fdopen(code_fd, "rb") as f:
            code = f.read().decode("utf-8", errors="replace")
        os.dup2(out_fd, 1)
        os.dup2(out_fd, 2)
        os.close(out_fd)
        try:
            exit_code = execute(request.get("lang"), code, state)
        finally:
            _flush()
            os.dup2(null, 1)
            os.dup2(null, 2)
        sock.send(json.dumps({"token": request.get("token"), "code": exit_code}).encode("utf-8"))


# ─── родитель: канал шлюза ─────────────────────────────────────────────────────

def set_undumpable() -> None:
    """PR_SET_DUMPABLE 0: /proc/<pid>/fd, mem и ptrace процесса закрыты для того же uid. Не встало - отказ старта."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 or libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) не встал")


def reap(*_) -> None:
    """Процесс 1 подбирает завершившихся детей и сирот кода."""
    try:
        while os.waitpid(-1, os.WNOHANG)[0] > 0:
            pass
    except ChildProcessError:
        pass


def unnamed_file(data: bytes = b"") -> int:
    """Новый файл в /tmp без имени: дескриптор есть только у тех, кому его передали."""
    fd, path = tempfile.mkstemp(prefix=FILE_PREFIX)
    os.unlink(path)
    if data:
        os.write(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
    return fd


def tail(fd: int, limit: int) -> tuple[str, bool]:
    """Последние limit знаков файла вывода и признак обрезки; pread не двигает общий с кодом указатель."""
    size = os.fstat(fd).st_size
    window = limit * BYTES_PER_CHAR
    start = max(0, size - window)
    text = os.pread(fd, size - start, start).decode("utf-8", errors="replace")
    return text[-limit:], size > window or len(text) > limit


def drain(sock: socket.socket) -> None:
    """Выбросить ответы, записанные в сокет до запроса."""
    sock.setblocking(False)
    try:
        while sock.recv(MSG_MAX_BYTES):
            pass
    except (BlockingIOError, InterruptedError):
        pass
    finally:
        sock.setblocking(True)


def run_one(sock: socket.socket, frame: dict) -> dict | None:
    """Запуск через процесс кода; None - процесс кода ушёл."""
    lang, nonce = frame.get("lang") or PYTHON, frame.get("nonce")
    limit = int(frame.get("limit") or DEFAULT_LIMIT)
    if lang not in LANGS:
        return {"nonce": nonce, "code": None, "out": f"lang - {', '.join(LANGS)}", "truncated": False}
    token = secrets.token_hex(TOKEN_BYTES)
    code_fd = unnamed_file(str(frame.get("code") or "").encode("utf-8"))
    out_fd = unnamed_file()
    try:
        drain(sock)
        socket.send_fds(sock, [json.dumps({"token": token, "lang": lang}).encode("utf-8")], [code_fd, out_fd])
        os.close(code_fd)
        code_fd = -1
        started = time.perf_counter()
        while True:
            msg = sock.recv(MSG_MAX_BYTES)
            if not msg:
                return None
            try:
                answer = json.loads(msg)
            except ValueError:
                continue
            if isinstance(answer, dict) and answer.get("token") == token:
                break
        ms = (time.perf_counter() - started) * MS_PER_S
        exit_code = answer.get("code")
        out, truncated = tail(out_fd, limit)
    finally:
        for fd in (code_fd, out_fd):
            if fd >= 0:
                os.close(fd)
    return {"nonce": nonce, "code": exit_code if isinstance(exit_code, int) else ERROR_CODE, "out": out,
            "truncated": truncated, "ms": round(ms, 2)}


def main() -> None:
    """Fork процесса кода, затем prctl родителя, кадр готовности и цикл канала шлюза."""
    mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    pid = os.fork()
    if pid == 0:                    # процесс кода: канала шлюза у него нет
        mine.close()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.close(null)
        set_undumpable()
        serve_code(theirs)
        os._exit(0)
    theirs.close()
    set_undumpable()
    signal.signal(signal.SIGCHLD, reap)
    inp, out = os.fdopen(0, "rb"), os.fdopen(1, "wb")

    def send(frame: dict) -> None:
        out.write(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()

    send({"ready": True, "langs": list(LANGS)})
    for line in inp:                # конец stdin - шлюз ушёл: процесс 1 выходит, контейнер убирается (--rm)
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        reply = run_one(mine, frame if isinstance(frame, dict) else {})
        if reply is None:           # процесс кода ушёл (выход процесса, память): канал закрывается
            return
        send(reply)


if __name__ == "__main__":
    main()
