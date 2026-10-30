"""Агенты основной модели (SAM-91, SAM-133): POST /sam/agents, вопрос создателю тиком во входящие, ответ
POST /sam/inject, итог основной модели. Настоящий шлюз по HTTP, настоящий воркер шлюза или сессия Cortex за ACP-парой;
подменены модель агентов, MCP Cortex, часы ожидания входящих и память."""
import asyncio

import pytest

from gateway import worker_session
from gateway.tools import agents as agents_tool
from tests.fakes_agents import AgentsRig, Gate, GatewayTurn, _resp, _tc

TASK = "собери новости про SAM"
QUESTION = "за какой период?"
ANSWER = "за неделю"
RESULT = "3 новости за неделю"
SAM, CORTEX = agents_tool.FRAMEWORK_SAM, agents_tool.FRAMEWORK_CORTEX
GRANT = {SAM: {}, CORTEX: {"tools": ["radar_where"]}}


@pytest.fixture
async def rigs(monkeypatch, tmp_path):
    made = []

    def make(routes, **kw):
        made.append(AgentsRig(monkeypatch, tmp_path, routes, **kw))
        return made[-1]
    yield make
    for rig in made:
        await rig.close()


def _ask(question=QUESTION):
    return _resp(tool_calls=[_tc("q1", agents_tool.ASK_TOOL, {"question": question})])


def _say(text):
    """Итог шага агента текстом - одинаково у воркера шлюза и Cortex."""
    return _resp(content=text)


# ─── Вопрос создателю и итог ──────────────────────────────────────────────────

@pytest.mark.parametrize("framework, between_turns", [
    (SAM, False),          # ответ лёг, пока агент писал итог «жду»
    (CORTEX, False),       # ответ - в идущий ход Cortex
    (CORTEX, True),        # ход кончился «жду», ответ будит следующий ход задачи
], ids=["sam", "cortex_running_turn", "cortex_between_turns"])
async def test_main_model_agent_asks_creator_and_result_reaches_creator(rigs, framework, between_turns):
    gate = Gate(_say("Жду ответа."))
    rig = rigs({TASK: [_ask(), gate.reply if between_turns else gate, _say(RESULT)]})
    short = await rig.create(description=TASK, framework=framework, **GRANT[framework])
    session = agents_tool.task_session(short)

    await rig.until(lambda: rig.inbox_of(agents_tool.QUESTION_SOURCE))
    [question] = rig.inbox_of(agents_tool.QUESTION_SOURCE)
    assert question["session"] == session and QUESTION in question["text"]
    assert f"POST /sam/inject session_id={session}" in question["text"]
    assert [q["text"] for q in rig.task(short)["questions"]] == [QUESTION]
    assert rig.bus.of("task_message")[0]["content"] == f"Вопрос создателю: {QUESTION}"   # виден в мониторе
    if between_turns:
        await rig.until(lambda: "session/close" in rig.pair.methods())
    else:
        await rig.until(gate.entered.is_set)

    r = await rig.post("/sam/inject", {"session_id": session, "message": ANSWER})
    assert r.json()["kind"] == "running_inject"
    gate.open.set()

    task = await rig.finished(short)
    assert task["status"] == "done" and RESULT in task["result"]
    [done] = rig.inbox_of(agents_tool.DONE_SOURCE)
    assert done["session"] == session and f"[TASK_DONE:{short}]" in done["text"] and RESULT in done["text"]
    assert f"Он спрашивал: {QUESTION}" in done["text"]
    assert ANSWER in str(rig.llm.by_task[TASK][-1]["messages"])           # ответ дошёл до агента
    assert session not in rig.context                                       # сессия отвязана: inject - воскрешение


# ─── Создание: отказы ─────────────────────────────────────────────────────────

async def _no_model(*args, **kwargs):
    return None


@pytest.mark.parametrize("body, trouble, code", [
    ({"description": " "}, None, 400),
    ({"description": "x", "level": "genius"}, None, 400),
    ({"description": "x", "tools": ["rm_rf"]}, None, 400),                    # нет такого инструмента воркера
    ({"description": "x", "framework": CORTEX}, None, 400),                  # cortex без выдачи
    ({"description": "x", "framework": "hermes"}, None, 400),
    ({"description": "x", "depth": "глубоко"}, None, 422),                    # не число - отказ схемы запроса
    ({"description": "x"}, "busy", 429),                                      # все воркеры заняты
    ({"description": "x"}, "no_device", 503),
    ({"description": "x"}, "no_model", 503),                                  # роутер не нашёл модель метки
    ({"description": "x"}, "no_mongo", 503),
], ids=["no_description", "unknown_level", "unknown_tool", "cortex_without_grant", "unknown_framework",
        "depth_not_number", "busy", "no_device", "no_model", "no_mongo"])
