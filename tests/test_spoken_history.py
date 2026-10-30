"""В историю только прозвучавшее (ADR-013, SAM-55): отчёт моста о судьбе, срок отчёта, ответ в пути в контексте
следующего хода. Шина - свежий EventBus, история - tests/fakes_voice.History."""
import asyncio

import pytest

from gateway import spoken_history as sh
from tests.fakes_voice import HANG_GUARD_S, SESSION, BusLog, FakeLLM, History, voice_stand


def _ledger(**kw):
    log = BusLog()
    return sh.SpokenLedger(log.bus.emit, **kw), History(log.bus)


@pytest.mark.parametrize("sentences, written", [
    ([{"text": "Раз.", "status": "played"}, {"text": "Два", "status": "cut"}, {"text": "Три.", "status": "dropped"},
      {"text": "Старое.", "status": "stale"}, {"text": "Ложь.", "status": "retracted"}],
     [("Раз. Два [прервано]", sh.PLAYED)]),
    # судьба - поле записи, не текст: метку в тексте модель повторяла в речи
    ([{"text": "Раз.", "status": "played"}, {"text": "Два.", "status": "unknown"}], [("Раз. Два.", sh.UNKNOWN)]),
    ([{"text": "Раз.", "status": "dropped"}], []),                     # ничего не прозвучало - записи нет
])
async def test_history_gets_only_what_was_heard_by_bridge_report(sentences, written):
    ledger, history = _ledger()
    ledger.expect(SESSION, "Раз. Два. Три.")
    assert ledger.settle(SESSION, sentences) and not ledger.pending(SESSION)
    assert [(d["content"], d[sh.FATE]) for d in history.docs] == written


async def test_reports_settle_replies_in_order_and_own_reply_by_key():
    """Собеседник перебил: новый ход кончился раньше отчёта о прежнем ответе. Мост шлёт отчёты по очереди ответов, а своя
    реплика с ключом (SAM-85) ждёт отчёта со своим ключом и ответ хода не забирает."""
    ledger, history = _ledger()
    ledger.expect(SESSION, "Своя.", key="k")
    ledger.expect(SESSION, "Первый.")
    ledger.expect(SESSION, "Второй.")
    assert ledger.settle(SESSION, [{"text": "Первый.", "status": "cut"}])
    assert history.said() == [("assistant", "Первый. [прервано]")] and ledger.pending(SESSION)
    assert ledger.settle(SESSION, [{"text": "Второй.", "status": "played"}])
    assert ledger.settle(SESSION, [{"text": "Своя.", "status": "played"}], "k")
    assert [c for _, c in history.said()] == ["Первый. [прервано]", "Второй.", "Своя."] and not ledger.pending(SESSION)


async def test_no_report_in_time_writes_unknown_fate_and_late_report_ignored():
    log = BusLog()
    written = asyncio.Event()
    log.bus.add_listener(lambda name, data: written.set() if name == sh.EVENT else None)
    history = History(log.bus)
    ledger = sh.SpokenLedger(log.bus.emit, timeout_s=0)      # срок вышел сразу: ждём запись, а не время
    ledger.expect(SESSION, "Раз.")
    await asyncio.wait_for(written.wait(), HANG_GUARD_S)
    assert [(d["content"], d[sh.FATE]) for d in history.docs] == [("Раз.", sh.UNKNOWN)]
    assert not ledger.settle(SESSION, [{"text": "Раз.", "status": "played"}])   # поздний отчёт не пишется
    assert len(history.docs) == 1


async def test_reply_still_sounding_is_in_next_turn_context():
    """Сверка 9 окт 15:2x: ответ «Пять.» ждал отчёта моста и в истории его не было; на «как дела?» быстрая модель
    видела вопрос без ответа и отвечала «Пять. Всё нормально». Ответ в пути стоит за своей фразой, текстом без тега;
    своя реплика say ни на какую фразу не отвечает и в контекст не идёт."""
    stand = voice_stand(FakeLLM("Пять.", "Норм."), ledger=True)
    await stand.turn("Сэм, сколько будет девять минус четыре?")
    stand.deps.ledger.expect(SESSION, "Тесты зелёные.", key="k")
    await stand.turn("Сэм, как дела?")
    assert stand.llm.dialogue(1) == [("user", "Антон: Сэм, сколько будет девять минус четыре?"),
                                     ("assistant", "Пять."), ("user", "Антон: Сэм, как дела?")]
    assert stand.deps.ledger.settle(SESSION, [{"text": "Пять.", "status": "played"}])
    assert stand.history.said() == [("user", "Сэм, сколько будет девять минус четыре?"), ("user", "Сэм, как дела?"),
                                    ("assistant", "Пять.")]


async def test_legacy_fate_mark_never_reaches_model_or_monitor():
    """Записи до 9 окт 19:30 несут метку судьбы текстом: модель и монитор её не видят, «[прервано]» остаётся."""
    stand = voice_stand(FakeLLM("Да."))
    stand.history.docs += [{"session_id": SESSION, "role": "user", "content": "раз?"},
                           {"session_id": SESSION, "role": "assistant", "content": "Раз. [судьба неизвестна]"},
                           {"session_id": SESSION, "role": "user", "content": "два?"},
                           {"session_id": SESSION, "role": "assistant", "content": "Два. [прервано]"}]
    await stand.turn("Сэм, ну?")
    assert [c for role, c in stand.llm.dialogue() if role == "assistant"] == ["Раз.", "Два. [прервано]"]
    assert sh.replay_fate({"content": "Раз. [судьба неизвестна]"}) == {"content": "Раз.", sh.FATE: sh.UNKNOWN}
    assert sh.replay_fate({"content": "Раз.", sh.FATE: sh.PLAYED})[sh.FATE] == sh.PLAYED
    assert sh.FATE not in sh.replay_fate({"content": "текстовый ответ"})
