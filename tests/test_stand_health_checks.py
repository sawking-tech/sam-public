"""Health check компонентов для надзора (SAM-195): файлы живости слуха и моста, строка cortex в /health шлюза,
сборка состояния компонента из процесса и проверки. Время и файлы - параметрами и tmp_path."""
import importlib.util
from pathlib import Path

import pytest

from voice.layer import health as vh

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "stand_watch.py"
_spec = importlib.util.spec_from_file_location("stand_watch_hc", SCRIPT)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

AT = 1_791_000_000.0


@pytest.mark.parametrize("threads, status", [
    ({"say", "vmic-accept", "MainThread"}, vh.OK),
    ({"vmic-accept", "MainThread"}, vh.DEGRADED),          # поток озвучки умер - SAM не говорит
    ({"say"}, vh.DEGRADED),                                 # узел звука не принимает подписчиков
])
def test_voice_is_ok_only_with_speech_and_sound_hub_alive(threads, status):
    assert vh.status(vh.voice_parts(threads)) == status


@pytest.mark.parametrize("doc, now, state", [
    (None, AT, None),                                                    # файла нет: процесс только поднялся
    ({"at": AT, "status": vh.OK}, AT + vh.STALE_S, vh.UP),
    ({"at": AT, "status": vh.OK}, AT + vh.STALE_S + 1, vh.DOWN),         # процесс повис или умер
    ({"at": AT, "status": vh.DEGRADED}, AT + 1, vh.DOWN),
], ids=["missing", "fresh", "stale", "degraded"])
def test_health_file_state_for_the_supervisor(doc, now, state):
    assert vh.state(doc, now) == state


def test_hearing_loading_its_model_after_a_restart_is_not_down(monkeypatch, tmp_path):
    """Вкладка подняла слух, он грузит модель ~20 с: файл прошлого запуска удалён, нового ещё нет - надзор видит
    процесс и не зовёт перезапуск."""
    old = tmp_path / vh.VOICE_HEALTH
    vh.write(old, vh.record({"say": True}, AT - 3600))
    vh.clear(old)
    monkeypatch.setattr(sw.sc, "BUS_DIR", tmp_path)
    monkeypatch.setattr(sw.sc, "OFF", {"radar", "embedder", "mcp", "bridge", "tts"})
    monkeypatch.setattr(sw.sc, "python_cmdlines", lambda: ["python -m uvicorn gateway.main:app", "python -m voice.layer.voice"])
    monkeypatch.setattr(sw.sc, "fetch_health", lambda url: {"status": "ok"})
    assert sw.snapshot(now=AT)["voice"] == sw.UP
    vh.write(old, vh.record({"say": True}, AT - vh.STALE_S - 1))     # повис после старта - уже down
    assert sw.snapshot(now=AT)["voice"] == sw.DOWN


class _Loop:
    def __init__(self):
        self.later = []

    def call_later(self, delay, fn, *args):
        self.later.append((delay, fn, args))


def test_bridge_loop_mark_is_set_by_the_loop_and_a_hung_loop_is_degraded():
    loop, mark, now = _Loop(), {}, [AT]
    vh.tick_loop(loop, mark, every_s=2, clock=lambda: now[0])
    assert mark["tick"] == AT and loop.later[0][0] == 2                     # следующую метку ставит цикл
    assert vh.status(vh.bridge_parts(mark, AT + 1)) == vh.OK
    assert vh.status(vh.bridge_parts(mark, AT + vh.STALE_S + 1)) == vh.DEGRADED   # цикл висит - метка стоит
    assert vh.status(vh.bridge_parts({}, AT)) == vh.DEGRADED


def _appears(path, timeout_s=3.0):
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if (doc := vh.read(path)) is not None:
            return doc
        time.sleep(0.05)
    return None


def test_beat_threads_write_their_files_with_their_parts(tmp_path):
    vh.start_voice_beat(tmp_path)
    voice = _appears(tmp_path / vh.VOICE_HEALTH)
    assert set(voice["parts"]) == set(vh.VOICE_THREADS) and voice["status"] == vh.DEGRADED   # в тесте нет потоков слуха
    import time
    vh.start_bridge_beat(tmp_path, {"tick": time.time()}, lambda: True)
    bridge = _appears(tmp_path / vh.BRIDGE_HEALTH)
    assert bridge["status"] == vh.OK and bridge["parts"] == {"loop": True} and bridge["gateway"] is True


async def test_bridge_link_is_up_only_while_serving_the_gateway(tmp_path, monkeypatch):
    import websockets
    from voice.layer import bridge

    class _Stop(BaseException):
        pass
    seen, beat = [], {}
    monkeypatch.setattr(bridge.health, "start_bridge_beat", lambda d, mark, connected: beat.update(c=connected))

    class _Conn:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False
    tries = []

    def connect(url, **kw):
        tries.append(url)
        if len(tries) == 2:
            seen.append(beat["c"]())                 # сокет закрылся - связи нет
            raise _Stop
        return _Conn()

    async def serve(ws, d, phrases, session):
        seen.append(beat["c"]())
        raise OSError("сокет закрылся")
    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(bridge, "_serve", serve)
    monkeypatch.setattr(bridge, "RECONNECT_S", 0)
    with pytest.raises(_Stop):
        await bridge.run_bridge(tmp_path, url="ws://gateway/voice/turn")
    assert seen == [True, False]