async def test_create_agent_refusal_maps_to_http_code(rigs, monkeypatch, body, trouble, code):
    rig = rigs({})
    busy = []
    if trouble == "busy":
        busy = [asyncio.get_running_loop().create_future() for _ in range(rig.agent.MAX_WORKERS)]
        rig.handles.update({f"busy{i}": f for i, f in enumerate(busy)})
    elif trouble == "no_device":
        rig.db["catalog"].delete_many({})
    elif trouble == "no_model":
        monkeypatch.setattr(rig.agent, "get_best_llm", _no_model)   # граница роутера: поставщиков нет
    elif trouble == "no_mongo":
        monkeypatch.setattr(rig.agent, "_mongo_ok", False)
    r = await rig.post("/sam/agents", body, privileged=True)
    assert r.status_code == code and r.json()["detail"]
    assert code == 422 or "\n" not in r.json()["detail"]
    assert rig.db.tasks.count_documents({}) == 0 and rig.pair.agents == []
    for future in busy:
        future.cancel()
    rig.handles.clear()


# ─── Создание: права, метка, каркас, дерево ───────────────────────────────────

def _session_meta(rig) -> dict:
    """_meta сессии Cortex с провода ACP: что увидел агент."""
    return next(m["params"]["_meta"] for m in rig.pair.wire if m.get("method") == "session/new")


@pytest.mark.parametrize("body, env, framework, tools, meta", [
    ({"tools": ["search_web"], "level": "weak"}, {}, SAM,
     ["search_web", "report_status", "ask_creator", "estimate_steps", "request_steps"], None),   # сужено, каналы остаются
    ({}, {}, SAM, None, None),                                                # без выдачи - все инструменты воркера
    ({"tools": ["radar_where"], "peers": [" ab12 ", "", "  "], "depth": 5}, {"SAM_AGENT_FRAMEWORK": CORTEX}, CORTEX,
     ["radar_where"], {"creator": agents_tool.CREATOR_MAIN_MODEL, "tools": ["radar_where"], "level": "medium",
                       "peers": ["ab12"], "depth": 1, "children": 1}),        # каркас из конфига, depth срезан
    ({"framework": CORTEX, "tools": ["radar_where"]}, {}, CORTEX, ["radar_where"],
     {"creator": agents_tool.CREATOR_MAIN_MODEL, "tools": ["radar_where"], "level": "medium"}),   # без детей
    ({"framework": SAM, "depth": 1}, {"SAM_AGENT_FRAMEWORK": CORTEX}, SAM, None, None),   # depth - только у cortex
], ids=["sam_narrowed", "sam_all_tools", "cortex_from_env_depth_capped", "cortex_no_children", "depth_only_cortex"])
async def test_create_agent_records_rights_level_and_tree(rigs, monkeypatch, body, env, framework, tools, meta):
    rig = rigs({TASK: [_say(RESULT)]})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    r = await rig.post("/sam/agents", {"description": TASK, **body}, privileged=True)
    assert r.status_code == 200, r.text
    task = await rig.finished(r.json()["task_id"])
    granted = tools if tools is not None else rig.agent.WORKER_TOOL_NAMES   # без выдачи - все инструменты воркера
    assert (task["tools"], task["level"], task["creator"]) == (granted, body.get("level", "medium"),
                                                               agents_tool.CREATOR_MAIN_MODEL)
    if framework == CORTEX:
        assert _session_meta(rig) == meta
    else:
        seen = {t["function"]["name"] for t in rig.llm.by_task[TASK][0]["tools"]}
        assert rig.pair.agents == [] and seen == set(granted) and r.json()["tools"] == granted


async def test_level_model_comes_from_config(rigs, monkeypatch):
    rig = rigs({TASK: [_say(RESULT)]})
    monkeypatch.setenv("SAM_AGENT_LLM_SMART", "Zai")
    monkeypatch.setenv("ZAI_API_KEY", "test-do-not-use")
    r = await rig.post("/sam/agents", {"description": TASK, "level": "smart"}, privileged=True)
    await rig.finished(r.json()["task_id"])
    assert rig.llm.models == [r.json()["model"]] and r.json()["model"] != "gemini-2.0-flash"


