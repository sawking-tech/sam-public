"""Агенты SAM в сессиях Cortex (ADR-022, SAM-133): права, запрос права и выдача, ребёнок агента, вопросы и итоги по
цепочке создателей, конец задачи. Настоящий шлюз по HTTP и CortexWorkers из корня композиции, процесс Cortex - ACP-пара
с настоящим CortexAgent; подменены модель агентов, MCP Cortex и часы ожидания входящих."""
import pytest

from gateway import cortex_workers as cw
from gateway.tools import agents as agents_tool
from tests.fakes_agents import AgentsRig, Gate, _resp, _tc

CORTEX = agents_tool.FRAMEWORK_CORTEX
MCP = {"radar_where": "Ирина в зале", "get_current_time": "20:15", "run_command": "C: 124 ГБ свободно"}


@pytest.fixture
async def rigs(monkeypatch, tmp_path):
    made = []

    def make(routes, **kw):
        made.append(AgentsRig(monkeypatch, tmp_path, routes, mcp_tools=MCP, **kw))
        return made[-1]
    yield make
    for rig in made:
        await rig.close()


def _call(name, **args):
    return _resp(tool_calls=[_tc(f"c-{name}", name, args)])


def _say(text):
    return _resp(content=text)


def _fed(call) -> list[str]:
    """Итоги инструментов, которые модель получила к этому шагу."""
    return [m["content"] for m in call["messages"] if m.get("role") == "tool"]


# ─── Запрос права и выдача ────────────────────────────────────────────────────

DISK = "проверь место на диске"


async def test_cortex_permission_request_reaches_creator_and_grant_works(rigs):
    gate = Gate(_call("radar_where"))
    rig = rigs({DISK: [_call("request_permission", tool="run_command", why="проверить диск"), gate,
                       _call("run_command", command="dir"), _say("Свободно 124 ГБ.")]})
    short = await rig.create(description=DISK, framework=CORTEX, tools=["radar_where"])
    await rig.until(gate.entered.is_set)
    [question] = rig.inbox_of(agents_tool.QUESTION_SOURCE)
    assert f"grant_agent_tool(task_id={short}, tool=run_command)" in question["text"]

    granted = await rig.post(f"/sam/agents/{short}/grant", {"tool": "run_command"}, privileged=True)
    assert granted.status_code == 200 and granted.json()["granted"]
    itself = await rig.post(f"/sam/agents/{short}/grant", {"tool": f"agent:{short}"}, privileged=True)
    assert itself.status_code == 409 and "самому себе" in itself.json()["detail"]
    await rig.post("/sam/inject", {"session_id": f"task:{short}", "message": "выдала run_command"})   # ответ на вопрос
    gate.open.set()

    assert (await rig.finished(short))["result"] == "Свободно 124 ГБ."
    calls = rig.llm.by_task[DISK]
    assert "run_command" not in [t["function"]["name"] for t in calls[1]["tools"]]
    assert "run_command" in [t["function"]["name"] for t in calls[2]["tools"]]       # со следующего шага
    assert ("run_command", {"command": "dir"}) in rig.mcp.calls
    closed = await rig.post(f"/sam/agents/{short}/grant", {"tool": "radar_where"}, privileged=True)
    assert closed.status_code == 409 and "не идёт" in closed.json()["detail"]


async def test_cortex_grant_between_turns_reaches_the_next_turn(rigs):
    rig = rigs({DISK: [_call("request_permission", tool="run_command", why="диск"), _say("Жду права."),
                       _say("Свободно 124 ГБ.")]})
    short = await rig.create(description=DISK, framework=CORTEX, tools=["radar_where"])
    await rig.until(lambda: "session/close" in rig.pair.methods())
    granted = await rig.post(f"/sam/agents/{short}/grant", {"tool": "run_command"}, privileged=True)
    assert granted.json()["granted"]          # сессии нет - право копится; закрывающейся - выдано ей и копится тоже
    await rig.post("/sam/inject", {"session_id": f"task:{short}", "message": "выдала run_command"})
    await rig.finished(short)
    first, second = rig.pair.metas()
    assert first["tools"] == ["radar_where"] and second["tools"] == ["radar_where", "run_command"]


# ─── Ребёнок агента ───────────────────────────────────────────────────────────

PARENT, CHILD = "узнай время у ребёнка", "скажи время"


def _spawn(description=CHILD, tools="get_current_time"):
    return _call("spawn_agent", description=description, tools=tools)


