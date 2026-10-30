"""Шаги агента кончились (SAM-212): создатель получает частичный итог, просьба о шагах всегда получает ответ -
выдачу, отказ или срок, каскадом через создателей; создатель извне выдаёт шаги через REST и MCP sam. Настоящие
воркер, цикл и учёт шагов; подменены модель, часы ожидания и ход основной модели."""
import asyncio
import dataclasses
import functools

from gateway import agent, agent_budget, worker, worker_partial
from gateway import agent_loop as al
from gateway.agent_budget import GRANTED, POOL, Budgets
from gateway.tools import agents as agents_tool
from tests.fakes_agents import GatewayRig, WorkerRig, _resp, _tc, report

PARENT = "voice"
CFG = {"source": "test", "model": "m"}
TASK = "найди лог упавшей задачи"


def _work(call_id, n):
    return _resp(tool_calls=[_tc(call_id, "probe", {"n": n})])


def _request(call_id, steps=2):
    return _resp(tool_calls=[_tc(call_id, agents_tool.REQUEST_STEPS_TOOL, {"steps": steps, "why": "нужен ещё запрос"})])


def _status(call_id, text):
    return _resp(tool_calls=[_tc(call_id, agents_tool.STATUS_TOOL, {"message": text})])


def _rig(replies, pool, budgets=None, **kw):
    """Воркер с настоящим учётом шагов: probe - рабочий инструмент, request_steps - просьба (ответ даёт тест)."""
    budgets = budgets or Budgets()

    async def probe(args):
        return f"коллекция tasks, запись {args['n']}"

    async def request_steps(args):
        return "Вопрос отправлен создателю"

    rig = WorkerRig(replies, tools={"probe": probe, agents_tool.REQUEST_STEPS_TOOL: request_steps}, **kw)
    loop = dataclasses.replace(rig.loop, step_budget=lambda s: budgets.step(s[len("task:"):]),
                               turn_done=lambda s, tools: budgets.turn_done(s[len("task:"):], tools))
    rig.deps = dataclasses.replace(rig.deps, agent_loop=functools.partial(al.run_agent_loop, loop), max_turns=50)
    task_id = rig.new_task(TASK, creator=agents_tool.CREATOR_MAIN_MODEL)
    budgets.open(task_id[:8], pool)
    return rig, task_id, budgets


async def _run(rig, task_id):
    short = task_id[:8]
    rig.handles[task_id] = "handle"
    rig.context[f"task:{short}"] = (PARENT, short)
    await worker.run_worker_task(rig.deps, task_id, TASK, "pc", CFG, PARENT)
    return short


async def test_out_of_steps_the_creator_gets_what_the_agent_managed():
    """Живой случай ba3ecaf7 и 20716c74: вместо «[SAM] Превышен лимит шагов агента.» - что успел, на чём встал, почему."""
    rig, task_id, _ = _rig([_status("s0", "подключилась к Mongo"), _work("w1", 1), _work("w2", 2)], pool=2)
    short = await _run(rig, task_id)

    result = rig.task(task_id)["result"]
    assert result.startswith(worker_partial.STOPPED) and al.STEP_LIMIT_REPLY not in result
    assert "подключилась к Mongo" in result and "коллекция tasks, запись 2" in result
    assert 'Встал на: probe({"n": 2})' in result
    assert rig.responded == [(short, result, PARENT)]


async def test_a_grant_while_waiting_continues_the_work():
    """Шаги кончились, просьба ждёт ответа: агент ждёт его, выдача продолжает работу до итога."""
    rig, task_id, budgets = _rig([_work("w1", 1), _request("r1"), _work("w2", 2), report("f", "нашла: таймаут")],
                                 pool=2)
    short = task_id[:8]

    async def creator_grants(seconds):
        rig.clock.now += seconds
        if rig.clock.now >= 10 and budgets.left(short) == 0:
            budgets.grant(agent_budget.ROOT, short, 2)
            rig.bus.inject(f"task:{short}", GRANTED.format(steps=2, reason="обоснованно"))
    rig.deps = dataclasses.replace(rig.deps, sleep=creator_grants)

    await _run(rig, task_id)
    assert rig.task(task_id)["result"] == "нашла: таймаут"


async def test_an_unanswered_request_ends_with_the_partial_result_and_says_so():
    rig, task_id, _ = _rig([_work("w1", 1), _request("r1"), _work("w2", 2)], pool=2, answer_wait_s=90)
    await _run(rig, task_id)
    result = rig.task(task_id)["result"]
    assert worker_partial.UNANSWERED in result and rig.clock.now == 90


