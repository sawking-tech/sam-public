"""Память на входе до развилки основной модели (SAM-101, gateway/recall.py)."""
import asyncio
import threading

import pytest

from gateway import main_model, recall

AT = "2026-10-10T02:00:00+06:00"
SHORT_TIMEOUT_S = 0.05
# Предел ожидания в тесте, а не замер: поиск ждёт события, срок нужен только чтобы сломанный тест не висел
HANG_GUARD_S = 10
CHAT = {"text": "где Ирина", "source": "monitor"}
LONG_FACTS = "- короткий\n\n-   " + "я" * (recall.FACT_CHARS + 10)


async def _at_input(model, search, message, timeout_s=HANG_GUARD_S):
    return await recall.at_input(model, search, "Ирина", limit=5, timeout_s=timeout_s, at=AT, phrase="где Ирина",
                                 session="monitor", message=message)


def _broken(q, limit):
    raise RuntimeError("qdrant down")


@pytest.mark.parametrize("in_gateway,search,message,returned,delivered", [
    # Модель вне шлюза: вход уходит в deliver с полем facts без маркеров списка
    (False, lambda q, limit: "- Ирина - жена Антона", CHAT, "- Ирина - жена Антона",
     [{"ts": AT, **CHAT, "facts": ["Ирина - жена Антона"]}]),
    # Модель в шлюзе: факты - контекст хода, доставки нет
    (True, lambda q, limit: "- факт", None, "- факт", []),
    # Поиск упал или пуст - вход уходит один
    (False, _broken, {"text": "где Ирина"}, "", [{"ts": AT, "text": "где Ирина"}]),
    (False, lambda q, limit: "", None, "", []),
    # Маркеры списка сняты, длинная заметка обрезана до FACT_CHARS
    (False, lambda q, limit: LONG_FACTS, CHAT, LONG_FACTS,
     [{"ts": AT, **CHAT, "facts": ["короткий", "я" * (recall.FACT_CHARS - 1) + "…"]}]),
], ids=["outside-model-gets-facts", "gateway-model-context-only", "failed-search", "empty-search", "fact-lines"])
async def test_input_reaches_main_model_with_facts(in_gateway, search, message, returned, delivered):
    got = []
    model = main_model.ConcreteModel("m", lambda: True, in_gateway=in_gateway, deliver=got.append)
    assert await _at_input(model, search, message) == returned
    assert got == delivered


async def test_late_memory_does_not_hold_input_and_catches_up():
    """Поиск не успел к сроку: вход уходит без фактов, факты догоняют записью rag со ссылкой на время входа."""
    release = threading.Event()
    caught_up = asyncio.Event()
    delivered = []

    def deliver(record):
        delivered.append(record)
        if len(delivered) == 2:
            caught_up.set()
    model = main_model.claude_code(lambda: True, deliver)

    def slow(q, limit):
        release.wait(HANG_GUARD_S)
        return "- Ирина - жена Антона"
    try:
        facts = await _at_input(model, slow, CHAT, timeout_s=SHORT_TIMEOUT_S)
        assert facts == ""
        assert delivered == [{"ts": AT, **CHAT}]
    finally:
        release.set()
    await asyncio.wait_for(caught_up.wait(), HANG_GUARD_S)
    assert delivered[1] == {"source": "rag", "at": AT, "phrase": "где Ирина", "session": "monitor",
                            "facts": ["Ирина - жена Антона"]}
