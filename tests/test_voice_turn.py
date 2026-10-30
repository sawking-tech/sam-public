"""Голосовой ход (ADR-010, SAM-53): фраза -> один вызов LLM потоком -> речь; кому сказано, тишина, граница быстрой,
ведущий вне шлюза. Ход собран из настоящих модулей (tests/fakes_voice.py), подменён поставщик LLM."""
import threading
from pathlib import Path

import pytest
import yaml

from gateway import addressee, main_model, phrase_class, quiet_mode
from gateway import voice_turn as vt
from gateway.workflow_spec import parse_workflow
from tests.fakes_voice import FAST_ROUTE, MAIN_ROUTE, SESSION, Clock, FakeLLM, reply, text_call, tool_call, voice_stand
# Прежние имена помощников: их берут тесты других групп (background_visible, dialogue_notes, rough_edges, turn_trace)
from tests.fakes_voice import CountingLLM as _CountingLLM, legacy_reply as _resp, legacy_voice_deps as _deps  # noqa: F401

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
HEARD_AT = "2026-10-10T02:00:00+06:00"


def _handoff_flow():
    path = WORKFLOWS / "fast-handoff.yml"
    return parse_workflow(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


async def test_phrase_to_sam_streams_one_call_and_history_waits_for_bridge():
    """Один вызов LLM с приоритетом голоса, дельты потоком; ответ виден монитору, в историю - по отчёту моста."""
    stand = voice_stand(FakeLLM("Привет!", stream=["При", "вет!"]), ledger=True)
    seen = []
    result = await stand.turn("Сэм, привет", on_delta=seen.append)
    assert len(stand.llm.calls) == 1 and result.voice == "Привет!" and seen == ["При", "вет!"]
    assert stand.llm.priorities == [vt.llm_gate.PRIORITY_VOICE]
    assert vt.llm_gate.current_priority() == vt.llm_gate.PRIORITY_DEFAULT
    assert stand.log.names().count("user_message") == 1
    assert [r["system"] for r in stand.replies()] == [True]          # монитор видит, история не пишет
    assert stand.deps.ledger.pending(SESSION)
    assert stand.history.said() == [("user", "Сэм, привет")]


SPAWN_ARGS = {"description": "открой браузер"}


@pytest.mark.parametrize("route, answer, offered, ran", [
    (MAIN_ROUTE, reply("Открываю", [tool_call("c1", "spawn_agent", SPAWN_ARGS)]), ["spawn_agent", "quiet"],
     [("spawn_agent", SPAWN_ARGS)]),
    (MAIN_ROUTE, reply("Открываю. " + text_call("spawn_agent", SPAWN_ARGS)), ["spawn_agent", "quiet"],
     [("spawn_agent", SPAWN_ARGS)]),
    (MAIN_ROUTE, reply("Ок", [tool_call("c1", "mcp_call", {"device": "pc", "tool": "x"})]), ["spawn_agent", "quiet"],
     []),
    (FAST_ROUTE, reply("Ок"), ["quiet"], []),           # у быстрой вызовов нет, кроме минимума (SAM-169)
], ids=["native", "text", "outside-voice-path", "fast"])
async def test_voice_turn_runs_only_voice_tools(route, answer, offered, ran):
    """Нативный и текстовый spawn_agent исполняются без второго вызова; инструмент не голосового пути - нет."""
    calls = []

    def recorder(name):
        async def _impl(args):
            calls.append((name, args))
            return "Задача abcd создана, выполняю в фоне."
        return _impl
    stand = voice_stand(FakeLLM(answer), route=route,
                        tools={name: recorder(name) for name in ("spawn_agent", "quiet", "mcp_call")})
    result = await stand.turn("Сэм, открой браузер")
    assert len(stand.llm.calls) == 1 and stand.llm.offered() == offered
    assert calls == ran and result.tools == [name for name, _ in ran]


@pytest.mark.parametrize("in_time", [True, False])
async def test_memory_in_time_enters_context_late_memory_does_not_hold_turn(in_time):
    release = threading.Event()

    def search(query, limit):
        if not in_time:
            release.wait()          # поиск ещё идёт, когда срок памяти вышел
        return "- Мурка рыжий"
    stand = voice_stand(FakeLLM("Да"), rag=search, **({} if in_time else {"rag_timeout_s": 0}))
    try:
        result = await stand.turn("Сэм, какой Мурка")
    finally:
        release.set()
    assert result.rag_used is in_time and len(stand.llm.calls) == 1
    assert ("Мурка рыжий" in stand.llm.system()) is in_time


@pytest.mark.parametrize("route, reset, expected, source", [
    (FAST_ROUTE, False, [("source", vt.SOURCE_FAST), "При", "вет"], vt.SOURCE_FAST),
    (MAIN_ROUTE, False, [("source", vt.SOURCE_MAIN), "При", "вет"], vt.SOURCE_MAIN),
    # поставщик сменился посреди ответа: замену llm_client берёт маршрутом по умолчанию - отвечает основная
    (FAST_ROUTE, True, [("source", vt.SOURCE_FAST), "При", "<reset>", ("source", vt.SOURCE_MAIN), "вет"],
     vt.SOURCE_MAIN),
], ids=["fast", "main", "reset"])
async def test_answer_source_told_before_deltas_and_switch_hands_turn_to_main(route, reset, expected, source):
    stand = voice_stand(FakeLLM("Привет", stream=["При", "вет"], reset=reset), route=route)
    seen = []
    result = await stand.turn("Сэм, привет", on_delta=seen.append, on_reset=lambda: seen.append("<reset>"),
                              on_source=lambda s: seen.append(("source", s)))
    assert seen == expected and result.source == source
    assert [r["source"] for r in stand.replies()] == [source]
    assert ("assistant_stream_reset" in stand.log.names()) is reset


@pytest.mark.parametrize("raw, spoken", [
    ("Сейчас скажу. <voice>Да.</voice>", "Сейчас скажу. Да."),            # прозрачный режим: звучит весь ответ
    ("<silent></silent>", ""),
    ("<silent>не мне</silent>", ""),
    (text_call("noop", {"reason": "болтовня"}), ""),   # модель повторяла формат истории, и вызов звучал JSON-ом
    ("", ""),
])
async def test_silence_and_service_text_never_sound(raw, spoken):
    """Молчание и вызов текстом - пустая реплика: окно беседы не открывается, ответ не метится передачей и spoken=0
    (workflow быстрой молчание не проверяют)."""
    stand = voice_stand(FakeLLM(raw), route=FAST_ROUTE, responder_for=phrase_class.responder_for)
    result = await stand.turn("открой окно, душно", who=None)      # «не уверена»: люди между собой
    assert result.addressed == addressee.UNSURE and result.voice == spoken
    assert (SESSION in stand.deps.last_spoken) is bool(spoken)
    [event] = stand.replies()
    on = vt.HANDOFF_ON if spoken else vt.HANDOFF_OFF
    assert event[vt.HANDOFF_FIELD] == on and event[vt.SPOKEN_FIELD] == (vt.SPOKEN_ON if spoken else vt.SPOKEN_OFF)


async def test_unaddressed_phrase_skips_model_and_unsure_gets_right_to_silence():
    """Разговор дома: фраза коту модель не зовёт и видна монитору скрытой записью; команда сразу после ответа SAM -
    ей; вне окна беседы фраза Ирины уходит модели с тем, кто сказал, и правом промолчать."""
    clock = Clock(100.0)
    stand = voice_stand(FakeLLM("Длинный ответ.", "Повторяю.", "<silent></silent>"), clock=clock)

    cat = await stand.turn("Мурка, брысь со стола")
    assert cat.addressed == addressee.NOT_ME and cat.voice == "" and stand.llm.calls == []
    assert [e.get("ignore") for e in stand.log.of("user_message")] == ["не мне - обращение к коту; Антон"]
    assert stand.history.said() == []                  # в контекст модели и сводку фраза не идёт (SAM-98)

    await stand.turn("Сэм, расскажи про погоду")
    clock.t += 5
    again = await stand.turn("повтори", who=None)
    assert again.addressed == addressee.ME and len(stand.llm.calls) == 2

    clock.t += 60
    unsure = await stand.turn("Она опять тормозит, потому что думает", who="partner")
    assert unsure.addressed == addressee.UNSURE and unsure.voice == ""
    assert "partner" in stand.llm.phrase(2) and "<silent></silent>" in stand.llm.phrase(2)


@pytest.mark.parametrize("request_quiet", ["Стоп", "Пока не озвучивай ничего, мы тут разговариваем"])
async def test_quiet_on_request_model_called_only_by_name(request_quiet):
    """Запись 9 окт 22:26-22:28: после «Стоп» ни одного ответа, пока SAM не позвали по имени."""
    clock, said = Clock(100.0), []
    stand = voice_stand(FakeLLM("Длинный ответ.", "Слушаю."), clock=clock,
                        say=lambda session, text: said.append(text) or True)
    await stand.turn("Сэм, расскажи про стенд")
    clock.t += 5
    heard = [(request_quiet, None), ("и книга холодно", None), ("Но это достаточно много", "owner"),
             ("— это уже проблемы, их слишком много Мне не помещаются Ирина с Ильёй", "partner"), ("Повтори ещё раз", "owner")]
    for text, who in heard:
        result = await stand.turn(text, who=who)
        assert result.voice == "" and result.addressed == addressee.BACKGROUND, text
        clock.t += 10
    assert len(stand.llm.calls) == 1 and stand.deps.quiet.is_quiet(SESSION)
    assert said == [quiet_mode.ENTER_SAY]               # вход в тишину - готовой фразой, без модели
    result = await stand.turn("Сэм, что скажешь?")
    assert len(stand.llm.calls) == 2 and result.voice == "Слушаю." and not stand.deps.quiet.is_quiet(SESSION)


@pytest.mark.parametrize("phrase, boundary, pause, hint, handoff", [
    ("Сэм, какая сейчас погода дома", True, None, True, True),     # факт вне разговора: подсказка паузы и метка
    ("Сэм, как дела", True, None, False, False),                   # болтовня
    ("Сэм, какая погода", False, None, False, False),              # граница выключена по умолчанию
    ("Сэм, вымой посуду", True, "Сейчас гляну.", None, True),      # пауза кодом, модель не зовётся
])
async def test_fast_boundary_action_gets_pause_and_handoff_mark(phrase, boundary, pause, hint, handoff):
    """Фраза не по силам быстрой: она берёт паузу, ответ помечен передачей, workflow fast-handoff его ловит."""
    stand = voice_stand(FakeLLM("Сейчас гляну."), route=FAST_ROUTE,
                        **({"responder_for": phrase_class.responder_for} if boundary else {}),
                        **({"pause": lambda: pause} if pause else {}))
    deltas = []
    result = await stand.turn(phrase, on_delta=deltas.append)
    [event] = stand.replies()
    if pause:
        assert stand.llm.calls == [] and deltas == [pause] and result.voice == pause
        assert event["model"] == vt.PAUSE_MODEL
    else:
        assert (vt.HANDOFF_HINT in stand.llm.phrase()) is hint
    assert event[vt.HANDOFF_FIELD] == (vt.HANDOFF_ON if handoff else vt.HANDOFF_OFF)
    assert _handoff_flow().event.matches("assistant_reply", event) is handoff


@pytest.mark.parametrize("fast_with_lead, route, phrase, pause, voice, note", [
    (False, FAST_ROUTE, "Сэм, как дела", None, "", None),             # ведущий отвечает один
    (True, MAIN_ROUTE, "Сэм, как дела", None, "", None),              # быстрой нет
    (True, FAST_ROUTE, "Сэм, как дела", None, "Всё нормально, Антон.", vt.FAST_REPLY_NOTE),
    (True, FAST_ROUTE, "Сэм, включи свет", "Сейчас гляну.", "Сейчас гляну.", vt.FAST_REPLY_NOTE_HANDOFF),
], ids=["lead-alone", "no-fast", "fast-reply", "fast-pause"])
async def test_lead_online_owns_the_turn_and_gets_fast_reply_pause_and_memory(fast_with_lead, route, phrase, pause,
                                                                              voice, note):
    """SAM-91, SAM-101, SAM-173: ведущий на связи - фраза в истории, модель шлюза молчит или отвечает быстрая и её
    ответ уходит ведущему; память ищется до развилки и уходит ведущему записью rag ко времени фразы."""
    delivered, queries = [], []
    lead = main_model.claude_code(lambda: True, delivered.append)
    stand = voice_stand(FakeLLM("Всё нормально, Антон."), route=route, main_model=lambda: lead,
                        fast_with_lead=fast_with_lead, responder_for=phrase_class.responder_for,
                        rag=lambda q, limit: queries.append(q) or "- Барсик - кот, серебристая шиншилла",
                        **({"pause": lambda: pause} if pause else {}))
    result = await stand.turn(phrase, heard_at=HEARD_AT)
    assert result.voice == voice and result.rag_used and phrase in queries[0]
    assert stand.log.names().count("user_message") == 1 and "main_model_turn" in stand.log.names()
    assert delivered[0] == {"source": "rag", "at": HEARD_AT, "phrase": phrase, "session": SESSION,
                            "facts": ["Барсик - кот, серебристая шиншилла"]}
    if note is None:
        assert stand.llm.calls == [] and stand.replies() == [] and delivered[1:] == []
        return
    assert result.source == vt.SOURCE_FAST and len(stand.llm.calls) == (0 if pause else 1)
    assert delivered[1:] == [{"text": note.format(phrase=phrase, voice=voice), "source": vt.FAST_REPLY_SOURCE,
                              "session": SESSION}]
    [event] = stand.replies()
    assert event[vt.MAIN_FIELD] == main_model.CLAUDE_CODE
    assert not _handoff_flow().event.matches("assistant_reply", event)   # при ведущем передачу ведёт он сам


def _repo_flow(name):
    path = WORKFLOWS / f"{name}.yml"
    return parse_workflow(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


@pytest.mark.parametrize("lead", [False, True], ids=["main-llm", "lead-online"])
async def test_unbacked_promise_of_fast_reaches_fast_action_only_without_lead(lead):
    """fast-voice 19, workflows 94 и 96: быстрая сказала «включила» без spawn_agent - её ответ ловит fast-action
    (и fast-review); при ведущем ни один workflow быстрой не идёт, ответ получает ведущий сам."""
    fields = {"main_model": (lambda: main_model.claude_code(lambda: True, lambda item: None)),
              "fast_with_lead": True} if lead else {}
    stand = voice_stand(FakeLLM("Включила свет в зале."), route=FAST_ROUTE, **fields)
    await stand.turn("Сэм, включи свет")
    [event] = stand.replies()

    assert event["reply_class"] == "action_unbacked" and event["phrase"] == "Сэм, включи свет"
    for name in ("fast-action", "fast-review"):
        assert _repo_flow(name).event.matches("assistant_reply", event) is not lead


@pytest.mark.parametrize("route", [FAST_ROUTE, MAIN_ROUTE], ids=["fast", "main"])
async def test_dialogue_notes_reach_the_system_message_of_the_turn(monkeypatch, mongo_db, route):
    """fast-voice 22: свежие заметки основной о разговоре (dialogue-notes) - в системном сообщении голосового хода,
    и быстрой, и основной. Заметки читает настоящий dialogue_notes_text шлюза из репозитория на mongomock."""
    from gateway import agent
    from gateway.repos.dialogue_notes_repo import DialogueNotesRepo
    repo = DialogueNotesRepo(mongo_db, max_age_s=3600)
    repo.put(SESSION, {"topic": "гости в субботу", "line": "Антон выбирает торт"})
    monkeypatch.setattr(agent, "dialogue_notes_repo", repo)
    stand = voice_stand(FakeLLM("Наполеон подойдёт."), route=route, notes=agent.dialogue_notes_text,
                        notes_parts=agent.dialogue_notes_parts)
    await stand.turn("Сэм, какой торт взять")

    system = stand.llm.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "Тема: гости в субботу" in system["content"] and "Линия: Антон выбирает торт" in system["content"]


async def test_background_and_silence_stay_out_of_the_history_the_model_reads(mongo_db):
    """addressee 13, 14: фон не зовёт модель, в истории лежит скрытой записью (монитор после F5), а в историю, которую
    читает модель, не попадает; монитор видит событие addressee с решением. Ответ <silent></silent> в историю не идёт.
    Настоящие EventBus с записью в mongomock, SpokenLedger и SessionRepo."""
    from gateway.repos import SessionRepo
    stand = voice_stand(FakeLLM("<silent></silent>"), ledger=True)
    stand.log.bus.set_db(mongo_db)

    background = await stand.turn("АПЛОДИСМЕНТЫ", who=None)
    unsure = await stand.turn("Она опять тормозит, потому что думает", who="partner")

    assert background.addressed == addressee.BACKGROUND and unsure.addressed == addressee.UNSURE
    assert len(stand.llm.calls) == 1
    assert [(e["to"], e["text"]) for e in stand.log.of("addressee")] == [
        (addressee.BACKGROUND, "АПЛОДИСМЕНТЫ"), (addressee.UNSURE, "Она опять тормозит, потому что думает")]
    hidden = mongo_db.sessions.find_one({"content": "АПЛОДИСМЕНТЫ"})
    assert hidden is not None and hidden.get("ignore", "").startswith("фон")
    seen = [(m["role"], m["content"]) for m in SessionRepo(mongo_db).get_recent(SESSION, 50)]
    assert seen == [("user", "Она опять тормозит, потому что думает")]


async def test_noop_main_model_takes_the_turn_and_nobody_answers():
    """main-model 15: основная `noop` - ход принят (фраза в истории, событие main_model_turn), модель шлюза не
    зовётся, ответа нет."""
    stand = voice_stand(FakeLLM("Всё нормально, Антон."), main_model=main_model.noop)
    result = await stand.turn("Сэм, как дела", heard_at=HEARD_AT)
    assert result.voice == "" and stand.llm.calls == [] and stand.replies() == []
    assert stand.log.names().count("user_message") == 1 and "main_model_turn" in stand.log.names()


@pytest.mark.parametrize("phrases", [("Сейчас посмотрю.", "Дай подумать.", "Сейчас гляну."), ("Секунду.",)],
                         ids=["three", "one"])
def test_fast_pause_is_from_the_setting_and_never_the_same_twice_in_a_row(monkeypatch, phrases):
    """fast-voice 9: пауза до ответа основной - из SAM_FAST_PAUSE_PHRASES, каждый раз другая (одна фраза в
    настройке - она и звучит). Выбор случайный: проверяется длинной серией."""
    from gateway import agent
    monkeypatch.setattr(agent, "FAST_PAUSE_PHRASES", phrases)
    monkeypatch.setattr(agent, "_last_pause", [])
    said = [agent._fast_pause() for _ in range(PAUSE_SERIES)]
    assert set(said) == set(phrases)
    assert len(phrases) == 1 or all(a != b for a, b in zip(said, said[1:]))


PAUSE_SERIES = 60   # при случайном выборе с повтором подряд серия из 60 дала бы повтор почти наверняка


async def test_fast_turn_gets_the_cache_layout_with_hint_next_to_the_phrase():
    """SAM-229: быстрая получает заметки и возможности в системной части, память и время - в последнем сообщении,
    подсказка хода - вплотную к фразе, после памяти."""
    stand = voice_stand(FakeLLM("Сейчас гляну."), route=FAST_ROUTE, rag="test-do-not-use факт",
                        notes_parts=lambda sid: ("ЛИНИЯ", "ЗАГОТОВКИ"), capabilities=lambda: "ВОЗМОЖНОСТИ",
                        responder_for=phrase_class.responder_for)
    await stand.turn("Сэм, какая сейчас погода дома")
    system, task = stand.llm.system(), stand.llm.phrase()
    messages = stand.llm.calls[0]["messages"]
    assert "ЛИНИЯ" in system and "ВОЗМОЖНОСТИ" in system and "test-do-not-use факт" not in system
    # фраза - реплика диалога с подписью говорящего, без памяти и времени
    assert messages[-2]["role"] == "user" and messages[-2]["content"].endswith("Сэм, какая сейчас погода дома")
    assert "test-do-not-use факт" not in messages[-2]["content"]
    assert task.index("test-do-not-use факт") < task.index("ЗАГОТОВКИ") < task.index(vt.HANDOFF_HINT)
    assert task.endswith("Сэм, какая сейчас погода дома»")   # задание с фразой повторно - последним


async def test_main_turn_keeps_notes_and_memory_in_system():
    stand = voice_stand(FakeLLM("Всё хорошо."), route=MAIN_ROUTE, rag="test-do-not-use факт",
                        notes=lambda sid: "ЗАМЕТКИ")
    await stand.turn("Сэм, как дела")
    assert "ЗАМЕТКИ" in stand.llm.system() and "test-do-not-use факт" in stand.llm.system()