def test_partial_result_keeps_the_newest_results_within_the_limit():
    calls = [{"role": "assistant", "tool_calls": [{"id": f"c{i}", "function": {"name": "probe", "arguments": "{}"}}]}
             for i in range(5)]
    messages = []
    for i, call in enumerate(calls):
        messages += [call, {"role": "tool", "tool_call_id": f"c{i}", "content": f"итог {i} " + "x" * 300}]
    text = worker_partial.partial_result(messages, limit=400)
    assert len(text) <= 400 and "итог 4" in text and "итог 0" not in text


def test_question_of_an_agent_created_in_a_gateway_turn_reaches_the_gateway_model():
    """Живой случай 20716c74: создатель orchestrator (spawn_agent из хода) - вопрос видел только монитор."""
    import mongomock
    from gateway import tasks
    db = mongomock.MongoClient()["sam_test"]
    task = tasks.new_task(TASK, "pc", creator="orchestrator")
    tasks.save_task(db, task)
    short, asked = task["id"][:8], []
    agents_tool.ask_creator_impl({"question": "дай 4 шага"}, PARENT, short, agents_tool.AskDeps(
        add_question=functools.partial(tasks.add_question, db), emit=lambda *a: None, deliver=lambda m: None,
        to_gateway_model=lambda *a: asked.append(a)))
    assert asked and asked[0][:2] == (short, PARENT) and "дай 4 шага" in asked[0][2]


async def test_gateway_model_grants_as_root_and_its_reply_reaches_the_agent(monkeypatch, tmp_path):
    """Ход модели в шлюзе идёт в чистом контексте: в контексте воркера grant_steps выдавал бы из остатка самого агента."""
    rig = GatewayRig(monkeypatch, tmp_path)
    budgets = Budgets()
    monkeypatch.setattr(agent, "_budgets", budgets)
    short = rig.running_task(creator="orchestrator")
    budgets.open(short, 1)

    class Turn:
        async def run(self, text, device_id=None, _system_call=False, persist_reply=True):
            return agent._impl_grant_steps({"task_id": short, "steps": 3}) and "Даю 3 шага"
    monkeypatch.setattr(agent, "sam_agent", Turn())

    session = agents_tool.task_session(short)
    token = worker.worker_session_var.set(session)    # вопрос задаётся из хода воркера
    try:
        agent._ask_gateway_model(short, PARENT, "дай 3 шага")
    finally:
        worker.worker_session_var.reset(token)
    for _ in range(100):
        await asyncio.sleep(0)
    got = []
    while (msg := rig.bus.pop_inject(session)) is not None:
        got.append(msg)
    assert budgets.left(short) == 4
    assert got == [GRANTED.format(steps=3, reason="по просьбе"), agents_tool.creator_reply("Даю 3 шага")]


def test_a_creator_short_of_steps_is_told_to_ask_its_own_creator():
    budgets = Budgets()
    budgets.open("pa01", 10)
    budgets.open("ch01", 8, parent="pa01")
    ok, text = budgets.grant("pa01", "ch01", 5)
    assert not ok and "request_steps(steps=3" in text


def test_pool_is_the_creators_estimate_when_the_agent_asks_for_more():
    budgets = Budgets()
    budgets.open("ag01", 10)
    budgets.step("ag01")
    assert budgets.estimate("ag01", 25) == POOL.format(pool=10)
    budgets.open("ag02", 10)
    budgets.step("ag02")
    assert budgets.estimate("ag02", 4) == POOL.format(pool=4)


def test_external_creator_grants_steps_only_with_the_privilege_key(monkeypatch, tmp_path):
    rig = GatewayRig(monkeypatch, tmp_path)
    budgets = Budgets()
    monkeypatch.setattr(agent, "_budgets", budgets)
    short = rig.running_task()
    budgets.open(short, 2)
    assert rig.client.post(f"/sam/agents/{short}/steps", json={"steps": 3}).status_code == 401
    r = rig.client.post(f"/sam/agents/{short}/steps", json={"steps": 3, "reason": "по делу"}, headers=rig.privileged)
    assert r.status_code == 200 and budgets.left(short) == 5
    assert rig.bus.pop_inject(agents_tool.task_session(short)) == GRANTED.format(steps=3, reason="по делу")
    closed = rig.client.post("/sam/agents/zz000000/steps", json={"steps": 3}, headers=rig.privileged)
    assert closed.status_code == 409 and "закончил" in closed.json()["detail"]
