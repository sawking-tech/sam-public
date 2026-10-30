"""Холодный старт стенда (SAM-195): модули поднимают надзор, надзор в одном экземпляре и поднимает лежащий шлюз."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import restart
from gateway import stand_watch_keeper as keeper

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "stand_watch.py"
_spec = importlib.util.spec_from_file_location("stand_watch_cold", SCRIPT)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

NOW = 1000.0
STALE = 10.0


@pytest.mark.parametrize("doc, stopped, expected", [
    (None, False, True),                          # надзор не запускался или файл убран при -Stop
    ({"at": NOW - STALE - 1}, False, True),       # процесса надзора нет: файл не пишется
    ({"at": NOW - STALE + 1}, False, False),      # жив
    ({"at": NOW - 1, "status": "degraded"}, False, False),   # повис, но замок у него: второй выйдет
    (None, True, False),                          # стенд остановлен (.sam_stop)
    ({"at": NOW - STALE - 1}, True, False),
], ids=["missing", "stale", "fresh", "hung-holds-lock", "stopped", "stopped-stale"])
def test_should_start(doc, stopped, expected):
    assert keeper.should_start(doc, NOW, stopped, STALE) is expected


def test_ensure_starts_only_a_dead_supervisor():
    started = []
    assert keeper.ensure(lambda: None, lambda: False, lambda: started.append(1), lambda: NOW)
    assert not keeper.ensure(lambda: {"at": NOW}, lambda: False, lambda: started.append(1), lambda: NOW)
    assert not keeper.ensure(lambda: None, lambda: True, lambda: started.append(1), lambda: NOW)
    assert started == [1]


def test_zero_period_starts_no_thread(monkeypatch):
    monkeypatch.setattr(keeper.threading, "Thread", lambda *a, **k: pytest.fail("поток не нужен"))
    keeper.start("test", every_s=0)


def test_lock_is_held_by_one_process(tmp_path):
    path = tmp_path / keeper.LOCK_NAME
    first = keeper.acquire_lock(path)
    assert first is not None
    assert keeper.acquire_lock(path) is None
    first.release()
    assert keeper.acquire_lock(path) is not None


def test_second_supervisor_exits_with_one_line(tmp_path):
    """Замок занят - запуск надзора выходит сразу, до опроса стенда, одной строкой."""
    lock = keeper.acquire_lock(tmp_path / keeper.LOCK_NAME)
    env = {**os.environ, "SAM_VOICE_BUS_DIR": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-u", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    lock.release()
    lines = out.stdout.strip().splitlines()
    assert out.returncode == 0
    assert len(lines) == 1 and "уже запущен" in lines[0]
    assert not (tmp_path / keeper.HEALTH_NAME).exists()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _gateway_keeper(command="raise-gateway", stopped=False, down_s=90.0):
    clock, runs, lines = Clock(), [], []
    flag = {"stopped": stopped}
    gk = sw.GatewayKeeper(command, lambda cmd: (runs.append(cmd), (0, "ok"))[1], lambda: flag["stopped"],
                          down_s=down_s, clock=clock, log=lines.append)
    return gk, clock, runs, lines, flag


def test_gateway_raised_after_threshold_then_not_before_another():
    gk, clock, runs, _, _ = _gateway_keeper()
    assert not gk.tick(sw.DOWN)            # только что лёг: вкладка поднимет сама
    clock.t = 89
    assert not gk.tick(sw.DOWN)
    clock.t = 90
    assert gk.tick(sw.DOWN) and runs == ["raise-gateway"]
    clock.t = 269
    assert not gk.tick(sw.DOWN)            # не встал - следующая попытка через удвоенный срок
    clock.t = 270
    assert gk.tick(sw.DOWN) and len(runs) == 2
    clock.t = 270 + 360
    assert gk.tick(sw.DOWN) and len(runs) == 3
    for _ in range(5):                      # удвоение не дальше предела: 8 сроков
        clock.t += 90 * sw.BACKOFF_MAX
        assert gk.tick(sw.DOWN)


def test_gateway_up_resets_the_count():
    gk, clock, runs, _, _ = _gateway_keeper()
    gk.tick(sw.DOWN)
    clock.t = 80
    gk.tick(sw.UP)                         # вкладка подняла
    clock.t = 100
    assert not gk.tick(sw.DOWN)
    clock.t = 189
    assert not gk.tick(sw.DOWN) and runs == []
    gk.tick("x2")                          # дубль - не лежит, подъёмом не лечится
    clock.t = 400
    assert not gk.tick(sw.DOWN) and runs == []


def test_gateway_not_raised_while_stand_is_stopped():
    gk, clock, runs, _, flag = _gateway_keeper(stopped=True)
    gk.tick(sw.DOWN)
    clock.t = 500
    assert not gk.tick(sw.DOWN) and runs == []
    flag["stopped"] = False                # стенд запущен заново: счёт с нуля
    assert not gk.tick(sw.DOWN)
    clock.t = 590
    assert gk.tick(sw.DOWN) and runs == ["raise-gateway"]


def test_gateway_without_command_says_so():
    gk, clock, runs, lines, _ = _gateway_keeper(command="")
    gk.tick(sw.DOWN)
    clock.t = 90
    assert not gk.tick(sw.DOWN) and runs == []
    assert len(lines) == 1 and restart.cmd_env(restart.GATEWAY) in lines[0]


@pytest.mark.parametrize("environ, windows, expected", [
    ({}, True, f'{restart.POWERSHELL} "{restart.START_SCRIPT}" -Gateway'),
    ({"SAM_RESTART_CMD_GATEWAY": "systemctl restart sam"}, True, "systemctl restart sam"),
    ({"SAM_RESTART_CMD_GATEWAY": "systemctl restart sam"}, False, "systemctl restart sam"),
    ({}, False, ""),
], ids=["windows-default", "env-windows", "env-posix", "posix-none"])
def test_cold_start_command(environ, windows, expected):
    assert restart.cold_start_command(environ, windows) == expected


def test_cortex_raised_by_the_gateway_after_threshold():
    clock, posts, lines = Clock(), [], []
    ck = sw.CortexKeeper(lambda: (posts.append(1), (0, "cortex: перезапущено"))[1], lambda: False,
                         down_s=30.0, clock=clock, log=lines.append)
    assert not ck.tick(sw.DOWN)
    clock.t = 30
    assert ck.tick(sw.DOWN) and posts == [1] and "Cortex" in lines[0]
    clock.t = 40
    assert not ck.tick(None) and not ck.tick(sw.UP)          # шлюз лежит (None) или Cortex встал - счёт сначала
    clock.t = 75
    assert not ck.tick(sw.DOWN) and posts == [1]
    clock.t = 105
    assert ck.tick(sw.DOWN) and len(posts) == 2               # встал и снова лёг - срок снова исходный
