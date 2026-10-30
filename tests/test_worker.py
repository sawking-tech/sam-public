"""Воркер шлюза (gateway/worker.py) на настоящем цикле, задачах и сессиях воркеров в mongomock, свежей шине;
подменены модель и часы ожидания ответа создателя."""
import asyncio
from datetime import datetime, timedelta

import pytest

from gateway import tasks, worker, worker_session
from gateway.tools.agents import CREATOR_MAIN_MODEL, QUESTION_SOURCE
from tests.fakes_agents import RUN_WAIT_S, WorkerRig, _polled, _resp, _tc, ask, hang, llm_cfg, report

CFG = llm_cfg()
PARENT = "voice"


def _deps(**kw):
    """Воркер для соседних групп (test_off_event_loop): WorkerDeps на тех же настоящих модулях."""
    return WorkerRig(**kw).deps


def _act(ran):
    async def act(args):
        ran.append(args)
        return "калькулятор запущен"
    return act


async def _run_task(rig, task_id, cfg=CFG):
    short = task_id[:8]
    rig.handles[task_id] = "handle"
    rig.context[f"task:{short}"] = (PARENT, short)     # так задачу регистрирует запуск в шлюзе
    await worker.run_worker_task(rig.deps, task_id, "открой калькулятор", "pc", cfg, PARENT)
    return short


def _facts(description, limit):
    return "калькулятор - calc.exe"


def _broken_rag(description, limit):
    raise RuntimeError("qdrant down")


@pytest.mark.parametrize("rag, cfg, result, in_system", [
    (_facts, CFG, "калькулятор открыт", "ИЗ ПАМЯТИ (факты по теме задачи):\nкалькулятор - calc.exe"),
    (_broken_rag, CFG, "калькулятор открыт", "task_id="),          # RAG упал - работа идёт без фактов
    (None, None, "LLM не инициализирован", None),                  # нет модели - итог-ошибка
], ids=["with_facts", "rag_broken", "no_model"])
async def test_worker_task_runs_to_done_and_result_reaches_creator(rag, cfg, result, in_system):
    ran = []
    rig = WorkerRig([_resp(tool_calls=[_tc("c1", "act", {"app": "calc"})]), report("r1", "калькулятор открыт")],
                    rag=rag, tools={"act": _act(ran)})
    task_id = rig.new_task()
    short = await _run_task(rig, task_id, cfg)

    assert rig.task(task_id)["status"] == "done" and rig.task(task_id)["result"] == result
    assert rig.responded == [(short, result, PARENT)]
    assert [e["status"] for e in rig.bus.of("stage_update")] == ["running", "done", "running", "done"]
    assert rig.bus.of("task_done")[0]["task_id"] == short
    saved = worker_session.load_session(rig.db, task_id)
    assert saved["status"] == "idle" and saved["parent_session"] == PARENT
    assert rig.handles == {} and rig.context == {}
    if in_system:
        system, user = saved["messages"][:2]
        assert in_system in system["content"] and user["content"] == "[Устройство: pc] открой калькулятор"
        assert ran == [{"app": "calc"}]


@pytest.mark.parametrize("verify, status, in_result", [
    (lambda description, result: "итог пустой", "unverified", "[НЕ ПОДТВЕРЖДЕНО] итог пустой"),
    (lambda description, result: None, "done", "калькулятор открыт"),
    (None, "done", "калькулятор открыт"),
], ids=["rejected", "accepted", "no_contract"])
async def test_result_without_an_artifact_is_unverified_not_done(verify, status, in_result):
    rig = WorkerRig([report("r1", "калькулятор открыт")])
    rig.deps.verify_result = verify
    task_id = rig.new_task()
    short = await _run_task(rig, task_id)
    task = rig.task(task_id)
    assert task["status"] == status and in_result in task["result"]
    assert rig.responded[0][0] == short and in_result in rig.responded[0][1]


def _answer_then_fail(rig, session):
    def step(kwargs):
        rig.bus.inject(session, "ромашка")    # ответ создателя лёг, пока модель падала
        raise RuntimeError("модель упала")
    return step


def _never(rig, session):
    return hang


