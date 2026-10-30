"""Очередь к местам вызова LLM (SAM-84): голосовой ход получает освободившееся место первым."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway import llm_client, llm_gate
from tests.fakes_core import FakeOpenAI

SLOTS = 2


async def _hold(gate: llm_gate.PriorityGate, name: str, order: list[str], release: asyncio.Event,
                priority: int = llm_gate.PRIORITY_DEFAULT) -> None:
    with llm_gate.priority_scope(priority):
        async with gate:
            order.append(name)
            await release.wait()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _run_queue(with_voice: bool) -> list[str]:
    """Места заняты, в очереди два воркера, затем (если with_voice) голосовой ход."""
    gate, order, release = llm_gate.PriorityGate(SLOTS), [], asyncio.Event()
    busy = [asyncio.create_task(_hold(gate, f"busy{i}", order, release)) for i in range(SLOTS)]
    await _settle()
    queued = [asyncio.create_task(_hold(gate, f"worker{i}", order, asyncio.Event())) for i in range(2)]
    await _settle()
    if with_voice:
        queued.append(asyncio.create_task(
            _hold(gate, "voice", order, asyncio.Event(), llm_gate.PRIORITY_VOICE)))
        await _settle()
    release.set()          # оба занятых места освобождаются
    await asyncio.gather(*busy)
    await _settle()
    for t in queued:
        t.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    return order[SLOTS:]


@pytest.mark.parametrize("with_voice, order", [
    pytest.param(True, ["voice", "worker0"], id="voice-first"),
    pytest.param(False, ["worker0", "worker1"], id="first-come-without-voice"),
])
async def test_freed_slot_goes_to_voice_turn_first(with_voice, order):
    assert await _run_queue(with_voice) == order


async def test_cancelled_waiter_does_not_lose_slot():
    gate, release = llm_gate.PriorityGate(1), asyncio.Event()
    holder = asyncio.create_task(_hold(gate, "h", [], release))
    await _settle()
    waiter = asyncio.create_task(gate.acquire())
    await _settle()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    release.set()
    await holder
    await asyncio.wait_for(gate.acquire(), timeout=1)   # место вернулось


async def test_provider_limit_holds_its_calls(monkeypatch):
    """LLM_CONCURRENCY_ZAI=1 при общем 50: второй вызов к z.ai ждёт, пока поставщик не ответит на первый."""
    monkeypatch.setattr(llm_client, "_llm_sem", llm_gate.PriorityGate(50))
    monkeypatch.setattr(llm_client, "_source_sems", {})
    monkeypatch.setattr(llm_client, "_adapter", "openai")
    monkeypatch.setenv("LLM_CONCURRENCY_ZAI", "1")
    release, first_inside = asyncio.Event(), asyncio.Event()

    async def _answer():
        first_inside.set()
        await release.wait()
        return SimpleNamespace(choices=[], usage=None)
    provider = FakeOpenAI(_answer, _answer)
    monkeypatch.setattr(llm_client, "_openai_client", provider.client)

    calls = [asyncio.create_task(llm_client.make_llm_call({"source": "zai", "model": m}, messages=[]))
             for m in ("a", "b")]
    await first_inside.wait()
    await _settle()
    assert [r["model"] for r in provider.requests] == ["a"]
    release.set()
    await asyncio.gather(*calls)
    assert [r["model"] for r in provider.requests] == ["a", "b"]