async def test_main_model_agent_creates_one_child_and_child_result_wakes_it(rigs):
    rig = rigs({PARENT: [_spawn(), _say("Жду ребёнка."), _say("Сейчас 20:15.")],
                CHILD: [_call("get_current_time"), _say("20:15")]})
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["radar_where", "get_current_time"],
                              depth=1)
    assert (await rig.finished(parent))["result"] == "Сейчас 20:15."
    [child] = [t for t in rig.db.tasks.find() if t["id"][:8] != parent]
    short = child["id"][:8]
    assert child["creator"] == f"task:{parent}" and child["tools"] == ["get_current_time"] and child["status"] == "done"
    assert "started_at" in child   # отмечена идущей: срок жизни pending её не удалит посреди работы
    assert "spawn_agent" not in [t["function"]["name"] for t in rig.llm.by_task[CHILD][0]["tools"]]   # последний уровень
    assert f"[TASK_DONE:{short}] Агент закончил: 20:15" in str(rig.llm.by_task[PARENT][-1]["messages"])
    assert f"Агент {short} создан" in _fed(rig.llm.by_task[PARENT][1])[0]
    assert [m["session"] for m in rig.inbox_of(agents_tool.DONE_SOURCE)] == [f"task:{parent}"]   # итог ребёнка - родителю


@pytest.mark.parametrize("steps, refusals, children", [
    ([_spawn(tools="get_current_time, run_command")], ["шире твоих прав"], 0),                 # шире выдачи
    ([_resp(tool_calls=[_tc("a", "spawn_agent", {"description": CHILD, "tools": "get_current_time"}),
                        _tc("b", "spawn_agent", {"description": f"{CHILD} ещё", "tools": "get_current_time"})])],
     ["Инструмента spawn_agent нет"], 1),                                                     # два в шаге - один
    ([_spawn(), _spawn(description=f"{CHILD} ещё раз")], ["Инструмента spawn_agent нет"], 1),  # второй ребёнок
], ids=["wider_than_own", "parallel_spawns", "second_child"])
async def test_agent_gets_one_child_no_wider(rigs, steps, refusals, children):
    rig = rigs({PARENT: [*steps, _say("Готово.")], CHILD: [_say("20:15")]})
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["radar_where", "get_current_time"],
                              depth=1)
    await rig.until(lambda: len(rig.llm.by_task[PARENT]) == len(steps) + 1)
    fed = [c for call in rig.llm.by_task[PARENT][1:] for c in _fed(call)]
    assert all(any(r in c for c in fed) for r in refusals)
    assert rig.db.tasks.count_documents({"creator": f"task:{parent}"}) == children


async def test_failed_spawn_frees_the_place(rigs, monkeypatch):
    def room_again(kwargs):                                     # второй шаг: воркер освободился
        monkeypatch.setattr(rig.agent, "MAX_WORKERS", 4)
        return _spawn(description=f"{CHILD} ещё раз")
    rig = rigs({PARENT: [_spawn(), room_again, _say("Готово.")], CHILD: [_say("20:15")]})
    monkeypatch.setattr(rig.agent, "MAX_WORKERS", 1)          # первый ребёнок не влезает: место занял родитель
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["get_current_time"], depth=1)
    await rig.finished(parent)
    first, second = (_fed(c)[-1] for c in rig.llm.by_task[PARENT][1:3])
    assert first.startswith("Агент не создан: все 1 воркера заняты") and "создан;" in second
    assert rig.db.tasks.count_documents({"creator": f"task:{parent}"}) == 1


def _rogue_spawns(rig, requests, answers):
    """Шаг модели родителя, на котором агент шлёт событие spawn в обход проверок Cortex: шлюз получает его тем же
    расширением `_sam/agent_event`, что и от честного Cortex, но права сессии никто до него не проверял."""
    async def step(kwargs):
        [*_, prompt] = [m for m in rig.pair.wire if m.get("method") == "session/prompt"]
        for request in requests:
            answers.append(await rig.workers.on_agent_event(
                {"session_id": prompt["params"]["sessionId"], "kind": cw.SPAWN, "description": CHILD, **request}))
        return _say("Готово.")
    return step