@pytest.mark.parametrize("resume, step, error", [
    (False, _never, "Timeout 0.01s"),
    (False, _answer_then_fail, "модель упала"),
    (True, _never, "Timeout resume 0.01s"),
], ids=["deadline", "model_fails_answer_not_reanimating", "resume_deadline_dead"])
async def test_worker_failure_marks_the_task(resume, step, error):
    rig = WorkerRig(timeout=0.01)
    task_id = rig.new_task()
    rig.llm.queue(step(rig, f"task:{task_id[:8]}"))
    if resume:
        worker_session.save_session(rig.db, task_id, device="pc", description="x", messages=[],
                                    parent_session=PARENT)
        await worker.resume_worker_task(rig.deps, task_id, "дальше", CFG)
        assert worker_session.load_session(rig.db, task_id)["status"] == "dead"
    else:
        await _run_task(rig, task_id)
        assert rig.responded[0][1].startswith("[TASK_FAILED]") and rig.bus.of("task_failed")
        assert worker_session.load_session(rig.db, task_id) is None
    assert rig.task(task_id)["status"] == "failed" and error in rig.task(task_id)["error"]
    assert rig.reanimated == []


async def test_finished_worker_is_reanimated_with_its_history():
    rig = WorkerRig([report("r1", "первый итог"), report("r2", "второй итог")])
    rig.deps.fallback_session_id = lambda: "не та сессия"
    task_id = rig.new_task()
    short = await _run_task(rig, task_id)
    history = worker_session.load_session(rig.db, task_id)["messages"]

    await worker.resume_worker_task(rig.deps, task_id, "продолжай", CFG)

    sent = rig.llm.calls[-1]["messages"]
    assert sent[:len(history)] == history and sent[len(history)] == {"role": "user", "content": "[CONTINUE] продолжай"}
    assert rig.responded[-1] == (short, "второй итог", PARENT)          # родитель - из Mongo
    assert rig.task(task_id)["result"] == "второй итог"
    assert worker_session.load_session(rig.db, task_id)["status"] == "idle"


async def test_answer_between_final_and_release_reanimates_the_worker():
    """Ответ лёг как running_inject между итогом и снятием задачи - воскрешает агента, а не теряется."""
    rig = WorkerRig([report("r1", "итог")])
    task_id = rig.new_task()
    rig.deps.auto_respond = lambda short_id, *rest: rig.bus.inject(f"task:{short_id}", "ромашка")
    await _run_task(rig, task_id)
    assert rig.reanimated == [(task_id, "ромашка")]
    assert rig.bus.pop_inject(f"task:{task_id[:8]}") is None


async def test_resume_without_a_saved_session_does_nothing():
    rig = WorkerRig()
    await worker.resume_worker_task(rig.deps, "missing", "msg", CFG)
    assert rig.llm.calls == [] and rig.responded == [] and rig.bus.events == []


def _answer_while_writing_final(rig, session):
    def step(kwargs):
        rig.bus.inject(session, "ромашка")
        return report("r1", "Жду ответа")
    return step


@pytest.mark.parametrize("asks, answer_at, final, waited", [
    ("during_last_step", None, "ромашка", 0),     # ответ лёг, пока модель писала итог «жду» - тот же прогон
    ("plain", 30, "ромашка", 30),                 # ответ через 30 с - дождался и продолжил
    ("plain", None, "Жду ответа", 90),            # ответа нет - итог с вопросом по сроку
    (None, None, "готово", 0),                    # вопроса не было - без ожидания
], ids=["answer_during_last_step", "answer_after_30s", "no_answer_until_deadline", "no_question"])
async def test_worker_waits_for_creator_answer(asks, answer_at, final, waited):
    rig = WorkerRig(answer_wait_s=90)
    task_id = rig.new_task(creator=CREATOR_MAIN_MODEL)
    session = f"task:{task_id[:8]}"
    if asks is None:
        steps = [report("r0", "готово")]
    elif asks == "during_last_step":
        steps = [ask("q", "какое слово?"), _answer_while_writing_final(rig, session), report("r2", "ромашка")]
    else:
        steps = [ask("q", "какое слово?"), report("r1", "Жду ответа"), report("r2", "ромашка")]
    rig.llm.queue(*steps)
    rig.clock.session, rig.clock.answer_at, rig.clock.answer = session, answer_at, "ромашка"

    await _run_task(rig, task_id)

    assert rig.task(task_id)["result"] == final and rig.clock.now == waited
    assert set(rig.clock.slept) <= {worker.ANSWER_POLL_S}
    if final == "ромашка":
        assert {"role": "user", "content": f"{worker.INJECT_PREFIX} ромашка"} in rig.llm.calls[-1]["messages"]
    if asks:
        assert rig.delivered[0]["source"] == QUESTION_SOURCE and rig.delivered[0]["session"] == session
        assert [q["text"] for q in rig.task(task_id)["questions"]] == ["какое слово?"]


