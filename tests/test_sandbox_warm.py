"""Быстрая песочница (ADR-016 «Быстрая проверка»): тёплый контейнер сессии и исполнитель по каналу кадров.

Пул и канал проверяются без Docker: канал открывает поддельный исполнитель (tests/fakes_warm_executor.py) локальным
процессом вместо `docker run -i`. Настоящий исполнитель (fork, prctl, передача дескрипторов) - Linux; изоляция,
замер, зависание и попытки подделать ответ - на живом Docker под меткой integration (`pytest -m integration`), без
Docker или образа живой тест пропускается с причиной.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway import sandbox, sandbox_repl, sandbox_warm
from gateway.sandbox import Owner

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="bridge", memory="512m", cpus="1", pids="128")
VOICE, OTHER, WORKER = Owner("voice"), Owner("chat-2"), Owner("voice", "ab12")
HANG_TIMEOUT_S = 1
BROKEN_WAIT_S, POLL_S = 5, 0.01     # поток чтения видит лишний кадр за миллисекунды
FAKE = Path(__file__).with_name("fakes_warm_executor.py")


def local_channel(*args, argv=None):
    """Канал к исполнителю локальным процессом; docker kill - убийство процесса."""
    opened = []

    def open_channel(name, base, limits):
        holder = {}
        channel = sandbox_warm.Channel(argv or [sys.executable, "-u", str(FAKE), *args],
                                       kill=lambda: holder["channel"].proc.kill())
        holder["channel"] = channel
        opened.append((name, base, limits))
        return channel
    open_channel.opened = opened
    return open_channel


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make_pool(tmp_path):
    pools = []

    def make(**kw):
        pool = sandbox_warm.WarmPool(root=tmp_path, limits=LIMITS, open_channel=kw.pop("open_channel", local_channel()),
                                     **kw)
        pools.append(pool)
        return pool
    yield make
    for pool in pools:
        pool.stop_all("конец теста")


# ─── решения ──────────────────────────────────────────────────────────────────

def test_warm_container_has_the_sandbox_isolation_and_no_init(tmp_path):
    argv = sandbox_warm.warm_argv("sam-warm-x", tmp_path, LIMITS, "print(1)")
    joined = " ".join(argv)
    for flag in ("run -i --rm", "--network bridge", "--memory 512m", "--pids-limit 128", "--read-only",
                 "--tmpfs /tmp", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 1000:1000",
                 "--pull=never"):
        assert flag in joined
    assert argv.count("-v") == 1 and argv[argv.index("-v") + 1] == f"{tmp_path}:/work"
    # и форма --env=X; tini (--init) держал бы канал
    assert not any(a.split("=")[0] in ("-e", "--env", "--env-file", "--privileged", "--init") for a in argv)
    assert argv[-4:] == ["python", "-u", "-c", "print(1)"]


def test_pool_never_gives_the_warm_container_a_network(make_pool):
    opener = local_channel()
    pool = make_pool(open_channel=opener)
    assert pool.exec(VOICE, {"code": "1"}).code == 0
    assert opener.opened[0][2].network == "none"            # SAM_SANDBOX_NETWORK=bridge не доходит


def test_victim_is_the_oldest_free_and_idle_needs_no_users():
    old, new, busy = (sandbox_warm.Warm(s, s, t) for s, t in (("a", 10), ("b", 20), ("c", 1)))
    busy.users = 1
    assert sandbox_warm.victim([old, new, busy]) is old
    assert sandbox_warm.victim([busy]) is None
    assert sandbox_warm.idle_ones([old, new, busy], now=615, idle_s=600) == [old]


@pytest.mark.parametrize("value, expected", [(None, 30), ("x", 30), (-5, 30), (5, 5), (10_000, 300)])
def test_exec_timeout_is_bounded(monkeypatch, value, expected):
    monkeypatch.delenv(sandbox_warm.TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(sandbox_warm.MAX_TIMEOUT_ENV, raising=False)
    assert sandbox_warm.exec_timeout(value) == expected


@pytest.mark.parametrize("by, asked, expected", [
    ("ab12", None, sandbox_warm.Budget(30)),          # воркер: таймаут кода, замок и старт отдельно
    ("", None, sandbox_warm.Budget(10, 10)),          # основной агент: весь вызов не дольше 10 с
    ("", 5, sandbox_warm.Budget(5, 5)),
    ("", 11, None),
], ids=["worker", "main_default", "main_short", "main_too_long"])
def test_main_agent_waits_at_most_its_ceiling(monkeypatch, by, asked, expected):
    for name in (sandbox_warm.TIMEOUT_ENV, sandbox_warm.MAX_TIMEOUT_ENV, sandbox_warm.MAIN_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    plan, refusal = sandbox_warm.budget(by, asked)
    assert plan == expected
    assert (refusal is None) == (expected is not None)
    if refusal:
        assert "дольше 10 с" in refusal.lower() and "воркеру" in refusal and "\n" not in refusal


def test_long_output_is_named_as_cut():
    text = sandbox_warm.result_text({"code": 0, "out": "x" * 10, "truncated": True, "ms": 1.25})
    assert text.startswith("Код выхода 0 за 1.2 мс") and "последние" in text
    assert sandbox_warm.result_text({"code": 0, "out": "", "ms": 1}).endswith("Вывода нет")


@pytest.mark.parametrize("params, expected", [
    ({"code": "1", "lang": "ruby"}, "lang - python, shell, node, sql"),
    ({"code": "  "}, "Нужен code"),
    ({"code": "x" * (sandbox.FILE_MAX_BYTES + 1)}, "sandbox_run"),
    ({"code": "1", "timeout": 60}, "воркеру"),
], ids=["lang", "empty", "too_big", "main_too_long"])
def test_bad_request_is_one_line_and_starts_nothing(make_pool, params, expected):
    opener = local_channel()
    out = make_pool(open_channel=opener).exec(VOICE, params)
    assert expected in out.text and "\n" not in out.text
    assert opener.opened == []


# ─── исполнитель: выполнение кода (то, что не требует Linux) ──────────────────

def test_python_keeps_its_namespace_and_prints_the_last_expression(capsys):
    state = sandbox_repl.State()
    assert sandbox_repl.execute("python", "x = 41", state) == 0
    assert sandbox_repl.execute("python", "x + 1", state) == 0
    assert capsys.readouterr().out == "42\n"
    assert sandbox_repl.execute("python", "raise SystemExit(3)", state) == 3
    assert sandbox_repl.execute("python", "1/0", state) == sandbox_repl.ERROR_CODE
    err = capsys.readouterr().err
    assert "ZeroDivisionError" in err and "sandbox_repl" not in err      # кадров исполнителя в трассировке нет


def test_code_sees_its_namespace_as_main_not_the_executor():
    state = sandbox_repl.State()
    sandbox_repl.execute("python", "import sys\nm = sys.modules.get('__main__')", state)
    assert state.module.__name__ == "__main__" and not hasattr(state.module, "Executor")
    assert not hasattr(state.module, "serve_code")


# ─── пул и канал на поддельном исполнителе ────────────────────────────────────

def test_state_lives_between_runs(make_pool):
    pool = make_pool()
    assert pool.exec(VOICE, {"code": "set x=41"}).text.endswith("Вывода нет")
    out = pool.exec(VOICE, {"code": "get x"})
    assert out.code == 0 and out.text.endswith("41")
    assert pool.live() == 1


def test_hang_kills_only_its_container_and_next_call_works(make_pool):
    pool = make_pool()
    pool.exec(VOICE, {"code": "set x=1"})
    pool.exec(OTHER, {"code": "set y=2"})
    hung = pool.exec(VOICE, {"code": "hang", "timeout": HANG_TIMEOUT_S})
    assert hung.code is None and f"Не уложилось в {HANG_TIMEOUT_S} с" in hung.text
    assert pool.exec(OTHER, {"code": "get y"}).text.endswith("2")      # чужой контейнер жив со своим состоянием
    again = pool.exec(VOICE, {"code": "get x"})
    assert "прежнее состояние потеряно" in again.text and again.text.endswith("NameError")
    assert "потеряно" not in pool.exec(VOICE, {"code": "1"}).text       # сказано один раз


def test_main_agent_call_stops_at_its_ceiling_with_the_lock_and_start_inside(make_pool, monkeypatch):
    monkeypatch.setenv(sandbox_warm.MAIN_TIMEOUT_ENV, "1")
    hung = make_pool().exec(VOICE, {"code": "hang"})
    assert "Не уложилось в 1 с" in hung.text and "воркеру" in hung.text
    slow = make_pool(open_channel=local_channel("--slow-start", "3")).exec(VOICE, {"code": "1"})
    assert "не поднялся за 1 с" in slow.text and "воркеру" in slow.text and "\n" not in slow.text


def test_frame_with_a_foreign_nonce_is_not_taken(make_pool):
    pool = make_pool()
    forged = pool.exec(VOICE, {"code": "forge"})
    assert forged.code is None and "не на этот запуск" in forged.text
    assert pool.exec(VOICE, {"code": "ok"}).text.endswith("ok")       # новый контейнер


def test_frame_without_a_request_breaks_the_channel_and_the_sweep_stops_it(make_pool):
    pool = make_pool()
    assert pool.exec(WORKER, {"code": "flood"}).code == 0
    channel = next(iter(pool._warm.values())).channel
    deadline = time.monotonic() + BROKEN_WAIT_S
    while not channel.broken and time.monotonic() < deadline:
        time.sleep(POLL_S)
    assert channel.broken and channel.frames.qsize() <= sandbox_warm.FRAMES_QUEUED
    assert pool.sweep() == 1 and pool.live() == 0
    assert "кадр без запроса" in pool.exec(WORKER, {"code": "1"}).text


def test_executor_that_does_not_read_stdin_times_out_the_send(make_pool):
    pool = make_pool(open_channel=local_channel("--deaf"))
    big = "x" * (sandbox.FILE_MAX_BYTES - 1)        # больше буфера канала: запись встаёт
    out = pool.exec(WORKER, {"code": big, "timeout": HANG_TIMEOUT_S})
    assert out.code is None and f"Не уложилось в {HANG_TIMEOUT_S} с" in out.text
    assert pool.live() == 0


def test_idle_container_is_stopped_by_the_sweep(make_pool, monkeypatch):
    monkeypatch.setenv(sandbox_warm.IDLE_ENV, "600")
    clock = Clock()
    pool = make_pool(clock=clock)
    pool.exec(VOICE, {"code": "set x=1"})
    clock.now += 599
    assert pool.sweep() == 0 and pool.live() == 1
    clock.now += 1
    assert pool.sweep() == 1 and pool.live() == 0
    again = pool.exec(VOICE, {"code": "get x"})
    assert "простой дольше 600 с" in again.text and again.text.endswith("NameError")


def test_other_session_never_gets_this_container(make_pool, tmp_path):
    pool = make_pool()
    first = pool.exec(VOICE, {"code": "set secret=voice"})
    other = pool.exec(OTHER, {"code": "get secret"})
    assert other.text.endswith("NameError") and other.warm_id != first.warm_id
    owners = {json.loads((tmp_path / f"{o.warm_id}{sandbox.OWNER_SUFFIX}").read_text(encoding="utf-8"))["session"]
              for o in (first, other)}
    assert owners == {"voice", "chat-2"}
    assert not sandbox.JOB_ID.match(first.warm_id)       # не задача: sandbox_run и показ папку не берут


def test_warm_folder_is_not_a_job_for_run_show_or_app(make_pool, tmp_path):
    """sandbox 72: папка тёплого контейнера сессии - не задача: sandbox_run не запускается в ней, показ файла и
    приложение из неё (from_job) отказывают, хотя папка, файл и сессия на месте."""
    from gateway import apps
    from gateway.tools.coding_agents import CliResult
    warm_id = make_pool().exec(VOICE, {"code": "set x=1"}).warm_id
    (tmp_path / warm_id / "index.html").write_text("<p>warm</p>", encoding="utf-8")
    launched = []
    run = sandbox.run({"job": warm_id, "command": "cat index.html"}, root=tmp_path, limits=LIMITS, owner=VOICE,
                      runner=lambda *a, **k: launched.append(a) or CliResult(0, "warm", ""))
    assert "не из песочницы" in run.text and launched == []
    assert sandbox.served_file(tmp_path, warm_id, "index.html") is None
    app_root = tmp_path / "apps"
    made, error = apps.create(app_root, "warm", "python -m http.server 8000", {}, limits=LIMITS,
                              run=lambda *a, **k: launched.append(a), from_job=warm_id, sandbox_root=tmp_path,
                              owner=VOICE)
    assert made is None and "нет" in error and launched == [] and list(app_root.iterdir()) == []


def test_ceiling_evicts_the_oldest_free_container(make_pool, monkeypatch):
    monkeypatch.setenv(sandbox_warm.MAX_ENV, "1")
    clock = Clock()
    pool = make_pool(clock=clock)
    pool.exec(VOICE, {"code": "set x=1"})
    clock.now += 1
    pool.exec(OTHER, {"code": "set y=1"})
    assert pool.live() == 1
    assert "вытеснен" in pool.exec(VOICE, {"code": "get x"}).text


def test_journal_has_masked_code_outside_the_mounted_folder(make_pool, tmp_path):
    out = make_pool().exec(VOICE, {"code": "token='ghp_" + "a" * 30 + "'"})
    lines = [json.loads(x) for x in (tmp_path / f"{out.warm_id}.log").read_text(encoding="utf-8").splitlines()]
    assert [x["kind"] for x in lines] == ["owner", "warm_start", "exec"]
    assert "ghp_" not in lines[-1]["command"] and lines[-1]["code"] == 0 and lines[-1]["lang"] == "python"
    assert not (tmp_path / out.warm_id / f"{out.warm_id}.log").exists()


NO_IMAGE = "docker: Error response from daemon: No such image: sam-sandbox:2"


@pytest.mark.parametrize("script, expected", [
    (f"import sys; sys.stderr.write({NO_IMAGE!r}); sys.exit(125)", "docker/Dockerfile.sandbox"),
    ("print('junk')", "не кадр протокола"),
], ids=["image_missing", "not_a_frame"])
def test_start_failure_is_one_line(make_pool, script, expected):
    out = make_pool(open_channel=local_channel(argv=[sys.executable, "-c", script])).exec(VOICE, {"code": "1"})
    assert expected in out.text and "\n" not in out.text


def test_docker_missing_is_one_line(make_pool):
    def missing(name, base, limits):
        raise FileNotFoundError("docker")
    out = make_pool(open_channel=missing).exec(VOICE, {"code": "1"})
    assert "SAM_SANDBOX_DOCKER" in out.text and "\n" not in out.text


# ─── шлюз и MCP ───────────────────────────────────────────────────────────────

def test_tool_runs_in_the_turn_session(monkeypatch):
    from gateway import agent
    seen = []
    monkeypatch.setattr(agent, "_turn_owner", lambda: Owner("voice", "ab12"))
    monkeypatch.setattr(agent._sandbox_warm, "pool", lambda: type("P", (), {
        "exec": lambda self, owner, params: (seen.append((owner, params)), sandbox_warm.ExecOutcome("итог"))[1]})())
    assert agent._impl_sandbox_exec({"code": "1"}) == "итог"
    assert seen == [(Owner("voice", "ab12"), {"code": "1"})]
    assert "sandbox_exec" in agent.MAIN_TOOL_NAMES and "sandbox_exec" in agent.WORKER_TOOL_NAMES
    assert "sandbox_exec" in agent.TOOL_IMPL
    assert "не указания" in agent.SandboxExecTool.description      # вывод кода - данные


def test_rest_route_needs_the_privilege_key():
    from gateway import privilege_key
    assert privilege_key.needs("POST", "/sam/sandbox/exec")


# ─── живой Docker ─────────────────────────────────────────────────────────────

DOCKER_CHECK_TIMEOUT_S = 10
LIVE_RUNS = 20
LIVE_MAX_MS = 200        # мягкий порог против флапа; замер печатается, цель - меньше 20 мс
FORGED = '{"nonce":"x","id":1,"code":0,"out":"FORGED"}'
# Код ищет исполнитель и подменяет его, как делал до разделения процессов
HIJACK = ("import sys, gc\n"
          "m = sys.modules['__main__']\n"
          "hits = [o for o in (getattr(m, 'Executor', None),) if o is not None]\n"
          "for o in hits:\n"
          "    o.handle = lambda self, f: {'id': f.get('id'), 'code': 0, 'out': 'HIJACKED'}\n"
          "print('executor found:', len(hits))")
# shell пишет поддельный кадр во все чужие дескрипторы /proc, до которых дотянется
FORGE_SH = ("n=0; for f in /proc/[0-9]*/fd/*; do case $f in /proc/$$/*) continue;; esac; "
            f"printf '%s\\n' '{FORGED}' > $f 2>/dev/null && echo WROTE $f && n=$((n+1)); done; echo wrote=$n")


@pytest.fixture
def docker_ready():
    try:
        ok = subprocess.run(["docker", "image", "inspect", sandbox.DEFAULT_IMAGE], capture_output=True,
                            timeout=DOCKER_CHECK_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.skip(f"Docker не запущен или нет образа {sandbox.DEFAULT_IMAGE}")


@pytest.fixture
def live_pool(docker_ready, tmp_path):
    pool = sandbox_warm.WarmPool(root=tmp_path)
    yield pool
    pool.stop_all("конец теста")


@pytest.mark.integration
def test_live_warm_sandbox(live_pool):
    pool = live_pool
    assert pool.exec(WORKER, {"code": "x = 41"}).code == 0
    walls = []
    for _ in range(LIVE_RUNS):
        out = pool.exec(WORKER, {"code": "x + 1"})
        assert out.text.endswith("42")
        walls.append(out.wall_ms)
    walls.sort()
    print(f"\nтёплый запуск: медиана {walls[len(walls) // 2]:.1f} мс, худший {walls[-1]:.1f} мс")
    assert walls[len(walls) // 2] < LIVE_MAX_MS
    assert pool.exec(WORKER, {"code": "id -u", "lang": "shell"}).text.splitlines()[-1] == "1000"
    net = pool.exec(WORKER, {"code": "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 80), 3)\n"
                                     "    print('NET')\nexcept OSError as e:\n    print('NONET')"})
    assert net.text.endswith("NONET")
    assert pool.exec(WORKER, {"code": "console.log(1 + 2)", "lang": "node"}).text.endswith("3")
    assert "│" in pool.exec(WORKER, {"code": "create table t as select 1 a; select * from t", "lang": "sql"}).text
    old = next(w.name for w in pool._warm.values())
    hung = pool.exec(WORKER, {"code": "while True: pass", "timeout": HANG_TIMEOUT_S})
    assert "Не уложилось" in hung.text
    running = subprocess.run(["docker", "ps", "-q", "--filter", f"name={old}"], capture_output=True, text=True)
    assert running.stdout.strip() == ""
    again = pool.exec(WORKER, {"code": "x"})
    assert "прежнее состояние потеряно" in again.text and "NameError" in again.text


@pytest.mark.integration
def test_live_code_cannot_forge_the_answer(live_pool):
    pool = live_pool
    hijack = pool.exec(WORKER, {"code": HIJACK})
    assert hijack.text.endswith("executor found: 0")
    assert pool.exec(WORKER, {"code": "print(2+2)"}).text.endswith("4")
    assert pool.exec(WORKER, {"code": "echo hi", "lang": "shell"}).text.endswith("hi")
    forge = pool.exec(WORKER, {"code": FORGE_SH, "lang": "shell"})
    assert forge.code == 0 and forge.text.endswith("wrote=0"), forge.text      # /proc/1 и процесс кода закрыты
    for lang, code, expected in (("python", "print(2+2)", "4"), ("shell", "echo hi", "hi")):
        out = pool.exec(WORKER, {"code": code, "lang": lang})
        assert out.text.endswith(expected) and "FORGED" not in out.text
    node = pool.exec(WORKER, {"lang": "node", "code": (
        "const fs=require('fs');let n=0;for(const p of ['/proc/1/fd/1','/proc/1/fd/0']){try{fs.writeFileSync(p,"
        + json.dumps(FORGED + "\n") + ");n++}catch(e){}}console.log('wrote='+n)")})
    assert node.text.endswith("wrote=0")
    assert pool.exec(WORKER, {"code": "print(2+2)"}).text.endswith("4")


@pytest.mark.integration
def test_live_background_output_stays_out_of_the_next_run(live_pool):
    pool = live_pool
    pool.exec(WORKER, {"code": "(while true; do echo BG; sleep 0.05; done) & echo started", "lang": "shell"})
    out = pool.exec(WORKER, {"code": "import time; time.sleep(0.5); print('own')"})
    assert out.text.endswith("own") and "BG" not in out.text


def test_worker_shares_the_warm_container_of_its_creator_session(make_pool, monkeypatch):
    """sandbox 54: воркер, созданный в сессии voice, работает в тёплом контейнере этой сессии - видит состояние,
    оставленное основным ходом; другая сессия - нет. Настоящие _turn_owner и _impl_sandbox_exec шлюза и пул на
    поддельном исполнителе."""
    from gateway import agent, token_usage
    from gateway.worker import worker_session_var
    pool = make_pool()
    monkeypatch.setattr(agent._sandbox_warm, "pool", lambda: pool)
    monkeypatch.setitem(agent._task_context_map, "task:ab12", ("voice", "ab12"))

    with token_usage.usage_scope("voice", "main"):
        agent._impl_sandbox_exec({"code": "set plan=график"})
    token = worker_session_var.set("task:ab12")
    try:
        with token_usage.usage_scope("task:ab12", "worker"):
            assert agent._turn_owner() == Owner("voice", "ab12")
            assert agent._impl_sandbox_exec({"code": "get plan"}).endswith("график")
    finally:
        worker_session_var.reset(token)
    with token_usage.usage_scope("chat-2", "main"):
        assert agent._impl_sandbox_exec({"code": "get plan"}).endswith("NameError")


def test_gateway_shutdown_stops_every_warm_container(make_pool, monkeypatch):
    """sandbox 69: остановка шлюза гасит все тёплые контейнеры: код сессий не живёт после шлюза."""
    import asyncio

    from gateway import main
    pool = make_pool()
    pool.exec(VOICE, {"code": "1"})
    pool.exec(OTHER, {"code": "1"})
    procs = [w.channel.proc for w in pool._warm.values()]
    monkeypatch.setattr(sandbox_warm, "pool", lambda: pool)
    asyncio.run(main._stop_warm_sandboxes())

    assert pool._warm == {}
    for proc in procs:
        proc.wait(timeout=BROKEN_WAIT_S)


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def test_warm_stop_is_journaled_with_its_reason(make_pool, tmp_path):
    """sandbox 70: журнал тёплой рядом с папкой: warm_start, exec и warm_stop с причиной (зависание, остановка
    шлюза)."""
    pool = make_pool()
    out = pool.exec(VOICE, {"code": "hang", "timeout": HANG_TIMEOUT_S})
    pool.exec(VOICE, {"code": "1"})
    pool.stop_all("шлюз остановлен")
    lines = [json.loads(x) for x in (tmp_path / f"{out.warm_id}.log").read_text(encoding="utf-8").splitlines()]
    stops = [x["reason"] for x in lines if x["kind"] == sandbox_warm.KIND_STOP]
    assert stops == [f"запуск не уложился в {HANG_TIMEOUT_S} с", "шлюз остановлен"]
    assert [x["kind"] for x in lines].count(sandbox_warm.KIND_START) == 2


def test_warm_folder_outlives_its_container_while_the_gateway_lives(make_pool):
    """sandbox 71: папка warm-<id> переживает перезапуск контейнера: новый контейнер сессии получает ту же папку
    с файлами, теряются только переменные."""
    opener = local_channel()
    pool = make_pool(open_channel=opener)
    first = pool.exec(VOICE, {"code": "set x=1"})
    base = opener.opened[0][1]
    (base / "report.csv").write_text("a,b\n", encoding="utf-8")
    pool.exec(VOICE, {"code": "hang", "timeout": HANG_TIMEOUT_S})
    again = pool.exec(VOICE, {"code": "get x"})
    assert len(opener.opened) == 2 and opener.opened[1][1] == base and base.name == first.warm_id
    assert (base / "report.csv").read_text(encoding="utf-8") == "a,b\n" and "файлы в /work на месте" in again.text


@pytest.mark.skipif(not hasattr(__import__("os"), "pread"), reason="исполнитель тёплой - Linux (pread); "
                    "проверено в контейнере python:3.12-slim")
@pytest.mark.parametrize("size, truncated", [(10_000, True), (100, False)], ids=["long", "short"])
def test_executor_cuts_the_output_to_its_limit(tmp_path, size, truncated):
    """sandbox 62: исполнитель отдаёт последние limit знаков вывода и признак обрезки; короткий вывод - целиком."""
    import os
    path = tmp_path / "out"
    text = "".join(str(i % 10) for i in range(size - 1)) + "Я"
    path.write_text(text, encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    try:
        out, cut = sandbox_repl.tail(fd, sandbox.OUTPUT_MAX_CHARS)
    finally:
        os.close(fd)
    assert out == text[-sandbox.OUTPUT_MAX_CHARS:] and cut is truncated