@pytest.mark.parametrize("depth, requests, refused, children", [
    (0, [{"tools": ["get_current_time"]}], [cw.NO_MORE_CHILDREN], 0),                         # права на детей нет
    (1, [{"tools": ["get_current_time"]}, {"tools": ["get_current_time"]}], [cw.NO_MORE_CHILDREN], 1),   # второй
    (1, [{"tools": ["run_command"]}], [cw.WIDER_CHILD.format(names="run_command")], 0),      # шире выдачи
    (1, [{"tools": ["get_current_time"], "depth": 5}], [], 1),                                  # глубина урезается
], ids=["no_right", "second_child", "wider_than_own", "depth_capped"])
async def test_gateway_rechecks_tree_limits_by_its_own_task_record(rigs, depth, requests, refused, children):
    """agents 36: Cortex проверяет пределы по сессии, но агент мимо Cortex дошёл бы до шлюза сам - шлюз
    перепроверяет по своей записи задачи: право на детей, их число, выдачу не шире родителя, глубину."""
    answers = []
    rig = rigs({PARENT: [], CHILD: [_say("20:15")]})
    rig.llm.routes[PARENT] = [_rogue_spawns(rig, requests, answers), _say("Готово."), _say("Готово.")]
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["radar_where", "get_current_time"],
                              depth=depth)
    await rig.finished(parent)
    texts = [a["result"] for a in answers]
    assert [t for t in texts if not t.startswith("Агент ")] == refused
    assert rig.db.tasks.count_documents({"creator": f"task:{parent}"}) == children
    kids = [meta for meta in rig.pair.metas() if meta.get(cw.CREATOR_KEY) == f"task:{parent}"]
    assert len(kids) == children and all(cw.DEPTH_KEY not in meta for meta in kids)   # внуков создавать нельзя


# ─── Вопрос ребёнка, ответ родителя, сироты ───────────────────────────────────

async def test_child_question_goes_to_its_parent_and_parent_answers_by_send_to_agent(rigs):
    def child_of(parent):
        return rig.db.tasks.find_one({"creator": f"task:{parent}"})["id"][:8]

    def answer(kwargs):                                         # родитель отвечает ребёнку по его id
        return _call("send_to_agent", to=child_of(parent), message="ЧЧ:ММ")
    child_waits = Gate(_say("Жду формат."))
    rig = rigs({PARENT: [_spawn(), _say("Жду ребёнка."), answer, _say("Жду итог."), _say("Сейчас 20:15.")],
                CHILD: [_call("ask_creator", question="какой формат?"), child_waits, _say("20:15")]})
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["get_current_time"], depth=1)
    await rig.until(child_waits.entered.is_set)
    child_waits.open.set()
    assert (await rig.finished(parent))["result"] == "Сейчас 20:15."
    short = child_of(parent)
    parent_saw = str([c["messages"] for c in rig.llm.by_task[PARENT]])
    assert f"[вопрос агента {short}] какой формат?" in parent_saw and f"send_to_agent(to={short}" in parent_saw
    assert f"[от агента {parent}] ЧЧ:ММ" in str([c["messages"] for c in rig.llm.by_task[CHILD]])
    assert rig.inbox_of(agents_tool.QUESTION_SOURCE) == []      # вопрос ребёнка до основной модели не дошёл


async def test_child_permission_request_names_grant_child_and_parent_grants_only_its_own(rigs):
    def child_of(parent):
        return rig.db.tasks.find_one({"creator": f"task:{parent}"})["id"][:8]

    def grant(kwargs):                                          # выдать своему, попробовать чужому, ответить ребёнку
        child = child_of(parent)
        return _resp(tool_calls=[_tc("g1", "grant_child", {"task_id": child, "tool": "radar_where"}),
                                 _tc("g2", "grant_child", {"task_id": "zz99", "tool": "radar_where"}),
                                 _tc("s1", "send_to_agent", {"to": child, "message": "выдала radar_where"})])
    child_waits = Gate(_call("get_current_time"))
    rig = rigs({PARENT: [_spawn(), _say("Жду ребёнка."), grant, _say("Выдала."), _say("Сейчас 20:15.")],
                CHILD: [_call("request_permission", tool="radar_where", why="где Ирина"), child_waits,
                        _call("radar_where"), _say("20:15")]})
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["radar_where", "get_current_time"],
                              depth=1)
    await rig.until(lambda: len(rig.llm.by_task[PARENT]) >= 4)       # родитель выдал и ответил
    child_waits.open.set()
    await rig.finished(parent)
    short = child_of(parent)
    assert f"grant_child(task_id={short}, tool=radar_where)" in str(rig.llm.by_task[PARENT][2]["messages"])
    fed = _fed(rig.llm.by_task[PARENT][3])
    assert "Выдано" in fed and "Задача zz99 - не твой потомок: выдавать можно только своим" in fed
    assert "radar_where" in [t["function"]["name"] for t in rig.llm.by_task[CHILD][2]["tools"]]
    assert rig.inbox_of(agents_tool.QUESTION_SOURCE) == []


