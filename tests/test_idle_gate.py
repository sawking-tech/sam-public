"""Ворота простоя (SAM-62, ADR-015): фоновое сжатие ждёт тишины и не делит GPU с живым ответом.

Время ручное: часы ворот и предел ожидания - один подставной счётчик, опрос двигает его вместо сна."""
import asyncio

import pytest

from gateway import compression
from gateway.idle_gate import IdleGate

POLL_S = 1.0


class _Clock:
    """Ручные часы: sleep сдвигает время и отдаёт ход циклу."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds
        await asyncio.sleep(0)


def _gate(clock, idle_s):
    return IdleGate(idle_s=idle_s, clock=clock, poll_s=POLL_S, sleep=clock.sleep, timer=clock)


@pytest.fixture(autouse=True)
def reset_state():
    compression._compress_in_progress.clear()
    yield
    compression._compress_in_progress.clear()


async def test_idle_gate_opens_after_silence_counted_from_turn_end():
    clock = _Clock()
    gate = _gate(clock, idle_s=10)
    assert gate.is_idle()                    # новые ворота открыты

    gate.touch()                             # активность закрывает, тишина idle_s открывает
    clock.t += 9.9
    assert not gate.is_idle()
    clock.t += 0.2
    assert gate.is_idle()

    with gate.activity():                    # ход идёт минуту: тишины нет
        clock.t += 60
        assert not gate.is_idle()
    assert not gate.is_idle()                # тишина считается с конца хода
    clock.t += 11
    assert gate.is_idle()

    with pytest.raises(RuntimeError):        # упавший ход тоже отпускает ворота
        with gate.activity():
            raise RuntimeError("provider down")
    clock.t += 11
    assert gate.is_idle()

    gate.touch()                             # wait_idle дожидается тишины
    start = clock.t
    assert await gate.wait_idle() is True
    assert clock.t - start >= 10

    with gate.activity():                    # ход повис: wait_idle сдаётся по пределу
        start = clock.t
        assert await gate.wait_idle(max_s=5) is False
        assert 5 <= clock.t - start < 5 + POLL_S + 1
        assert not gate.is_idle()


# ─── фоновое сжатие за воротами ───────────────────────────────────────────────

def _schedule(gate, clock, ran, events, session, **kw):
    """Настоящая постановка фонового сжатия; само сжатие записывает время запуска по ручным часам."""
    async def _llm():
        return {"model": "m"}

    async def _compress(*a, **_):
        ran.append((session, clock.t))
        return "ok"
    return compression.schedule_background_compression(
        session, _llm, session_repo=None, archive_repo=None, meta_repo=None, summarizer=None,
        is_trivial_summary=lambda s: False, gate=gate, compress=_compress,
        event_emit=lambda kind, data: events.append(kind), **kw)


def _task(session):
    """Задача фонового сжатия сессии (имя ставит schedule_background_compression)."""
    return next(t for t in asyncio.all_tasks() if t.get_name() == f"compress:{session}")


@pytest.mark.parametrize("urgent, in_turn, waited_s, expected_events", [
    (False, False, 30, ["compression_waiting"]),    # человек только что говорил: ждёт тишины
    (True, False, 0, []),                           # несжатого выше потолка: сразу
    (False, True, 120, ["compression_waiting"]),    # ход повис: после предела без паузы
], ids=["waits-for-silence", "urgent-goes-now", "runs-after-limit"])
async def test_background_compression_waits_for_pause_up_to_limit(urgent, in_turn, waited_s, expected_events):
    clock, ran, events = _Clock(), [], []
    gate = _gate(clock, idle_s=30)
    gate.touch()
    start = clock.t
    if in_turn:
        with gate.activity():
            assert _schedule(gate, clock, ran, events, "s-gate", urgent=urgent, idle_max_s=120)
            await _task("s-gate")
    else:
        assert _schedule(gate, clock, ran, events, "s-gate", urgent=urgent, idle_max_s=120)
        assert not _schedule(gate, clock, ran, events, "s-gate")   # вторая фраза не ставит второе сжатие
        await _task("s-gate")
    assert [s for s, _ in ran] == ["s-gate"]
    assert waited_s <= ran[0][1] - start < waited_s + POLL_S + 1
    assert events == expected_events
    assert not compression.in_progress("s-gate")


@pytest.mark.parametrize("kind", ["text", "voice"])
async def test_text_and_voice_turns_hold_the_gate(monkeypatch, kind):
    """memory-in-pauses 1: текстовый и голосовой ходы шлюза держат ворота закрытыми весь ход и ещё SAM_IDLE_S
    после конца. Подменены только сами ходы (модель); обёртка шлюза и ворота настоящие."""
    from types import SimpleNamespace

    from gateway import agent
    clock = _Clock()
    gate = _gate(clock, idle_s=30)
    monkeypatch.setattr(agent._idle_gate, "gate", gate)
    during = []

    async def turn(*args, **kwargs):
        during.append(gate.is_idle())
        clock.t += 100          # ход долгий: дольше порога, ворота всё равно закрыты
        during.append(gate.is_idle())
        return SimpleNamespace(addressed="background") if kind == "voice" else "ответ"
    if kind == "text":
        monkeypatch.setattr(agent._runner_module, "run_turn", turn)
        monkeypatch.setattr(agent, "_get_runner_deps", lambda: None)
        await agent.SAMAgentRunner().run("привет", device_id="t")
    else:
        monkeypatch.setattr(agent._voice_turn_module, "run_voice_turn", turn)
        await agent.run_voice_turn("привет", "t")

    assert during == [False, False]
    clock.t += 29
    assert not gate.is_idle()
    clock.t += 1
    assert gate.is_idle()
