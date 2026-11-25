"""Перезапуск компонента из хода (SAM-195): решение по имени, вызов команды подменой, самоперезапуск шлюза после
ходов, закрытие Cortex, ручка /sam/stand/restart и инструмент модели. Настоящая команда - только безвредный python."""
import asyncio
import re
import sys
from pathlib import Path

import pytest

from gateway import agent, restart as rs
from tests.fakes_core import app_client  # noqa: F401  (фикстура)

START_SAM = Path(__file__).resolve().parents[1] / "start_sam.ps1"


@pytest.mark.parametrize("name, env, windows, kind, has", [
    ("mcp", {}, True, rs.COMMAND, 'start_sam.ps1" -Mcp'),
    ("TTS", {}, True, rs.COMMAND, 'start_sam.ps1" -Tts'),               # имя без регистра
    ("radar", {"SAM_RADAR": "1"}, True, rs.COMMAND, "start_radar.ps1"),
    ("radar", {}, True, rs.REFUSED, "нет компонента 'radar'"),
    ("apps", {}, True, rs.REFUSED, "есть: gateway"),                   # страница приложений - часть шлюза (SAM-204)
    ("mcp", {"SAM_RESTART_CMD_MCP": "systemctl restart sam-mcp"}, False, rs.COMMAND, "systemctl restart sam-mcp"),
    ("mcp", {}, False, rs.REFUSED, "перезапуск mcp здесь не настроен: SAM_RESTART_CMD_MCP"),
    ("gateway", {}, True, rs.SELF, ""),                                   # вкладка поднимет
    ("gateway", {"SAM_RESTART_CMD_GATEWAY": "systemctl restart sam"}, False, rs.SELF, "systemctl restart sam"),
    ("gateway", {}, False, rs.REFUSED, "SAM_RESTART_CMD_GATEWAY"),
    ("cortex", {}, False, rs.CLOSE_CORTEX, ""),
    ("monitor", {}, True, rs.REFUSED, "есть: gateway"),
], ids=["mcp", "case", "radar", "radar-off", "apps", "env", "linux-none", "self-tab", "self-cmd", "self-linux-none", "cortex", "unknown"])
def test_plan_by_name_os_and_env(name, env, windows, kind, has):
    p = rs.plan(name, env, windows)
    assert p.kind == kind and has in (p.command if kind != rs.REFUSED else p.reason)
    assert "\n" not in p.reason


def test_every_default_switch_exists_in_start_sam():
    """Ключи по умолчанию есть в param start_sam.ps1: иначе PowerShell откажет на неизвестный параметр."""
    params = set(re.findall(r"\[switch\]\$(\w+)", START_SAM.read_text(encoding="utf-8")))
    assert {sw.lstrip("-") for sw in rs.START_SWITCHES.values()} <= params


class _Rig:
    def __init__(self, turns=0, code=(0, "OK")):
        self.turns, self.code, self.ran, self.exited, self.detached, self.closed, self.t = turns, code, [], 0, [], 0, 0.0
        self.order = []

    async def run(self, command, log_path):
        self.ran.append((command, log_path.name))
        return self.code

    async def close(self, only_dead=False):
        self.closed += 1
        self.only_dead = only_dead
        return 2

    async def sleep(self, s):
        self.t += s
        if self.turns and self.t >= 3:     # ход кончился через 3 с
            self.turns = 0
        await asyncio.sleep(0)

    def exit(self):
        self.order.append(("exit", self.turns, self.t))
        self.exited += 1

    def restarter(self, windows=True, environ=None):
        return rs.Restarter(turns=lambda: self.turns, close_cortex=self.close, run=self.run, detach=self.detached.append,
                            exit_self=self.exit, sleep=self.sleep, clock=lambda: self.t, environ=environ or {},
                            windows=windows, log=lambda m: None)


@pytest.mark.parametrize("code, out", [
    ((0, "Stopped: MCP"), "mcp: перезапущен (Stopped: MCP)"),
    ((1, "Port 3000 is held by node"), "mcp: перезапуск не удался, код 1: Port 3000 is held by node"),
    ((None, ""), "mcp: команда перезапуска идёт дольше"),
], ids=["ok", "failed", "slow"])
async def test_component_command_runs_and_its_outcome_is_one_line(code, out):
    rig = _Rig(code=code)
    result = await rig.restarter().restart("mcp")
    assert result.startswith(out) and "\n" not in result
    assert rig.ran == [(rs.default_command("mcp", True), "restart-mcp.log")] and rig.exited == 0