# ─── Сообщение в сессию агента ────────────────────────────────────────────────

async def test_message_to_a_finished_worker_reanimates_it_with_its_history(rigs):
    rig = rigs({TASK: [_say("Нашла 2 новости."), _say(RESULT)]})
    short = await rig.create(description=TASK)
    await rig.finished(short)
    history = worker_session.find_by_prefix(rig.db, short)["messages"]
    r = await rig.post("/sam/inject", {"session_id": f"task:{short}", "message": ANSWER})
    assert r.json()["kind"] == "reanimated" and r.json()["task_id"] == short
    await rig.until(lambda: len(rig.inbox_of(agents_tool.DONE_SOURCE)) == 2)
    sent = rig.llm.by_task[TASK][-1]["messages"]
    assert sent[:len(history)] == history and sent[len(history)]["content"] == f"[CONTINUE] {ANSWER}"
    assert RESULT in (await rig.finished(short))["result"]


@pytest.mark.parametrize("session, dead, reason", [
    ("task:zz99", False, agents_tool.NOT_FOUND),
    ("task:dead0001", True, agents_tool.NOT_FOUND),
    ("task:", False, agents_tool.BAD_REQUEST),          # пустой префикс входит в имя любой задачи (SAM-76)
], ids=["unknown", "dead", "empty_id"])
async def test_message_to_an_unknown_or_dead_agent_is_an_error(rigs, session, dead, reason):
    rig = rigs({})
    if dead:
        worker_session.save_session(rig.db, "dead0001-0000", device="pc", description="x", messages=[],
                                    parent_session="voice", status="dead")
    r = await rig.post("/sam/inject", {"session_id": session, "message": "m"})
    assert r.json()["status"] == "error" and r.json()["reason"] == reason and rig.handles == {}


# ─── Воркер spawn_agent: вопрос и итог остаются в шлюзе ──────────────────────

async def test_spawn_agent_worker_question_and_result_stay_in_gateway(rigs, monkeypatch):
    rig = rigs({TASK: [_ask("что открыть?"), _say(RESULT)]})
    turn = GatewayTurn("Готово, нашла.")
    monkeypatch.setattr(rig.agent, "sam_agent", turn)                          # ход основной модели в шлюзе
    monkeypatch.setattr(rig.agent, "_current_llm", {"source": "gemini", "model": "gemini-2.0-flash"})
    monkeypatch.setattr(rig.agent, "_current_session_id", "voice")
    reply = await rig.agent.TOOL_IMPL["spawn_agent"]({"description": TASK})
    short = reply.split()[1]
    task = await rig.finished(short)                                            # спросила - ответ хода модели в шлюзе
    assert task["creator"] == "orchestrator" and task["status"] == "done"
    assert rig.inbox() == []                                                    # наружу не идёт
    assert rig.bus.of("task_message")[0]["content"] == "Вопрос создателю: что открыть?"
    await rig.until(lambda: len(turn.turns) == 2)
    assert "что открыть?" in turn.turns[0][0] and turn.turns[0][1] == "voice"    # SAM-212: вопрос - ходом модели
    assert agents_tool.creator_reply("Готово, нашла.") in str(rig.llm.by_task[TASK][-1]["messages"])
    assert f"[TASK_DONE:{short}]" in turn.turns[1][0] and RESULT in turn.turns[1][0]


@pytest.mark.parametrize("params, said", [({"question": ""}, "Нужен question"),
                                          ({"question": "?"}, "ask_creator работает только внутри агента")])
async def test_ask_creator_outside_an_agent_or_empty_is_refused(rigs, params, said):
    rig = rigs({})
    assert said in await rig.agent.TOOL_IMPL[agents_tool.ASK_TOOL](params) and rig.inbox() == []


# ─── Inject к основной модели вне шлюза ───────────────────────────────────────

async def test_inject_to_outside_main_model_carries_memory_facts(rigs):
    from gateway import rag
    rig = rigs({})
    assert rag.save_fact("Барсик - кот Ирины")
    r = await rig.post("/sam/inject", {"session_id": "monitor", "message": "Барсик - кот Ирины?"})
    assert r.status_code == 200 and r.json()["kind"] == "main_model"
    [delivered] = rig.inbox()
    assert delivered["text"] == "Барсик - кот Ирины?" and delivered["facts"] == ["Барсик - кот Ирины"] and delivered["ts"]