def test_health_file_round_trip_and_broken_file(tmp_path):
    path = tmp_path / vh.VOICE_HEALTH
    vh.write(path, vh.record({"say": True}, AT))
    assert vh.state(vh.read(path), AT + 1) == vh.UP
    path.write_text("{oops", encoding="utf-8")
    assert vh.read(path) is None


@pytest.mark.parametrize("loop_running, gateway, status", [
    (True, False, vh.OK),            # шлюз лежит - мост всё равно жив, переподключается
    (False, True, vh.DEGRADED),
])
def test_bridge_beat_writes_loop_state_and_gateway_link(tmp_path, loop_running, gateway, status):
    path = tmp_path / vh.BRIDGE_HEALTH
    vh.beat_once(path, lambda: {"loop": loop_running}, lambda: {"gateway": gateway}, clock=lambda: AT)
    doc = vh.read(path)
    assert doc["status"] == status and doc["gateway"] is gateway


@pytest.mark.parametrize("states, out", [
    (("x2", sw.UP, None), "x2"), ((sw.UP, sw.DOWN, None), sw.DOWN), ((sw.UP, None, sw.UP), sw.UP),
    ((None, None, None), sw.DOWN), ((sw.DOWN, sw.UP, sw.UP), sw.DOWN),
], ids=["x2", "health-down", "all-up", "no-checks", "no-process"])
def test_component_state_is_the_worst_of_its_checks(states, out):
    assert sw.sc.worst(*states) == out


@pytest.mark.parametrize("body, previous, out", [
    ({"components": {"cortex": "ok"}}, None, {"cortex": sw.UP}),
    ({"components": {"cortex": "down: процесс Cortex вышел"}}, None, {"cortex": sw.DOWN}),
    ({"components": {}}, None, {}),                                       # старый шлюз
    (None, {"cortex": sw.DOWN, "bridge": sw.UP}, {"cortex": sw.DOWN}),    # шлюз не ответил - прежнее
], ids=["ok", "down", "old-gateway", "gateway-silent"])
def test_cortex_state_comes_from_gateway_components(body, previous, out):
    assert sw.sc.cortex_state(body, previous) == out


def test_snapshot_uses_restart_names_and_health_checks(monkeypatch):
    lines = ["python -m uvicorn gateway.main:app", "python -u -m voice.layer.voice",
             "python -u -m voice.layer.bridge", "python -m voice.layer.tts_silero_server"]
    monkeypatch.setattr(sw.sc, "python_cmdlines", lambda: list(lines))
    monkeypatch.setattr(sw.sc, "OFF", {"radar"})
    files = {vh.VOICE_HEALTH: sw.UP, vh.BRIDGE_HEALTH: sw.DOWN}
    monkeypatch.setattr(sw.sc, "read_file_health", lambda name, now: files[name])
    health = {"8000": {"status": "ok", "components": {"cortex": "ok"}}, "3000": {"status": "ok"}, "10204": None}
    monkeypatch.setattr(sw.sc, "fetch_health", lambda url: next(v for k, v in health.items() if k in url))
    assert sw.snapshot(now=AT) == {"gateway": sw.UP, "mcp": sw.UP, "embedder": sw.DOWN, "voice": sw.UP,
                                   "bridge": sw.DOWN, "tts": sw.UP, "cortex": sw.UP}


def test_gateway_reports_a_cortex_process_that_exited(monkeypatch):
    from gateway import agent, main

    class _Acp:
        def __init__(self, exited):
            self.exited, self.alive = exited, not exited
    monkeypatch.setattr(agent, "_cortex_agents", lambda create=False: [_Acp(False), _Acp(True)])
    with pytest.raises(ConnectionError, match="Cortex не жив"):
        main._health_probes()["cortex"]()
    monkeypatch.setattr(agent, "_cortex_agents", lambda create=False: [_Acp(False)])
    assert main._health_probes()["cortex"]() is None


def test_health_file_write_waits_out_a_reader_holding_the_file(tmp_path):
    import os
    path, calls, pauses = tmp_path / "x.health", [], []

    def busy_twice(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("Отказано в доступе")       # как на Windows, пока читатель держит файл
        os.replace(src, dst)
    vh.write(path, {"at": 1}, replace=busy_twice, sleep=pauses.append)
    assert vh.read(path) == {"at": 1} and len(calls) == 3 and pauses == [vh.REPLACE_PAUSE_S] * 2

    def always_busy(src, dst):
        raise PermissionError("Отказано в доступе")
    with pytest.raises(PermissionError):
        vh.write(path, {"at": 2}, replace=always_busy, sleep=lambda s: None)
    assert vh.read(path) == {"at": 1}                              # старый файл цел
