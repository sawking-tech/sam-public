"""WebSocket /voice/turn (SAM-53): протокол поверх голосового хода и очереди сессии (ADR-007).

serve_voice_socket - тот же, что за роутом gateway/main.py, с настоящими acquire_and_run и cancel_current_turn;
сокет моста - tests/fakes_voice.FakeVoiceSocket, ход держится на asyncio.Event, без lifespan шлюза и sleep.
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from gateway import session_lock, voice_socket
from gateway import voice_turn as vt
from gateway.spoken_history import SpokenLedger
from tests.fakes_voice import FAST_ROUTE, HANG_GUARD_S, BusLog, FakeLLM, FakeVoiceSocket, History, voice_stand


@contextlib.asynccontextmanager
async def _serving(run_turn, on_spoken=None):
    """Шлюз слушает сокет моста, пока тест его держит; на выходе мост отключается."""
    ws = FakeVoiceSocket()
    server = asyncio.create_task(voice_socket.serve_voice_socket(
        ws, run_turn, session_lock.acquire_and_run, session_lock.cancel_current_turn, on_spoken=on_spoken))
    try:
        yield ws
    finally:
        ws.close()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(server, HANG_GUARD_S)


async def test_ws_turn_streams_source_deltas_then_done():
    """Настоящий голосовой ход за сокетом: кто отвечает - до дельт, затем дельты и итог хода."""
    stand = voice_stand(FakeLLM("Привет!", stream=["При", "вет!"]), route=FAST_ROUTE)

    async def run_turn(text, session_id, **kw):
        return await vt.run_voice_turn(stand.deps, text, session_id, **kw)
    async with _serving(run_turn) as ws:
        ws.send({"type": "turn", "session": "ws-stream", "text": "Сэм, привет", "who": "owner"})
        got = [await ws.recv() for _ in range(4)]
    assert got == [{"type": "source", "source": "fast"}, {"type": "delta", "text": "При"},
                   {"type": "delta", "text": "вет!"}, {"type": "done", "voice": "Привет!", "tools": []}]


async def _failing_turn(text, session_id, **kw):
    raise RuntimeError("Все LLM провайдеры недоступны")


@pytest.mark.parametrize("message, error", [
    ("not json", "not a JSON object"),
    ({"type": "turn", "text": "   "}, "empty text"),
    ({"type": "dance"}, "unknown type"),
    ({"type": "spoken", "session": "s"}, "list of sentences"),
    ({"type": "turn", "session": "ws-fail", "text": "привет"}, "недоступны"),      # ход упал
])
async def test_ws_bad_message_or_failed_turn_is_one_error(message, error):
    async with _serving(_failing_turn, on_spoken=lambda *a: None) as ws:
        ws.send(message)
        reply = await ws.recv()
    assert reply["type"] == "error" and error in reply["error"]


async def test_ws_cancel_stops_running_turn():
    async def endless(text, session_id, on_delta=None, **kw):
        on_delta("Дол")
        await asyncio.Event().wait()
    async with _serving(endless) as ws:
        ws.send({"type": "turn", "session": "ws-cancel", "text": "расскажи длинно"})
        assert await ws.recv() == {"type": "delta", "text": "Дол"}
        ws.send({"type": "cancel", "session": "ws-cancel"})
        assert await ws.recv() == {"type": "cancelled"}


async def test_ws_phrase_during_turn_is_queued_and_each_answer_gets_done():
    """Вторая фраза в занятую сессию копится и идёт вторым ходом того же захвата: у каждого ответа свой done,
    иначе мост склеивал оба ответа в один отчёт о судьбе, а первый уходил в историю дважды."""
    heard, started, release = [], asyncio.Event(), asyncio.Event()

    async def held(text, session_id, **kw):
        heard.append(text)
        started.set()
        await release.wait()
        return SimpleNamespace(voice=f"ответ на {text}", tools=[])
    async with _serving(held) as ws:
        ws.send({"type": "turn", "session": "ws-queue", "text": "раз"})
        await asyncio.wait_for(started.wait(), HANG_GUARD_S)
        ws.send({"type": "turn", "session": "ws-queue", "text": "два"})
        assert await ws.recv() == {"type": "queued"}
        release.set()
        got = [await ws.recv() for _ in range(2)]
    assert got == [{"type": "done", "voice": "ответ на раз", "tools": []},
                   {"type": "done", "voice": "ответ на два", "tools": []}]
    assert heard == ["раз", "два"]


@pytest.mark.parametrize("key", [None, "k1"], ids=["turn-reply", "own-reply"])
async def test_ws_spoken_report_passes_on_with_optional_id(key):
    """Отчёт моста о судьбе доходит до учёта прозвучавшего; у своей реплики say - с её id (SAM-85)."""
    log = BusLog()
    history, ledger = History(log.bus), SpokenLedger(log.bus.emit)
    ledger.expect("voice", "Раз.", key=key)
    async with _serving(_failing_turn, on_spoken=ledger.settle) as ws:
        ws.send({"type": "spoken", "session": "voice", "sentences": [{"text": "Раз.", "status": "played"}],
                 **({"id": key} if key else {})})
    assert history.said("voice") == [("assistant", "Раз.")] and not ledger.pending("voice")