WORKER_ONLY = ("mcp_call", "search_web", "claude_delegate")


async def test_main_model_turn_has_no_worker_only_tools(monkeypatch, tmp_path):
    """workers 24: устройства, поиск и агент кода - у воркера; главный ход (агент шлюза с main_tool_names из
    корня композиции) их не видит, а вызов по имени не исполняется. Подменены модель и исполнители этих трёх."""
    from gateway import llm_client
    from tests.fakes_agents import FakeLLMClient, GatewayRig
    gw = GatewayRig(monkeypatch, tmp_path)
    ran = []
    for name in WORKER_ONLY:
        monkeypatch.setitem(gw.agent.TOOL_IMPL, name, _act(ran))
    llm = FakeLLMClient([_resp(tool_calls=[_tc(f"c-{n}", n, {"query": "x"}) for n in WORKER_ONLY]),
                         _resp(content="Готово.")])
    monkeypatch.setattr(llm_client, "make_llm_call", llm.make_llm_call)
    names = gw.agent._get_runner_deps().main_tool_names
    await gw.agent._agent_loop([{"role": "system", "content": "Ты SAM."}, {"role": "user", "content": "найди"}],
                               CFG, names, session_id="chat-1")
    assert ran == []
    assert "spawn_agent" in llm.tools_seen[0] and not set(WORKER_ONLY) & set(llm.tools_seen[0])
    assert set(WORKER_ONLY) <= set(gw.agent.WORKER_TOOL_NAMES)          # воркер их получает


async def test_answer_without_a_real_call_is_rejected_by_the_gateway_tool():
    """workers 5: настоящий report_status шлюза отклоняет «ОТВЕТ:», пока воркер не сделал ни одного вызова, кроме
    отчётов; после действия тот же «ОТВЕТ:» заканчивает задачу итогом."""
    from gateway import agent

    async def gateway_report(args):
        return agent._impl_report_status(args)
    ran = []
    rig = WorkerRig([report("r0", "калькулятор открыт"), _resp(tool_calls=[_tc("c1", "act", {"app": "calc"})]),
                     report("r1", "калькулятор открыт")], tools={"report_status": gateway_report, "act": _act(ran)})
    task_id = rig.new_task()
    await _run_task(rig, task_id)

    rejected = rig.llm.calls[1]["messages"][-1]["content"]
    assert rejected.startswith("[REJECT]") and ran == [{"app": "calc"}]
    assert rig.task(task_id)["status"] == "done" and rig.task(task_id)["result"] == "калькулятор открыт"


async def test_answer_without_any_action_never_becomes_the_result():
    """workers 5: воркер, который только отчитывается «ОТВЕТ:», выдуманного итога не выдаёт."""
    from gateway import agent

    async def gateway_report(args):
        return agent._impl_report_status(args)
    rig = WorkerRig([report(f"r{i}", "калькулятор открыт") for i in range(6)] + [_resp("сделала")],
                    tools={"report_status": gateway_report})
    task_id = rig.new_task()
    await _run_task(rig, task_id)

    assert rig.task(task_id)["result"] != "калькулятор открыт"


@pytest.mark.parametrize("result, says, hidden", [
    ("[STOP_WORKER]", "Не получилось с задачей", "[STOP_WORKER]"),
    ("[REJECT] Ты не сделал НИ ОДНОГО реального tool call", "Не получилось с задачей", "[REJECT]"),
    ("[SAM] Превышен лимит шагов агента.", "Не получилось с задачей", "Превышен лимит"),
    ("калькулятор открыт", "Результат от воркера: калькулятор открыт", "Не получилось"),
], ids=["stop_marker", "reject", "step_limit", "real_result"])
async def test_service_marks_of_a_result_are_not_retold(monkeypatch, result, says, hidden):
    """workers 7: итог со служебной меткой основная модель получает как короткое «не получилось» без самой метки;
    настоящий итог - как есть. Подменён ход основной модели (её ответ) и вывод в голос."""
    import asyncio

    from gateway import agent
    turns, spoken = [], []

    async def main_turn(message, **kwargs):
        turns.append(message)
        return "Не вышло, попробуем иначе?"
    monkeypatch.setattr(agent.sam_agent, "run", main_turn)
    monkeypatch.setattr(agent, "speak_worker_result", lambda session, voice: spoken.append((session, voice)))
    agent._auto_respond("abcd1234", result, "voice", CFG)
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])

    assert len(turns) == 1 and turns[0].startswith("[TASK_DONE:abcd1234]")
    assert says in turns[0] and hidden not in turns[0]
    assert spoken == [("voice", "Не вышло, попробуем иначе?")]


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def _answer_each_step(rig, session, step_no):
    def step(kwargs):
        rig.bus.inject(session, f"ответ {step_no}")      # создатель отвечает, пока модель пишет итог
        return report(f"r{step_no}", f"итог {step_no}")
    return step


