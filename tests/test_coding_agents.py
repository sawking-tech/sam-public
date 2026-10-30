"""Агент кода claude_delegate (SAM-40, SAM-60, SAM-164): настоящие claude_code_backend и opencode_backend, выбор
агента, папка, итог одной строкой; подменён только запуск CLI (граница процесса)."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gateway.tools import agent_channel as ch
from gateway.tools import coding_agents as ca
from tests.fakes_agents import CliRunner

CLI_ENV = {"claude": "SAM_CLAUDE_CLI", "opencode": "SAM_OPENCODE_CLI"}
FACTORY = {"claude": ca.claude_code_backend, "opencode": ca.opencode_backend}


@pytest.fixture
def clis(monkeypatch):
    """CLI обоих агентов найдены по окружению; агент, модель и сроки - по умолчанию."""
    for name, env in CLI_ENV.items():
        monkeypatch.setenv(env, name)
    for env in (ca.AGENT_ENV, ca.CLAUDE_MODEL_ENV, ca.TIMEOUT_ENV, ca.MAX_TIMEOUT_ENV):
        monkeypatch.delenv(env, raising=False)


def _registry(available=("claude", "opencode"), result=None):
    runners = {name: CliRunner(result, version_code=0 if name in available else 1) for name in FACTORY}
    return {name: make(runners[name]) for name, make in FACTORY.items()}, runners


def _ran(runners):
    return [name for name, r in runners.items() if r.calls]


# ─── Канал к создателю ────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent, task_id, channel", [
    ("claude", "ab12", True),
    ("claude", "", False),        # без задачи канала нет
    ("opencode", "ab12", False),  # opencode канал не умеет
])
def test_coding_agent_gets_its_channel_only_inside_a_task(clis, monkeypatch, tmp_path, agent, task_id, channel):
    monkeypatch.setenv("SAM_API_KEY", "x")
    monkeypatch.setenv("SAM_COMMAND_SECRET", "y")
    monkeypatch.setenv(ca.GATEWAY_URL_ENV, "http://gw:8000")
    registry, runners = _registry()
    assert ca.delegate_impl({"task": "x", "agent": agent}, registry, roots=[tmp_path], task_id=task_id) == ""
    argv = runners[agent].calls[0][0]
    assert ("--mcp-config" in argv) is channel
    if channel:
        server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"][ch.SERVER_NAME]
        assert server["args"] == [str(ch.SERVER_SCRIPT)] and ch.SERVER_SCRIPT.exists()
        assert server["env"] == {ch.TASK_ENV: task_id, ch.GATEWAY_ENV: "http://gw:8000"}
        assert "--strict-mcp-config" in argv     # только этот канал, без .mcp.json с ключом привилегий
    if agent == "claude":
        env = runners[agent].envs[0]
        assert "SAM_API_KEY" not in env and "SAM_COMMAND_SECRET" not in env and "CLAUDECODE" not in env


# ─── Выбор агента ─────────────────────────────────────────────────────────────

BOTH = ("claude", "opencode")


@pytest.mark.parametrize("task, agent, env_agent, available, expected", [
    ("сделай через OpenCode", None, "", BOTH, "opencode"),
    ("почини тесты с помощью опенкода", None, "", BOTH, "opencode"),
    ("fix it via claude code", None, "opencode", BOTH, "claude"),          # слово в задаче сильнее окружения
    ("через Клода поправь README", None, "", BOTH, "claude"),
    ("поправь CLAUDE.md", None, "opencode", BOTH, "opencode"),             # имя файла - предмет задачи, не выбор
    ("открой opencode.json", None, "", BOTH, "claude"),                    # auto - первый доступный
    ("сделай через OpenCode", "claude", "opencode", BOTH, "claude"),       # явный параметр сильнее всего
    ("x", None, "", ("opencode",), "opencode"),                            # недоступный пропускается
    ("x", None, "", (), "Ни один coding agent не доступен"),
    ("x", "claude", "", ("opencode",), "Coding agent 'claude' не доступен"),
    ("x", "hermes", "", BOTH, "Coding agent 'hermes' не зарегистрирован"),
    ("", None, "", BOTH, "Нужен task"),
], ids=["word_opencode", "word_case_ending", "word_beats_env", "word_claude", "file_name_is_not_a_choice",
        "auto_first", "explicit_wins", "auto_skips_unavailable", "none_available", "explicit_unavailable",
        "unregistered", "no_task"])
def test_delegate_picks_the_agent(clis, monkeypatch, tmp_path, task, agent, env_agent, available, expected):
    monkeypatch.setenv(ca.AGENT_ENV, env_agent)
    registry, runners = _registry(available)
    out = ca.delegate_impl({"task": task, **({"agent": agent} if agent else {})}, registry, roots=[tmp_path])
    if expected in FACTORY:
        assert _ran(runners) == [expected] and runners[expected].calls[0][0][2] == task
    else:
        assert expected in out and "\n" not in out and _ran(runners) == []


@pytest.mark.parametrize("agent, env_value, which, cli", [
    ("claude", None, "C:/bin/claude.exe", "C:/bin/claude.exe"),     # claude.exe через PATH, не claude.cmd
    ("opencode", "D:/tools/opencode.exe", "C:/bin/opencode.exe", "D:/tools/opencode.exe"),   # окружение сильнее
])
def test_cli_comes_from_env_or_path(monkeypatch, tmp_path, agent, env_value, which, cli):
    if env_value:
        monkeypatch.setenv(CLI_ENV[agent], env_value)
    else:
        monkeypatch.delenv(CLI_ENV[agent], raising=False)
    monkeypatch.setattr(ca.shutil, "which", lambda name: which)
    runner = CliRunner()
    ca.delegate_impl({"task": "x", "agent": agent}, {agent: FACTORY[agent](runner)}, roots=[tmp_path])
    assert runner.calls[0][0][0] == cli


# ─── Папка агента ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("workdir, cwd", [
    ("SAM", "repos/SAM"),           # имя репозитория внутри корня
    ("", "repos"),                  # пусто - первый корень
    ("C:/Windows", None), ("/etc", None), ("SAM/../../..", None), ("../outside", None),
    ("nope", "нет"),                # репозитория нет
])
def test_delegate_runs_only_inside_allowed_roots(clis, tmp_path, workdir, cwd):
    root = tmp_path / "repos"
    (root / "SAM").mkdir(parents=True)
    registry, runners = _registry()
    out = ca.delegate_impl({"task": "x", "workdir": workdir}, registry, roots=[root.resolve()])
    if cwd in (None, "нет"):
        assert (cwd or ca.ROOTS_ENV) in out and "\n" not in out and _ran(runners) == []
    else:
        assert runners["claude"].calls[0][1] == str((tmp_path / cwd).resolve())


def test_default_root_is_the_folder_holding_sam(clis, monkeypatch):
    monkeypatch.delenv(ca.ROOTS_ENV, raising=False)
    registry, runners = _registry()
    ca.delegate_impl({"task": "x"}, registry)
    assert runners["claude"].calls[0][1] == str(Path(ca.__file__).resolve().parents[3])


# ─── Итог CLI для модели ─────────────────────────────────────────────────────

@pytest.mark.parametrize("agent, result, expected", [
    ("claude", ca.CliResult(0, json.dumps({"is_error": False, "result": "Поправила опечатку"}), ""), "Поправила опечатку"),
    ("claude", ca.CliResult(0, json.dumps({"is_error": True, "result": "Credit balance too low\nmore"}), ""),
     "Claude Code: Credit balance too low"),
    ("claude", ca.CliResult(1, "", "\nError: not logged in\n    at stack"), "Claude Code: код 1: Error: not logged in"),
    ("claude", ca.CliResult(None, "", "", timed_out=True),
     f"Claude Code: не уложился в 10 с и остановлен; разбейте задачу или поднимите {ca.TIMEOUT_ENV}"),
    ("opencode", ca.CliResult(0, "\x1b[1mГотово\x1b[0m: опечатка исправлена\n", ""), "Готово: опечатка исправлена"),
], ids=["claude_ok", "claude_is_error", "nonzero_exit", "timeout", "opencode_ansi"])
def test_cli_result_is_one_line_for_the_model(clis, tmp_path, agent, result, expected):
    registry, runners = _registry(result=result)
    assert ca.delegate_impl({"task": "поправь опечатку", "agent": agent, "timeout": 10}, registry,
                            roots=[tmp_path]) == expected
    if agent == "opencode":   # `opencode -p` - пароль сервера; задача идёт командой run
        assert runners[agent].calls[0][0][:3] == ["opencode", "run", "поправь опечатку"]


def test_missing_cli_is_one_line_with_what_to_do(monkeypatch, tmp_path):
    monkeypatch.delenv("SAM_CLAUDE_CLI", raising=False)
    monkeypatch.setattr(ca.shutil, "which", lambda name: None)
    out = ca.claude_code_backend(CliRunner()).run("x", str(tmp_path), 10)
    assert out == "Claude Code: CLI claude не найден - установите его или задайте SAM_CLAUDE_CLI"


@pytest.mark.parametrize("env_model, model", [(None, "opus"), ("claude-opus-5-5", "claude-opus-5-5"), ("", None)],
                         ids=["opus_by_default", "from_env", "empty_leaves_cli_default"])
def test_claude_model_comes_from_env(clis, monkeypatch, tmp_path, env_model, model):
    if env_model is not None:
        monkeypatch.setenv(ca.CLAUDE_MODEL_ENV, env_model)
    registry, runners = _registry()
    ca.delegate_impl({"task": "x"}, registry, roots=[tmp_path])
    argv = runners["claude"].calls[0][0]
    assert (argv[argv.index("--model") + 1] if "--model" in argv else None) == model


@pytest.mark.parametrize("asked, env, ceiling, timeout", [
    (None, None, None, ca.DEFAULT_TIMEOUT_S),
    (None, "120", None, 120),
    (60, None, None, 60),
    ("abc", None, None, ca.DEFAULT_TIMEOUT_S), (-5, None, None, ca.DEFAULT_TIMEOUT_S), (0, None, None,
                                                                                        ca.DEFAULT_TIMEOUT_S),
    ([1], None, None, ca.DEFAULT_TIMEOUT_S),
    (99999, None, "600", 600),                        # потолок: «timeout: 99999» не держит CLI сутки
], ids=["default", "env", "param", "garbage_str", "negative", "zero", "list", "ceiling"])
def test_delegate_timeout_from_param_or_env(clis, monkeypatch, tmp_path, asked, env, ceiling, timeout):
    for name, value in ((ca.TIMEOUT_ENV, env), (ca.MAX_TIMEOUT_ENV, ceiling)):
        if value is not None:
            monkeypatch.setenv(name, value)
    registry, runners = _registry()
    ca.delegate_impl({"task": "x", **({"timeout": asked} if asked is not None else {})}, registry, roots=[tmp_path])
    assert runners["claude"].calls[0][2] == timeout


# ─── Настоящий процесс ────────────────────────────────────────────────────────

CHILD_DEATH_WAIT_S = 10   # taskkill /T на Windows и killpg на POSIX - доли секунды; срок с запасом на медленную машину


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, encoding="utf-8",
                             errors="replace").stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _dies(pid: int) -> bool:
    deadline = time.monotonic() + CHILD_DEATH_WAIT_S
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def test_run_cli_returns_output_and_code():
    res = ca.run_cli([sys.executable, "-c", "print('ok')"], None, 30)
    assert res.code == 0 and res.out.strip() == "ok" and not res.timed_out


@pytest.mark.slow
def test_run_cli_timeout_kills_the_tree():
    # Родитель запускает долгоживущего ребёнка, печатает его pid и ждёт; срок должен убить обоих
    child = "import time; time.sleep(60)"
    parent = (f"import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', {child!r}]); "
              "print(p.pid, flush=True); time.sleep(60)")
    res = ca.run_cli([sys.executable, "-c", parent], None, 3)
    assert res.timed_out
    assert _dies(int(res.out.split()[0]))


# ─── Вне event loop ───────────────────────────────────────────────────────────

LOOP_FREE_WAIT_S = 10   # срок, за который корутина рядом обязана успеть; блокированный цикл не успеет никогда


async def test_delegate_does_not_block_the_event_loop(monkeypatch, clis):
    """claude_delegate ждёт CLI минутами; пока он работает, шлюз отвечает монитору и другим сессиям: CLI в потоке
    ждёт отметку от корутины event loop - заблокированный цикл её не поставит."""
    from gateway import agent
    loop_ran = threading.Event()

    class _WaitsForLoop(CliRunner):
        def __call__(self, argv, cwd, timeout, env=None):
            if argv[-1] != "--version" and not loop_ran.wait(LOOP_FREE_WAIT_S):
                return ca.CliResult(1, "", "event loop стоял")
            return super().__call__(argv, cwd, timeout, env)

    monkeypatch.setattr(agent, "_coding_agents_registry", {"claude": ca.claude_code_backend(_WaitsForLoop())})
    monkeypatch.setenv(ca.ROOTS_ENV, str(Path(ca.__file__).resolve().parents[2]))
    call = asyncio.ensure_future(agent.TOOL_IMPL["claude_delegate"]({"task": "x"}))
    await asyncio.sleep(0)
    loop_ran.set()
    assert await call == ""


# ─── SAM-60: итог воркера голосом ─────────────────────────────────────────────

class TestWorkerResultByVoice:
    @pytest.fixture
    def layer(self, monkeypatch):
        """Подключённый голосовой слой: его очередь отправки и сессия, из которой пришла фраза."""
        import asyncio
        from gateway import voice_socket
        outbox = asyncio.Queue()
        monkeypatch.setattr(voice_socket, "_listeners", {outbox})
        monkeypatch.setattr(voice_socket, "_voice_sessions", {"voice"})
        return outbox

    @pytest.fixture
    def history(self, monkeypatch):
        """Что учёт прозвучавшего пишет в историю: события assistant_spoken."""
        from gateway import agent, spoken_history
        written = []
        monkeypatch.setattr(agent, "_spoken_ledger",
                            spoken_history.SpokenLedger(lambda name, data: written.append(data["voice"])))
        return written

    async def test_voice_session_result_goes_to_the_pause_queue(self, layer, history):
        from gateway import agent
        assert agent.speak_worker_result("voice", "Поправила опечатку в README.")
        msg = layer.get_nowait()
        assert msg["type"] == "say" and msg["text"] == "Поправила опечатку в README." and msg["id"]

    async def test_unvoiced_worker_result_is_not_in_history(self, layer, history):
        """SAM-85: итог воркера, сброшенный голосовым слоем, в историю не идёт; прозвучавший - идёт."""
        from gateway import agent
        agent.speak_worker_result("voice", "Тесты зелёные.")
        dropped = layer.get_nowait()["id"]
        assert agent.settle_spoken("voice", [{"text": "Тесты зелёные.", "status": "dropped"}], dropped)
        assert history == []
        agent.speak_worker_result("voice", "Ветка влита.")
        played = layer.get_nowait()["id"]
        agent.settle_spoken("voice", [{"text": "Ветка влита.", "status": "played"}], played)
        assert history == ["Ветка влита."]

    async def test_own_reply_does_not_take_the_report_of_a_turn(self, layer, history):
        """Отчёт об ответе хода (без id) не закрывает свою реплику, ждущую в паузе."""
        from gateway import agent
        agent.speak_worker_result("voice", "Итог воркера.")
        assert not agent.settle_spoken("voice", [{"text": "Ответ хода.", "status": "played"}])
        assert history == []

    async def test_played_workflow_reply_is_in_history_with_cut_mark(self, layer, history, monkeypatch):
        """SAM-85, SAM-96: своя реплика шага workflow (поздний час) ходом модели в шлюзе попадает в историю,
        когда прозвучала; оборванная - с меткой. Вход шага в историю не пишется (_system_call)."""
        from gateway import agent, spoken_history
        runs = []

        async def _run(text, **kw):
            runs.append(kw)
            return "Антон, третий час, пора спать."
        monkeypatch.setattr(agent.sam_agent, "run", _run)
        await agent._workflow_turn("[workflow:late-night/nudge] Своя реплика", "voice")
        assert runs[0]["_system_call"] and not runs[0]["persist_reply"]
        key = layer.get_nowait()["id"]
        agent.settle_spoken("voice", [{"text": "Антон, третий час, пора спать.", "status": "cut"}], key)
        assert history == [f"Антон, третий час, пора спать. {spoken_history.CUT_MARK}"]

    def test_text_session_and_empty_result_stay_silent(self, layer):
        from gateway import agent
        assert not agent.speak_worker_result("chat-web", "Готово.")
        assert not agent.speak_worker_result("voice", "  ")
        assert layer.empty()

    def test_no_voice_layer_is_one_line(self, monkeypatch, capsys):
        from gateway import agent, voice_socket
        monkeypatch.setattr(voice_socket, "_listeners", set())
        monkeypatch.setattr(voice_socket, "_voice_sessions", {"voice"})
        assert not agent.speak_worker_result("voice", "Готово.")
        assert capsys.readouterr().out.count("\n") == 1

    async def test_task_done_reply_reaches_the_voice_layer(self, layer, monkeypatch):
        import asyncio
        from gateway import agent
        from tests.fakes_voice import HANG_GUARD_S

        async def _run(msg, device_id=None, _system_call=False, persist_reply=True):
            assert "[TASK_DONE:ab12]" in msg and device_id == "voice" and _system_call
            assert not persist_reply   # в историю - только прозвучавшим (SAM-85)
            return "Готово, тесты зелёные."
        monkeypatch.setattr(agent.sam_agent, "run", _run)
        agent._auto_respond("ab12", "pytest: 12 passed", "voice", {})
        msg = await asyncio.wait_for(layer.get(), HANG_GUARD_S)   # пересказ идёт фоновой задачей: ждём реплику
        assert msg["type"] == "say" and msg["text"] == "Готово, тесты зелёные."

    async def test_turn_message_marks_its_session_as_voice(self, monkeypatch):
        from types import SimpleNamespace
        from gateway import session_lock, voice_socket
        from tests.fakes_voice import FakeVoiceSocket
        monkeypatch.setattr(voice_socket, "_voice_sessions", set())
        ws = FakeVoiceSocket()

        async def _turn(text, session_id, on_delta=None, on_reset=None, **kw):
            return SimpleNamespace(voice="Привет!", tools=[])
        ws.send({"type": "turn", "session": "kitchen", "text": "привет"})
        ws.close()
        with pytest.raises(ConnectionError):
            await voice_socket.serve_voice_socket(ws, _turn, session_lock.acquire_and_run,
                                                  session_lock.cancel_current_turn)
        assert voice_socket.is_voice_session("kitchen")