async def test_refused_name_runs_nothing():
    rig = _Rig()
    assert "SAM_RESTART_CMD_VOICE" in await rig.restarter(windows=False).restart("voice")
    assert rig.ran == [] and rig.exited == 0


async def test_gateway_answers_first_and_exits_only_after_the_running_turn():
    """Ход, позвавший перезапуск шлюза, идёт: инструмент отвечает сразу, выход - после конца ходов и паузы."""
    rig = _Rig(turns=1)
    r = rig.restarter()
    assert "после конца идущих ходов" in await r.restart("gateway")
    assert rig.exited == 0                                   # ответ уже ушёл, процесс жив
    await r.tasks["gateway"]
    assert rig.order == [("exit", 0, rig.t)] and rig.t >= 3 + rs.SELF_GRACE_S
    assert rig.detached == []


async def test_gateway_does_not_wait_turns_forever_and_runs_its_command_when_set():
    rig = _Rig(turns=1)

    async def stuck(s):            # ход не кончается никогда
        rig.t += s
        await asyncio.sleep(0)
    rig.sleep = stuck
    r = rig.restarter(windows=False, environ={"SAM_RESTART_CMD_GATEWAY": "systemctl restart sam"})
    await r.restart("gateway")
    assert "уже идёт" in await r.restart("gateway")          # повтор не ставит второй выход
    await r.tasks["gateway"]
    assert rig.detached == ["systemctl restart sam"] and rig.exited == 0
    assert rs.SELF_MAX_WAIT_S <= rig.t <= rs.SELF_MAX_WAIT_S + rs.SELF_GRACE_S + rs.POLL_S


async def test_cortex_restart_raises_dead_processes_and_force_restarts_all():
    rig = _Rig()
    r = rig.restarter()
    assert "живые и их ходы не трогаются" in await r.restart("cortex")
    await r.tasks["cortex"]
    assert rig.closed == 1 and rig.only_dead                 # надзор: только не живые, сразу
    await r.restart("cortex", force=True)
    await r.tasks["cortex"]
    assert rig.closed == 2 and not rig.only_dead             # после правки кода Cortex: все


async def test_real_command_output_goes_to_a_log_file(tmp_path):
    cmd = f'"{sys.executable}" -c "print(1); print(\'done\')"'
    assert await rs.run_command(cmd, tmp_path / "r.log") == (0, "done")
    slow = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    assert await rs.run_command(slow, tmp_path / "s.log", timeout_s=0.2) == (None, "")


async def test_cortex_restart_closes_and_raises_agents_of_this_gateway_at_once(monkeypatch):
    class _Acp:
        def __init__(self, alive, fails=False):
            self.alive, self.closed, self.started, self.fails = alive, False, False, fails

        async def close(self):
            self.closed = True

        async def start(self):
            if self.fails:
                raise RuntimeError("не поднялся")
            self.started = True
    live, dead, broken = _Acp(True), _Acp(False), _Acp(False, fails=True)
    monkeypatch.setattr(agent, "_cortex_agents", lambda create=False: [live, dead, broken])
    assert await agent._close_cortex() == 1 and live.closed and dead.closed
    assert live.started and dead.started                    # поднимаются сразу, не ждут хода; сбой одного - не сбой всех


def test_tool_is_given_to_main_model_and_workers_without_privilege():
    from gateway import privilege_key
    assert "restart_component" in agent.MAIN_TOOL_NAMES and "restart_component" in agent.WORKER_TOOL_NAMES
    assert "restart_component" in agent.TOOL_IMPL
    assert not privilege_key.needs("POST", "/sam/stand/restart")


@pytest.mark.usefixtures("radar_on")
def test_restart_endpoint_answers_with_the_restarter_line(app_client, monkeypatch):
    client, _ = app_client
    rig = _Rig()
    monkeypatch.setattr(agent, "restarter", rig.restarter(environ={"SAM_RADAR": "1"}))   # выключенного радара нет в names()
    r = client.post("/sam/stand/restart", json={"name": "radar"})
    assert r.status_code == 200 and r.json()["result"].startswith("radar: перезапущен")


@pytest.mark.usefixtures("radar_on")
def test_supervised_names_map_to_restart_names_and_compression_does_not():
    from gateway import stand_checks as sc
    supervised = set(sc.SUPERVISED) | {sc.CORTEX}
    assert {rs.restart_name(n) for n in supervised} == supervised     # у надзора и перезапуска одни имена
    assert rs.restart_name("compress:voice") is None