# ─── Inject в простаивающую основную модель шлюза (SAM-259) ───────────────────

async def test_inject_wake_emits_turn_failure(rigs, monkeypatch, caplog):
    from gateway import main, main_model
    rig = rigs({})
    monkeypatch.setattr(main, "main_models", main_model.MainModel({main_model.LLM: main_model.llm()}, [main_model.LLM]))

    async def run(message, device_id=None, user_id=None, images=None):
        raise RuntimeError("боль")

    monkeypatch.setattr(main.sam_agent, "run", run)
    r = await rig.post("/sam/inject", {"session_id": "monitor", "message": "проснись"})
    assert r.json()["kind"] == "woke_main_model"
    await rig.until(lambda: rig.bus.of("turn_failed"))
    failure = rig.bus.of("turn_failed")[-1]
    assert failure["session"] == "monitor"
    assert failure["error"] == "ход inject-wake сессии monitor упал: RuntimeError: боль"
    assert "[Inject] ERROR ход inject-wake сессии monitor упал: RuntimeError: боль" in caplog.text


async def test_inject_wakes_an_idle_gateway_model_and_queues_into_a_running_turn(rigs, monkeypatch):
    from gateway import main, main_model
    rig = rigs({})
    monkeypatch.setattr(main, "main_models", main_model.MainModel({main_model.LLM: main_model.llm()}, [main_model.LLM]))
    ran = []

    async def run(message, device_id=None, user_id=None, images=None):
        ran.append((message, device_id))
        return "ok"
    monkeypatch.setattr(main.sam_agent, "run", run)

    r = await rig.post("/sam/inject", {"session_id": "monitor", "message": "проснись, есть дело"})
    assert r.json()["kind"] == "woke_main_model"
    await rig.until(lambda: ran)
    assert ran == [("проснись, есть дело", "monitor")]

    monkeypatch.setattr(main, "is_busy", lambda sid: True)               # ход идёт: сообщение встаёт в его очередь
    r = await rig.post("/sam/inject", {"session_id": "monitor", "message": "ещё одно"})
    assert r.json()["kind"] == "live_inject" and len(ran) == 1
    assert rig.bus.pop_inject("monitor") == "ещё одно"


# ─── Лимит тяжёлой работы (SAM-260) ──────────────────────────────────────────

async def test_heavy_task_over_the_limit_is_queued_as_is_and_the_scheduler_starts_it_when_the_limit_opens(rigs):
    from datetime import datetime, timedelta
    from gateway import deferred_tasks
    rig = rigs({TASK: [_say(RESULT)]})
    spent_at = datetime.utcnow() - timedelta(minutes=10)
    rig.db["_token_usage"].insert_one({"timestamp": spent_at, "role": "worker", "source": "polza", "model": "m", "cost": 16.0,
                                       "input_tokens": 1, "output_tokens": 1, "session_id": "task:old"})   # час выбит (15 ₽)
    heavy = await rig.post("/sam/agents", {"description": "Сделай рефакторинг модуля учёта и тесты"}, privileged=True)
    assert heavy.status_code == 200 and heavy.json()["queued"] is True and heavy.json()["note"] == deferred_tasks.ACCEPTED
    assert rig.db.tasks.count_documents({}) == 0 and len(deferred_tasks.pending(rig.db)) == 1      # принято, агента ещё нет
    assert await rig.agent.run_deferred_tasks() == []                                              # лимит закрыт: ждёт
    light = await rig.post("/sam/agents", {"description": TASK, "steps": 3}, privileged=True)       # лёгкое по прикидке идёт всегда
    assert light.status_code == 200 and "queued" not in light.json()

    rig.db["_token_usage"].update_many({}, {"$set": {"timestamp": datetime.utcnow() - timedelta(hours=2)}})   # трата вышла из часа
    assert len(await rig.agent.run_deferred_tasks()) == 1 and deferred_tasks.pending(rig.db) == []   # шедулер запустил сам