async def test_answers_continue_the_run_at_most_answer_rounds_times(monkeypatch):
    """workers 10: продолжение с ответом создателя - не больше SAM_AGENT_ANSWER_ROUNDS раз за прогон; ответ сверх
    лимита остаётся в очереди и воскрешает агента после итога."""
    monkeypatch.setattr(worker, "MAX_ANSWER_ROUNDS", 2)
    rig = WorkerRig()
    task_id = rig.new_task()
    session = f"task:{task_id[:8]}"
    rig.llm.queue(*(_answer_each_step(rig, session, n) for n in range(1, 5)))
    await _run_task(rig, task_id)
    assert len(rig.llm.calls) == 3 and rig.task(task_id)["result"] == "итог 3"
    assert rig.reanimated == [(task_id, "ответ 3")]


TTL_SLACK = timedelta(minutes=1)


def _expires_in(task) -> timedelta:
    return task["expires_at"] - datetime.utcnow()


async def test_task_record_outlives_a_dead_gateway_and_cleans_itself_up():
    """stand 46, workers 14: задача лежит в Mongo с начала работы - шлюз упал посреди хода воркера, запись осталась;
    срок записи по статусу: не запустилась - 15 мин, идёт или упала - 2 ч, выполнена - 24 ч."""
    rig = WorkerRig([hang])
    task_id = rig.new_task()
    assert abs(_expires_in(rig.task(task_id)) - timedelta(minutes=15)) < TTL_SLACK
    run = asyncio.ensure_future(_run_task(rig, task_id))
    await asyncio.wait_for(_polled(lambda: rig.llm.calls), RUN_WAIT_S)
    record = rig.db.tasks.find_one({"id": task_id})                    # процесс шлюза гибнет здесь
    assert record["status"] == "running" and abs(_expires_in(record) - timedelta(hours=2)) < TTL_SLACK
    run.cancel()
    for status, term in (("done", timedelta(hours=24)), ("failed", timedelta(hours=2))):
        tasks.update_task_status(rig.db, task_id, status)
        assert abs(_expires_in(rig.task(task_id)) - term) < TTL_SLACK
    tasks.ensure_indexes(rig.db)                                        # так задачу заводит запуск в шлюзе
    index = {next(iter(i["key"])): i for i in rig.db.tasks.list_indexes()}["expires_at"]
    assert index["expireAfterSeconds"] == 0                             # Mongo удаляет запись к её сроку


def test_idle_worker_session_expires_a_day_after_its_last_activity():
    """workers 13: сессия воркера удаляется TTL-индексом по last_activity через 24 ч; работа сессии сдвигает срок."""
    rig = WorkerRig()
    worker_session.ensure_indexes(rig.db)
    index = {i["name"]: i for i in rig.db.worker_sessions.list_indexes()}["ttl_last_activity"]
    assert index["expireAfterSeconds"] == 24 * 3600 and dict(index["key"]) == {"last_activity": 1}
    worker_session.save_session(rig.db, "t" * 32, device="pc", description="x", messages=[], parent_session=PARENT)
    old = datetime.utcnow() - timedelta(hours=23)
    rig.db.worker_sessions.update_many({}, {"$set": {"last_activity": old}})
    worker_session.mark_running(rig.db, "t" * 32)
    assert worker_session.load_session(rig.db, "t" * 32)["last_activity"] > old + timedelta(hours=22)


def test_agent_timeout_is_configurable_and_used_by_the_gateway(monkeypatch):
    """Срок задачи агента из SAM_AGENT_TIMEOUT_S, шлюз берёт его же, а не зашитые 300 с."""
    import importlib
    from gateway import worker as worker_module
    monkeypatch.setenv("SAM_AGENT_TIMEOUT_S", "1234")
    reloaded = importlib.reload(worker_module)
    try:
        assert reloaded.AGENT_TIMEOUT_S == 1234
        assert reloaded.WorkerDeps.__dataclass_fields__["timeout_seconds"].default == 1234
    finally:
        monkeypatch.delenv("SAM_AGENT_TIMEOUT_S")
        importlib.reload(worker_module)
    from gateway import agent
    assert agent._get_worker_deps().timeout_seconds == worker_module.AGENT_TIMEOUT_S