def test_change_event_names_what_to_restart_but_not_compression_or_gateway():
    from gateway import stand_report as sr
    ev = sr.change_event([sr.Change("voice", "up", "down"), sr.Change("compress:voice", "up", "stalled"),
                          sr.Change("gateway", "up", "x2"), sr.Change("tts", "up", "down")])
    assert ev["restart"] == "voice,tts"


async def test_second_restart_of_the_same_component_waits_for_the_first():
    """Два вызова одного имени разом: команда идёт один раз, второй получает «уже идёт»; другое имя не ждёт."""
    release, ran = asyncio.Event(), []

    async def run(command, log_path):
        ran.append(log_path.name)
        await release.wait()
        return 0, ""
    r = rs.Restarter(turns=lambda: 0, close_cortex=None, run=run, environ={}, windows=True, log=lambda m: None)
    first = asyncio.create_task(r.restart("mcp"))
    await asyncio.sleep(0)
    assert "уже идёт" in await r.restart("mcp")
    other = asyncio.create_task(r.restart("voice"))
    await asyncio.sleep(0)
    release.set()
    assert (await first).startswith("mcp: перезапущен") and (await other).startswith("voice: перезапущен")
    assert ran == ["restart-mcp.log", "restart-voice.log"]
    assert (await r.restart("mcp")).startswith("mcp: перезапущен")   # первый кончился - можно снова


async def test_cortex_closes_after_its_running_turns_and_never_waits_past_the_limit():
    """Ход основной модели при LLM_SOURCE=acp идёт через Cortex и сам зовёт перезапуск: ответ сразу, закрытие - после
    хода; ход, не кончившийся за предел, рвётся."""
    for turn_ends_at, closed_at_least in ((3, 3), (None, rs.CORTEX_MAX_WAIT_S)):
        rig = _Rig()
        turns, closes = [1], []

        async def sleep(s, rig=rig, turns=turns, end=turn_ends_at):
            rig.t += s
            if end is not None and rig.t >= end:
                turns[0] = 0
            await asyncio.sleep(0)

        async def close(only_dead=False, rig=rig, closes=closes):
            closes.append(rig.t)
            return 1
        r = rs.Restarter(turns=lambda: 0, close_cortex=close, cortex_turns=lambda turns=turns: turns[0], sleep=sleep,
                         clock=lambda rig=rig: rig.t, environ={}, windows=True, log=lambda m: None)
        assert "после конца идущих ходов" in await r.restart("cortex", force=True) and closes == []
        assert "уже идёт" in await r.restart("cortex", force=True)
        await r.tasks["cortex"]
        assert len(closes) == 1 and closed_at_least <= closes[0] <= closed_at_least + rs.POLL_S


def test_hard_exit_timer_does_not_hold_the_process(monkeypatch):
    made = []
    monkeypatch.setattr(rs.signal, "raise_signal", lambda sig: None)

    class _Timer:
        def __init__(self, *a, **kw):
            self.daemon = False
            made.append(self)

        def start(self):
            pass
    monkeypatch.setattr(rs.threading, "Timer", _Timer)
    rs.exit_gateway()
    assert made and made[0].daemon


@pytest.mark.parametrize("state, force, runs", [
    ("up", False, False),          # вкладка уже подняла - перезапуск оборвал бы его второй раз
    ("up", True, True),            # после правки кода
    ("down", False, True), ("x2", False, True),
    (None, False, True),           # проверки у компонента нет (client, cosy)
], ids=["already-up", "force", "down", "x2", "no-check"])
async def test_restart_checks_health_first_unless_forced(state, force, runs):
    rig, checked = _Rig(), []

    async def check(name):
        checked.append(name)
        return state
    r = rs.Restarter(turns=lambda: 0, close_cortex=rig.close, check=check, run=rig.run, environ={}, windows=True,
                     log=lambda m: None)
    out = await r.restart("mcp", force)
    assert bool(rig.ran) is runs and ("уже поднялся" in out) is (not runs)
    assert checked == ([] if force else ["mcp"])


def test_shared_check_uses_the_supervisor_rules(monkeypatch):
    from gateway import stand_checks as sc
    monkeypatch.setattr(sc, "python_cmdlines", lambda: ["python mcp/mcp_aggregator.py", "python -m voice.layer.voice",
                                                         "python -m voice.layer.voice"])
    monkeypatch.setattr(sc, "fetch_health", lambda url: {"status": "ok"})
    monkeypatch.setattr(sc, "read_file_health", lambda name, now: "up")
    monkeypatch.setattr(sc, "OFF", set())
    assert sc.check("mcp") == "up" and sc.check("voice") == "x2" and sc.check("client") is None
    monkeypatch.setattr(sc, "python_cmdlines", lambda: None)            # таблица процессов не читается (Linux)
    assert sc.check("tts") == "down" and sc.check("voice") == "up"      # у синтеза других проверок нет


