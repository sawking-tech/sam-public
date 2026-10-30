"""Ход модели шлюза (gateway.runner): настоящий agent_loop и разбор речи, подменён только поставщик LLM."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import json

import pytest

from gateway import agent, runner, self_config
from gateway import worker as worker_runtime
from gateway.prompts import GUARD_BLOCK_MARK, GUARD_INLINE_RULES, GUARD_SECRET_RULES
from gateway.repos.session_repo import CHANNEL, IGNORE, TEXT_CHANNEL
from gateway.text_utils import transparent_speech
from tests.fakes_core import FakeLLMClient, loop_deps, off_loop_probe, resp, runner_deps, said, tc

NOW = datetime(2026, 10, 12, 12, 0)   # часы паузы между сессиями (UTC)
SCHEME = "Схема: ```\nA -> B\n```"


# ──────────────── Контракт для tests/test_voice_turn.py (группа голоса) ────────────────

class FakeEventBus:
    def __init__(self):
        self.events = []

    def emit(self, name, payload):
        self.events.append({"event": name, **payload})

    def by_name(self, name):
        return [e for e in self.events if e["event"] == name]


def _make_deps(*, history=None, llm_config=None, rag="", guard_ok=True, agent_loop_results=None):
    """RunnerDeps с очередью итогов agent_loop: этим пользуется голосовой ход (tests/test_voice_turn.py)."""
    results_q = list(agent_loop_results or [("плейн ответ", set())])

    async def _llm():
        return llm_config or {"source": "test", "model": "tm", "prompt_format": "standard"}

    async def _guard(msg, cfg):
        return guard_ok

    async def _loop(messages, cfg, names, session_id="default", max_turns=20):
        if not results_q:
            raise RuntimeError("Нет больше моков agent_loop")
        return results_q.pop(0)

    return runner.RunnerDeps(
        event_bus=FakeEventBus(),
        get_best_llm=_llm,
        get_session_history=lambda sid, limit: list(history or []),
        get_rag_context=lambda q, limit: rag,
        guard_check=_guard,
        agent_loop=_loop,
        system_prompt="ROOT SYS",
        main_tool_names=["create_task", "remember"],
        speech_of=transparent_speech,
    )


# ──────────────── Помощники ────────────────

def _first_request(deps) -> list[dict]:
    """Сообщения первого вызова поставщика в ходе."""
    return deps.llm.sent[0]


def _all_text(deps) -> str:
    return "\n".join(str(m.get("content")) for m in _first_request(deps))


@pytest.fixture
def critic_on(monkeypatch):
    """Критик ответа включён (по умолчанию в self_config) и спрашивает того же поставщика, что и ход."""
    monkeypatch.setattr(self_config, "_instance", self_config.SelfConfig())

    def _attach(deps):
        deps.llm_client = deps.llm
        deps.strip_thinking = lambda s: s
        return deps
    return _attach


# ──────────────── Что видит модель ────────────────

SKILL = {"role": "tool", "tool_name": "load_skill", "content": "Скилл radar: radar_where(person)", "linger": True}


def _skill_history(users_after: int) -> list[dict]:
    turns = []
    for i in range(users_after):
        turns += [{"role": "user", "content": f"реплика {i}"}, {"role": "assistant", "content": "ок"}]
    return [{"role": "user", "content": "где Ирина?"}, SKILL, *turns]


@pytest.mark.parametrize("session, history, rag, present, absent", [
    ("default", [], "", ["ROOT SYS", GUARD_SECRET_RULES], ["инициатор"]),
    ("workpc", [], "", ["Устройство сессии (инициатор, дефолт для create_task): workpc"], []),
    ("s", [], "факт1\nфакт2", ["Из памяти (используй активно):\nфакт1\nфакт2"], []),
    ("s", [{"role": "summary", "content": "обсуждали кофе", "time_range": "10:00-11:00"}], "",
     ["[Сжатая история предыдущего диалога]:\n[10:00-11:00] обсуждали кофе"], []),
    ("s", _skill_history(2), "", ["radar_where(person)"], []),
    ("s", _skill_history(runner.LINGER_TURNS), "", [], ["radar_where(person)"]),
    ("s", [{"role": "tool", "tool_name": "radar_where", "content": "кухня"}], "", [], ["кухня"]),
], ids=["root", "device_line", "memory", "summary", "skill_lingers", "skill_leaves", "plain_tool_result_out"])
async def test_system_prompt_carries_device_memory_summary_and_skill(session, history, rag, present, absent):
    deps = runner_deps(said("Ок."), history=history, rag=rag)
    await runner.run_turn(deps, "дальше", device_id=session)
    assert _first_request(deps)[0]["content"].startswith("ROOT SYS")
    text = _all_text(deps)
    assert all(p in text for p in present)
    assert not any(a in text for a in absent)


@pytest.mark.parametrize("fields, history, message, chat", [
    ({}, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}], "новое",
     ["hi", "hey", "новое"]),
    ({}, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"},
          {"role": "user", "content": "новое"}], "новое", ["hi", "hey", "новое"]),
    ({"history_speakers": True}, [{"role": "assistant", "content": "hey"},
                                  {"role": "user", "content": "новое", "who": "Петя"}], "новое",
     ["hey", "Петя: новое"]),
    ({}, [{"role": "user", "content": "сколько?"},
          {"role": "assistant", "content": "<voice>Пять. [судьба неизвестна]</voice>"}], "а теперь?",
     ["сколько?", "<voice>Пять.</voice>", "а теперь?"]),
    ({"history_speech_only": True, "history_speakers": True},
     [{"role": "user", "content": "погода?", "who": "Ирина"}, {"role": "assistant", "content": "<voice>Солнечно.</voice>"}],
     "спасибо", ["Ирина: погода?", "Солнечно.", "спасибо"]),
    ({}, [{"role": "user", "content": "погода?", "who": "Ирина"}, {"role": "assistant", "content": "<voice>Солнечно.</voice>"}],
     "спасибо", ["погода?", "<voice>Солнечно.</voice>", "спасибо"]),
    ({"history_speech_only": True}, [{"role": "assistant", "content": SCHEME, CHANNEL: TEXT_CHANNEL}], "ок",
     [f"{runner.TEXT_REPLY_MARK} {SCHEME}", "ок"]),
], ids=["chat_turns", "current_phrase_not_doubled", "signed_phrase_not_doubled", "legacy_fate_mark_cut",
        "voice_speech_signed", "text_chat_raw", "note_not_spoken"])
async def test_history_turns_reach_model_once_and_clean(fields, history, message, chat):
    deps = runner_deps(said("Ок."), history=history, **fields)
    await runner.run_turn(deps, message, device_id="s")
    assert [m["content"] for m in _first_request(deps)[1:]] == chat


async def test_harmony_history_gives_past_calls_as_text():
    """У Harmony нативных tools нет: прошлый вызов - в его разметке текстом."""
    history = [{"role": "tool_call", "tool_name": "search_web", "tool_args": {"query": "x"}}]
    deps = runner_deps(said("Ок."), history=history, prompt_format="harmony")
    await runner.run_turn(deps, "?", device_id="s")
    call = next(m["content"] for m in _first_request(deps)[1:] if m["role"] == "assistant")
    assert "<|channel|>" in call and "functions.search_web" in call


async def test_native_history_gives_past_calls_as_tool_calls_not_text():
    """SAM-217: текст <tool_call>{json}</tool_call> в репликах ассистента учил модель писать вызовы текстом.
    Подряд идущие вызовы - одно сообщение с tool_calls и по ответу инструмента с тем же id; итог не выдумывается."""
    history = [{"role": "user", "content": "найди"},
               {"role": "tool_call", "tool_name": "search_web", "tool_args": {"query": "x"}},
               {"role": "tool_call", "tool_name": "remember", "tool_args": {"text": "y"}},
               {"role": "assistant", "content": "нашла"}]
    deps = runner_deps(said("Ок."), history=history, prompt_format="standard")
    await runner.run_turn(deps, "?", device_id="s")
    sent = _first_request(deps)[1:]
    assert not any("<tool_call>" in str(m.get("content") or "") for m in sent)
    calls = next(m for m in sent if m.get("tool_calls"))
    ids = [tc["id"] for tc in calls["tool_calls"]]
    assert [tc["function"]["name"] for tc in calls["tool_calls"]] == ["search_web", "remember"]
    assert json.loads(calls["tool_calls"][0]["function"]["arguments"]) == {"query": "x"}
    answers = [m for m in sent if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in answers] == ids and all(m["content"] == runner.HISTORY_RESULT for m in answers)
    roles = [m["role"] for m in sent]
    assert roles[roles.index("tool") + 2] == "assistant"                       # итог хода - после ответов инструмента


@pytest.mark.parametrize("last, mark", [
    (None, None),
    ({}, None),
    ({"timestamp": NOW - timedelta(minutes=5)}, None),
    ({"timestamp": NOW - timedelta(hours=2)}, "НОВАЯ СЕССИЯ: прошло 2ч 0мин"),
    ({"timestamp": NOW - timedelta(minutes=45)}, "НОВАЯ СЕССИЯ: прошло 45мин"),
], ids=["empty_history", "no_timestamp", "five_minutes", "two_hours", "forty_five_minutes"])
async def test_long_pause_marks_new_session(last, mark):
    history = [] if last is None else [{"role": "user", "content": "x", **last}]
    deps = runner_deps(said("Ок."), history=history, utcnow=lambda: NOW)
    await runner.run_turn(deps, "привет", device_id="s")
    system = _first_request(deps)[0]["content"]
    assert (mark in system) if mark else ("НОВАЯ СЕССИЯ" not in system)


# ──────────────── Итог хода ────────────────

@pytest.mark.parametrize("message, replies, system_call, voice, shown", [
    ("привет", said("Привет!"), False, "Привет!", "привет"),
    ("включи свет", said("создаю задачу", tool="create_task"), False, "создаю задачу", "включи свет"),
    ("[TASK_DONE:abc12345] Результат от воркера: длинный текст\n\nПерефразируй живо",
     said("ok", tool="create_task"), True, "ok", "[TASK_DONE:abc12345] Результат от воркера: длинный текст"),
], ids=["plain", "spawn_intro_voiced", "task_done_first_line"])
async def test_turn_returns_clean_voice_and_emits_events(message, replies, system_call, voice, shown):
    queries = []
    history = [{"role": "user", "content": "первое"}, {"role": "assistant", "content": "ответ"},
               {"role": "user", "content": "второе"}]
    deps = runner_deps(replies, history=history)
    deps.get_rag_context = lambda q, limit: queries.append(q) or ""
    assert await runner.run_turn(deps, message, device_id="s", _system_call=system_call) == voice
    assert [e["content"] for e in deps.event_bus.by_name("user_message")] == [shown]
    assert [e["voice"] for e in deps.event_bus.by_name("assistant_reply")] == [voice]
    # память ищется по последним фразам человека вместе с текущей
    assert all(p in queries[0] for p in ("первое", "второе", message.split("\n")[0]))


@pytest.mark.parametrize("inline, judge_says, replies, result", [
    (False, False, [], runner.BLOCKED_REPLY),
    (True, False, said("Привет!"), "Привет!"),
    (True, True, [resp(tool_calls=[tc("c1", "noop", {})]), resp(content=GUARD_BLOCK_MARK)], runner.BLOCKED_REPLY),
    (False, True, said("Привет!"), "Привет!"),
], ids=["separate_guard_refuses", "inline_skips_separate_call", "inline_block_mark", "separate_guard_passes"])
async def test_guard_blocks_turn_or_rules_go_inline(inline, judge_says, replies, result):
    judged = []

    async def _judge(msg, cfg):
        judged.append(msg)
        return judge_says
    deps = runner_deps(replies, guard_inline=inline)
    deps.guard_check = _judge
    assert await runner.run_turn(deps, "как там Ирина?", device_id="s") == result
    assert judged == ([] if inline else ["как там Ирина?"])
    assert bool(deps.event_bus.by_name("guard_block")) == (result == runner.BLOCKED_REPLY)
    if deps.llm.sent:   # ход дошёл до модели: правило секретов - всегда, правила судьи - только встроенным
        system = _first_request(deps)[0]["content"]
        assert GUARD_SECRET_RULES in system
        assert (GUARD_INLINE_RULES in system) == inline


@pytest.mark.parametrize("replies, voice", [
    ([*said("открываю браузер", tool=None), *said("Запускаю.", tool="spawn_agent")], "Запускаю."),
    ([*said("открываю", tool=None), *said("всё равно открываю", tool=None)], "всё равно открываю"),
    (said("открываю браузер", tool=None), "открываю браузер"),
    ([*said("Готово, запомнила.", tool=None), *[resp(tool_calls=[tc(f"r{i}", "remember", {})]) for i in range(8)]],
     "Готово, запомнила."),
    ([*said("открываю браузер", tool=None), *[resp(tool_calls=[tc(f"r{i}", "remember", {})]) for i in range(8)]],
     "открываю браузер"),   # лимит шагов в повторе по глаголу действия не звучит вместо первого ответа
], ids=["retry_calls_the_tool", "retry_again_without_tool", "retry_failed", "retry_hit_step_limit",
        "action_retry_hit_step_limit"])
async def test_promise_without_action_gets_one_retry_and_keeps_first_answer(replies, voice):
    deps = runner_deps(replies)
    assert await runner.run_turn(deps, "open browser", device_id="s") == voice
    assert [e["voice"] for e in deps.event_bus.by_name("assistant_reply")] == [voice]


async def test_done_after_a_real_own_tool_is_one_turn_without_retry():
    """«Говори быстрее»: SAM настроила себя сама (tune_self) и говорит «сделала» - вызов был, повтора нет (SAM-07)."""
    deps = runner_deps(said("Сделала, говорю быстрее.", tool="tune_self"))
    assert await runner.run_turn(deps, "говори быстрее", device_id="s") == "Сделала, говорю быстрее."
    assert len(deps.llm.sent) == 2                         # вызов инструмента и реплика, без второго прогона


async def test_rejected_answer_whose_redo_fails_keeps_the_answer(critic_on):
    """Критик забраковал ответ, переделка упала на поставщике - звучит исходный ответ."""
    deps = critic_on(runner_deps([*said("Привет!"), resp(content="FIX: короче")]))
    assert await runner.run_turn(deps, "привет", device_id="s") == "Привет!"
    assert deps.event_bus.by_name("critic_block")


# ──────────────── Блокирующие вызовы не держат event loop (SAM-74, SAM-102) ────────────────

FACT = "- Кот Барсик"   # что отдаёт зонд на месте синхронного вызова


async def _turn_rag(probe, ctx):
    deps = runner_deps(said("Привет!"))
    deps.get_rag_context = probe
    await runner.run_turn(deps, "привет", device_id="s")


async def _critic_rules_rag(probe, ctx):
    deps = ctx.critic_on(runner_deps([*said("Привет!"), resp(content="OK")]))
    deps.get_rag_context = probe
    await runner.run_turn(deps, "привет", device_id="s")
    assert len(probe.where) == 2   # поиск хода и правила критика


async def _worker_rag(probe, ctx):
    from gateway import agent_loop, tasks, worker_session
    loop = loop_deps(llm_client=FakeLLMClient([resp(content="готово")]))

    async def _agent_loop(messages, cfg, names, max_turns, session_id):
        return await agent_loop.run_agent_loop(loop, messages, cfg, names, max_turns=max_turns, session_id=session_id)
    deps = worker_runtime.WorkerDeps(
        db=ctx.db, event_bus=loop.event_bus, tasks_module=tasks, worker_session_module=worker_session,
        agent_loop=_agent_loop, worker_tool_names=[], worker_system="You are worker", rag_fetcher=probe,
        auto_respond=lambda *a: None, task_handles={}, task_context_map={}, fallback_session_id=lambda: "main",
    )
    await worker_runtime.run_worker_task(deps, task_id="abcdef0123", description="open browser",
                                         device="workpc", llm_config={"model": "tm", "source": "test"},
                                         parent_session="main")


def _agent_tool(tool, impl):
    async def _scenario(probe, ctx):
        ctx.monkeypatch.setattr(agent, impl, probe)
        assert await agent.TOOL_IMPL[tool]({}) == FACT
    return _scenario


async def _memory_search_endpoint(probe, ctx):
    import httpx

    from gateway import main
    ctx.monkeypatch.setattr(main, "get_rag_context", probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testclient") as c:
        r = await c.get("/sam/memory/search", params={"q": "Барсик"})
    assert r.json()["facts"] == ["Кот Барсик"]


SCENARIOS = {
    "turn_rag": _turn_rag,
    "critic_rules_rag": _critic_rules_rag,
    "worker_rag": _worker_rag,
    **{f"tool_{tool}": _agent_tool(tool, impl) for tool, impl in {
        "remember": "_impl_remember", "update_memory": "_impl_update_memory",
        "list_memories": "_impl_list_memories", "forget": "_impl_forget",
        "forget_topic": "_impl_forget_topic", "sandbox_run": "_impl_sandbox_run",
    }.items()},
    "memory_search_endpoint": _memory_search_endpoint,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_blocking_calls_run_off_event_loop(name, critic_on, mongo_db, monkeypatch):
    """Эмбеддер на CPU, Qdrant, Mongo и Docker синхронны: зонд на их месте должен быть вызван в потоке."""
    probe = off_loop_probe(FACT)
    await SCENARIOS[name](probe, SimpleNamespace(critic_on=critic_on, db=mongo_db, monkeypatch=monkeypatch))
    assert probe.where and set(probe.where) == {"thread"}



async def test_tool_keeps_session_of_its_call(monkeypatch):
    """Сессия берётся в момент вызова инструмента, а не когда поток дошёл до неё: за это время другой ход мог
    переключить _current_session_id."""
    seen = []
    monkeypatch.setattr(agent, "_impl_forget_topic", lambda params, session_id: seen.append(session_id) or "ok")
    monkeypatch.setattr(agent, "_current_session_id", "s1")
    pending = agent.TOOL_IMPL["forget_topic"]({"query": "коты"})
    monkeypatch.setattr(agent, "_current_session_id", "s2")
    await pending
    assert seen == ["s1"]


@pytest.mark.parametrize("route, source", [
    (None, runner.SOURCE_MAIN),
    ("fast", runner.SOURCE_FAST),
], ids=["main", "fast"])
async def test_reply_event_names_who_answered(route, source):
    """SAM-242: монитор подписывает реплику по полю source события, у ответа основной оно тоже есть."""
    deps = runner_deps(said("Привет!"))
    base = await deps.get_best_llm()

    async def routed():
        return {**base, "route": route} if route else base
    deps.get_best_llm = routed
    await runner.run_turn(deps, "привет", device_id="s")
    assert [e["source"] for e in deps.event_bus.by_name("assistant_reply")] == [source]


async def test_reply_too_long_for_voice_keeps_a_sane_head_and_drops_the_tail():
    """SAM-244/246: больше пяти предложений - остаётся здравое начало, хвост отброшен, монитор получает generation_runaway."""
    facts = "один два три четыре пять шесть семь восемь".split()
    long_reply = " ".join(f"Новый факт номер {n} про погоду на улице." for n in facts)
    deps = runner_deps(said(long_reply))
    spoken = await runner.run_turn(deps, "расскажи", device_id="s")
    assert spoken and spoken.startswith("Новый факт номер один") and len(spoken) < len(long_reply)
    [event] = deps.event_bus.by_name("assistant_reply")
    assert event["voice"] == spoken and event["content"] == spoken and IGNORE not in event
    [report] = deps.event_bus.by_name("generation_runaway")
    assert report["sign"] == "spoken_too_long" and report["chars"] == len(long_reply) and report["kept"] == len(spoken)


async def test_reply_whose_head_is_also_bad_is_silent_and_hidden_from_models():
    babble = ("Monster Boom Monitor Box announced the new Monitor Box with Boom Monster features и объявил. " * 8).strip()
    deps = runner_deps(said("Привет. " + babble))
    assert await runner.run_turn(deps, "расскажи", device_id="s") == "Привет."   # здравое «Привет.» остаётся
    deps = runner_deps(said(babble))
    assert await runner.run_turn(deps, "расскажи", device_id="s") == ""
    [event] = deps.event_bus.by_name("assistant_reply")
    assert event["voice"] == "" and event[IGNORE].startswith("runaway:")