async def test_a_light_task_pool_is_recorded_and_its_spending_does_not_take_the_window(rigs):
    from datetime import datetime
    rig = rigs({TASK: [_say(RESULT)]})
    short = await rig.create(description=TASK, steps=5)                    # пул 5 шагов: лёгкая работа
    await rig.finished(short)
    assert rig.task(short)["pool_steps"] <= 10
    rig.db["_token_usage"].insert_one({"timestamp": datetime.utcnow(), "role": "worker", "source": "polza", "model": "m",
                                       "cost": 99.0, "input_tokens": 1, "output_tokens": 1, "session_id": f"task:{short}"})
    assert (await rig.http.get("/sam/usage/dev-budget")).json()["spent_rub"]["window"] < 40         # 99 ₽ лёгкой задачи не в окне


async def test_agent_info_shows_model_steps_and_working_time(rigs):
    from gateway import agent
    rig = rigs({})
    task_id = await rig.create(description="найди рецепт борща", steps=5)
    info = agent.agent_info(task_id)
    assert info["task_id"] == task_id and info["steps"]["pool"] == 5
    assert info["model"] and info["seconds"] >= 0 and info["effort_changes"] == 0
    assert agent.agent_info("nope")["error"]


async def test_only_the_creator_changes_the_effort_of_its_agent(rigs):
    from gateway import agent
    rig = rigs({})
    task_id = await rig.create(description="найди рецепт борща", steps=5)
    refused = await agent.set_agent_effort(task_id, agent.MORE, "не тянет", caller="task:someone1")
    assert refused == agent.NOT_CREATOR.format(id=task_id, creator=agent.agent_info(task_id)["creator"])
    assert "нет" in (await agent.set_agent_effort("nope", agent.MORE))


async def test_more_effort_moves_the_agent_to_the_next_dearer_model_and_counts_a_failure(rigs, monkeypatch):
    from gateway import agent, llm_router, model_picker
    rig = rigs({})
    task_id = await rig.create(description="найди рецепт борща", steps=5)
    agent._db.tasks.update_one({"id": {"$regex": f"^{task_id}"}}, {"$set": {"source": "polza", "model": "glm"}})

    async def recommend(*a, **k):
        return {"ranking": [{"source": "polza", "model": "glm", "est_cost_usd": 0.001},
                            {"source": "polza", "model": "deepseek", "est_cost_usd": 0.01}]}

    async def source_config(source, model=""):
        return {"source": source, "model": model}

    resumed = []

    async def resume(deps, full_id, message, cfg):
        resumed.append(cfg["model"])
    monkeypatch.setattr(model_picker, "recommend", recommend)
    monkeypatch.setattr(llm_router, "source_config", source_config)
    monkeypatch.setattr(agent._worker_runtime, "resume_worker_task", resume)
    result = await agent.set_agent_effort(task_id, agent.MORE, "не тянет")
    await rig.until(lambda: resumed)
    assert "deepseek" in result and resumed == ["deepseek"]
    info = agent.agent_info(task_id)
    assert info["model"] == "polza:deepseek" and info["effort_changes"] == 1
    run = agent._db["_task_runs"].find_one({"task_id": {"$regex": f"^{task_id}"}, "status": agent.ESCALATED})
    assert run and run["model"] == "glm"


async def test_more_effort_after_a_failed_run_does_not_count_the_failure_twice(rigs, monkeypatch):
    from datetime import datetime
    from gateway import agent, llm_router, model_picker
    rig = rigs({})
    task_id = await rig.create(description="найди рецепт борща", steps=5)
    full = agent._find_task(task_id)["id"]
    agent._db.tasks.update_one({"id": full}, {"$set": {"source": "polza", "model": "glm"}})
    agent._db["_task_runs"].insert_one({"task_id": full, "model": "glm", "status": "unverified",
                                        "finished_at": datetime.utcnow()})

    async def recommend(*a, **k):
        return {"ranking": [{"source": "polza", "model": "glm", "est_cost_usd": 0.001},
                            {"source": "polza", "model": "deepseek", "est_cost_usd": 0.01}]}

    async def source_config(source, model=""):
        return {"source": source, "model": model}

    async def resume(*a):
        pass
    monkeypatch.setattr(model_picker, "recommend", recommend)
    monkeypatch.setattr(llm_router, "source_config", source_config)
    monkeypatch.setattr(agent._worker_runtime, "resume_worker_task", resume)
    await agent.set_agent_effort(task_id, agent.MORE)
    assert agent._db["_task_runs"].count_documents({"task_id": full, "model": "glm"}) == 1