async def test_gateway_checks_cortex_by_its_own_processes_and_parses_force(monkeypatch):
    class _Acp:
        def __init__(self, exited):
            self.exited = exited
    monkeypatch.setattr(agent, "_cortex_agents", lambda: [_Acp(True)])
    assert await agent._component_state("cortex") == "down"
    monkeypatch.setattr(agent, "_cortex_agents", lambda: [])
    assert await agent._component_state("cortex") == "up"
    got = []

    async def restart(name, force=False):
        got.append((name, force))
        return "ok"
    monkeypatch.setattr(agent.restarter, "restart", restart)
    for raw in (True, "true", "1", None, "нет"):
        await agent._impl_restart_component({"name": "mcp", "force": raw})
    assert [f for _, f in got] == [True, True, True, False, False]


async def test_parallel_restarts_with_a_slow_check_run_the_command_once():
    """Ревью 1a540bc: проверка health идёт секунду - второй вызов того же имени в это время не должен её обогнать."""
    rig = _Rig()

    async def slow_check(name):
        await asyncio.sleep(0.05)
        return "down"
    r = rs.Restarter(turns=lambda: 0, close_cortex=rig.close, check=slow_check, run=rig.run, environ={}, windows=True,
                     log=lambda m: None)
    first, second = await asyncio.gather(r.restart("mcp"), r.restart("mcp"))
    assert len(rig.ran) == 1 and sorted(["перезапущен" in first, "уже идёт" in second]) == [True, True]
    assert (await r.restart("mcp")).startswith("mcp: перезапущен")   # имя освободилось


async def test_already_up_frees_the_name():
    rig = _Rig()

    async def up(name):
        return "up"
    r = rs.Restarter(turns=lambda: 0, close_cortex=rig.close, check=up, run=rig.run, environ={}, windows=True,
                     log=lambda m: None)
    assert "уже поднялся" in await r.restart("mcp") and "уже поднялся" in await r.restart("mcp")


def test_process_table_that_hangs_reads_as_unknown(monkeypatch):
    import subprocess
    from gateway import stand_checks as sc
    seen = {}

    def run(*a, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired("powershell", kw.get("timeout"))
    monkeypatch.setattr(sc.subprocess, "run", run)
    assert sc.python_cmdlines() is None and seen["timeout"] == sc.PROCESS_QUERY_TIMEOUT_S


async def test_cortex_is_down_when_kept_warm_but_not_alive(monkeypatch):
    class _Acp:
        def __init__(self, alive, exited=False):
            self.alive, self.exited, self.started = alive, exited, False

        async def start(self):
            self.started, self.alive = True, True

        async def close(self):
            self.alive = False
    worker, provider = _Acp(False), _Acp(False)
    monkeypatch.setattr(agent, "_cortex_agents", lambda create=False: [provider, worker])
    monkeypatch.setattr(agent, "_acp_provider_agent", provider)
    monkeypatch.setattr(agent, "_cortex_kept", [])
    monkeypatch.setattr(agent, "_cortex_warm_failed", False)
    monkeypatch.setattr(agent, "CORTEX_WARM", True)
    monkeypatch.setenv("LLM_SOURCE", "zai")
    assert not agent.cortex_down()                           # до подъёма при старте: не поломка
    await agent.warm_cortex()
    assert worker.started and not provider.started          # поставщик acp не основная модель - не поднимается
    assert not agent.cortex_down()
    worker.alive = False
    assert agent.cortex_down()                               # держится поднятым, а процесса нет - надзор поднимет
    healthy = _Acp(True)
    monkeypatch.setattr(agent, "_cortex_kept", [healthy, worker])
    assert await agent._close_cortex(only_dead=True) == 0
    assert worker.alive and healthy.alive and not healthy.started   # живой сосед не тронут


async def test_cortex_that_failed_to_build_is_down(monkeypatch):
    def broken(create=False):
        raise ImportError("cortex_workers")
    monkeypatch.setattr(agent, "_cortex_agents", broken)
    monkeypatch.setattr(agent, "_cortex_warm_failed", False)
    monkeypatch.setattr(agent, "CORTEX_WARM", True)
    await agent.warm_cortex()
    assert agent._cortex_warm_failed