async def test_orphan_question_and_result_rise_to_the_nearest_live_creator(rigs):
    child_asks = Gate(_call("ask_creator", question="какой формат?"))
    rig = rigs({PARENT: [_spawn(), _say("Жду ребёнка.")], CHILD: [child_asks, _say("20:15")]})
    parent = await rig.create(description=PARENT, framework=CORTEX, tools=["get_current_time"], depth=1)
    await rig.until(child_asks.entered.is_set)
    await rig.out_of_time(parent)                               # родитель не дождался и закрылся
    child_asks.open.set()
    short = rig.db.tasks.find_one({"creator": f"task:{parent}"})["id"][:8]
    await rig.until(lambda: rig.inbox_of(agents_tool.QUESTION_SOURCE))
    [question] = rig.inbox_of(agents_tool.QUESTION_SOURCE)      # вопрос сироты - основной модели
    assert question["session"] == f"task:{short}" and "какой формат?" in question["text"]
    await rig.out_of_time(short)
    sessions = [m["session"] for m in rig.inbox_of(agents_tool.DONE_SOURCE)]
    assert sorted(sessions) == sorted([f"task:{parent}", f"task:{short}"])     # итог сироты - тоже


# ─── Конец задачи ─────────────────────────────────────────────────────────────

async def test_cortex_agent_whose_process_died_is_failed_and_reported(rigs):
    rig = rigs({DISK: []})

    def dies(kwargs):
        rig.pair.kill()
        return _say("не дойдёт")
    rig.llm.routes[DISK].append(dies)
    short = await rig.create(description=DISK, framework=CORTEX, tools=["radar_where"])
    task = await rig.finished(short)
    assert task["status"] == "failed" and "агент Cortex не закончил" in task["result"]
    [done] = rig.inbox_of(agents_tool.DONE_SOURCE)
    assert "агент Cortex не закончил" in done["text"]


async def test_cortex_task_cancelled_by_its_creator_is_closed(rigs):
    gate = Gate(_say("не дойдёт"))
    rig = rigs({DISK: [gate]})
    short = await rig.create(description=DISK, framework=CORTEX, tools=["radar_where"])
    await rig.until(gate.entered.is_set)
    r = await rig.http.post(f"/sam/tasks/{short}/cancel", json={})
    assert r.json()["cancelled_handle"]
    task = await rig.finished(short)
    assert "агент отменён" in task["result"]
    assert "агент отменён" in rig.inbox_of(agents_tool.DONE_SOURCE)[0]["text"]
    assert f"task:{short}" not in rig.context


async def test_unanswered_question_holds_the_task_until_the_deadline(rigs):
    rig = rigs({DISK: [_call("ask_creator", question="какой диск?"), _say("Спросила, жду.")]})
    short = await rig.create(description=DISK, framework=CORTEX, tools=["radar_where"])
    await rig.until(lambda: "session/close" in rig.pair.methods())
    assert rig.task(short)["status"] not in ("done", "failed")    # ход кончился, задача ждёт ответ
    task = await rig.out_of_time(short)
    assert task["result"] == "Спросила, жду." and len(rig.llm.by_task[DISK]) == 2


async def test_turns_of_a_task_are_capped(rigs, monkeypatch):
    monkeypatch.setattr(cw, "MAX_TURNS", 2)
    rig = rigs({DISK: [_call("ask_creator", question="какой диск?"), _say("ход 1"),
                       _call("ask_creator", question="а ещё?"), _say("ход 2")]})
    short = await rig.create(description=DISK, framework=CORTEX, tools=["radar_where"])
    await rig.until(lambda: rig.pair.methods().count("session/close") == 1)
    await rig.post("/sam/inject", {"session_id": f"task:{short}", "message": "C:"})
    await rig.until(lambda: rig.pair.methods().count("session/close") == 2)
    await rig.post("/sam/inject", {"session_id": f"task:{short}", "message": "и D:"})   # ходов больше нет
    task = await rig.finished(short)
    assert task["result"] == "ход 2" and len(rig.pair.metas()) == 2


# ─── Живой создатель по цепочке ───────────────────────────────────────────────

_CREATORS = {"aa11": "task:bb22", "bb22": "task:cc33", "cc33": "main_model", "dd44": "task:dd44"}


@pytest.mark.parametrize("creator, running, expected", [
    ("task:aa11", {"task:aa11"}, "task:aa11"),    # создатель идёт - ему
    ("task:aa11", {"task:cc33"}, "task:cc33"),    # создатель и его создатель кончились - выше
    ("task:aa11", set(), "main_model"),           # никого живого - основной модели
    ("task:dd44", set(), "main_model"),           # петля в цепочке
    ("task:zz99", set(), "main_model"),           # задачи создателя нет
    ("orchestrator", set(), "orchestrator"),      # создатель не агент - он сам
])
def test_live_creator_walks_up_to_the_nearest_running(creator, running, expected):
    assert agents_tool.live_creator(creator, running.__contains__, lambda s: _CREATORS.get(s, "")) == expected
