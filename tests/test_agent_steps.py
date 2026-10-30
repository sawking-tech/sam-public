"""Шаги агента (ADR-022, «Бюджет дерева»): пул - меньшая из оценок создателя и агента, общий на дерево, по просьбе
создатель выдаёт ещё. Настоящий цикл шлюза и настоящий учёт шагов; подменена только модель."""
import dataclasses
import pathlib
import json
from types import SimpleNamespace as NS

from gateway import agent
from gateway.agent_budget import POOL, REDUCED, Budgets
from gateway.agent_loop import STEP_LIMIT_REPLY, run_agent_loop


def _resp(content=None, calls=()):
    tcs = [NS(id=f"c{i}", type="function", function=NS(name=n, arguments=json.dumps(a)))
           for i, (n, a) in enumerate(calls)]
    message = NS(content=content, tool_calls=tcs or None, role="assistant")
    return NS(choices=[NS(message=message, finish_reason="tool_calls" if tcs else "stop")], usage=None)


class _Llm:
    """Модель, которая всё время зовёт инструмент; видит, что ей подсказали."""

    def __init__(self):
        self.prompts = []

    async def make_llm_call(self, cfg, **kw):
        self.prompts.append(" ".join(str(m.get("content") or "") for m in kw.get("messages")))
        return _resp(calls=[("list_capabilities", {"x": len(self.prompts)})])

    def _effective_adapter(self, cfg):
        return "openai"

    def inc_parse_fail(self):
        pass


async def _run(budgets, session="task:ab12"):
    llm = _Llm()
    deps = dataclasses.replace(agent._get_agent_loop_deps(), llm_client=llm, step_budget=lambda s: budgets.step(s[5:]))
    out, _ = await run_agent_loop(deps, [{"role": "user", "content": "x"}], {"source": "test", "model": "m"},
                                  ["list_capabilities"], max_turns=50, session_id=session)
    return out, llm


async def test_the_pool_runs_out_with_a_warning_and_a_grant_continues_the_work():
    budgets = Budgets()
    budgets.open("ab12", 5)
    out, llm = await _run(budgets)
    assert out == STEP_LIMIT_REPLY and len(llm.prompts) == 5
    assert any("Шаги почти кончились" in p for p in llm.prompts)   # подсказка на доле пула
    assert budgets.grant("", "ab12", 3)[0]                       # создатель-корень выдал ещё
    out, llm = await _run(budgets)
    assert len(llm.prompts) == 3


def test_pool_is_the_smaller_estimate_and_a_child_takes_from_its_parent():
    budgets = Budgets()
    budgets.open("pp00", 40)                      # прикидка создателя
    assert budgets.estimate("pp00", 25) == POOL.format(pool=25)   # агент оценил меньше - пул по нему
    _, note = budgets.open("ch11", 10, parent="pp00")
    assert note == REDUCED.format(child="ch11", steps=10, left=15) and budgets.left("pp00") == 15
    ok, text = budgets.grant("pp00", "ch11", 100)
    assert not ok and "15" in text                # родитель не отдаст больше своего остатка
    budgets.close("ch11")                         # ребёнок закончил, ничего не истратив
    assert budgets.left("pp00") == 25


def test_a_turn_of_channel_calls_costs_no_step_and_channels_have_their_own_quota(monkeypatch):
    # Вопросы ребёнка и ответы создателя шагов не жгут, а бесплатное ограничено своей квотой (защита от абуза)
    from gateway import agent_budget
    monkeypatch.setattr(agent_budget, "CHANNEL_CALLS", 2)
    budgets = Budgets()
    budgets.open("ab12", 5)
    budgets.step("ab12")
    budgets.turn_done("ab12", ["ask_creator"])
    assert budgets.left("ab12") == 5                         # оборот одних вопросов шаг вернул
    budgets.step("ab12")
    budgets.turn_done("ab12", ["ask_creator", "list_capabilities"])
    assert budgets.left("ab12") == 4                         # с делом - шаг потрачен
    assert budgets.channel("ab12") is None and budgets.channel("ab12") is None
    assert "кончились" in budgets.channel("ab12")            # сверх квоты - отказ без вызова


def test_main_model_gives_no_more_than_the_cap_and_default_fills_a_missing_estimate(monkeypatch):
    """stand 37, workers 16: без прикидки пул - SAM_AGENT_STEPS; основная модель (корень) не выдаёт больше
    SAM_AGENT_STEPS_CAP ни при создании, ни добавкой; агент-родитель ограничен только своим остатком."""
    from gateway import agent_budget
    monkeypatch.setattr(agent_budget, "DEFAULT_STEPS", 7)
    monkeypatch.setattr(agent_budget, "CAP_STEPS", 20)
    budgets = Budgets()
    assert budgets.open("dflt", None)[0] == 7
    assert budgets.open("big0", 500)[0] == 20
    ok, text = budgets.grant("", "dflt", 21)
    assert not ok and "20" in text and budgets.left("dflt") == 7
    assert budgets.grant("", "dflt", 20)[0] and budgets.left("dflt") == 27


def test_worker_loop_safety_equals_the_pool_cap():
    """workers 23: страховка max_turns цикла воркера равна потолку пула - работу ограничивает пул задачи, а не
    прежние 12 оборотов."""
    from gateway import agent_budget
    assert agent._get_worker_deps().max_turns == agent_budget.CAP_STEPS


def test_estimate_adds_the_steps_already_done_before_the_turn():
    """SAM-209: оценка - сколько ещё нужно, считая текущий оборот; шаги до него (load_skill) в пул входят сами,
    иначе оценка 3 после одного шага оставляла два."""
    budgets = Budgets()
    budgets.open("ap01", 30)
    budgets.step("ap01")                          # оборот 1: load_skill
    budgets.step("ap01")                          # оборот 2: оценка вместе с первым действием
    assert budgets.estimate("ap01", 3) == POOL.format(pool=4)
    assert budgets.left("ap01") == 2              # этот оборот и ещё два
    budgets.open("ap02", 2)
    budgets.step("ap02")
    budgets.step("ap02")
    assert budgets.estimate("ap02", 3) == POOL.format(pool=2)   # не больше выделенного


SKILL = pathlib.Path(__file__).resolve().parents[1] / ".claude" / "skills" / "make-app" / "SKILL.md"


def test_make_app_skill_starts_with_app_make_and_checks_after():
    """SAM-209: первое действие скилла для страницы - app_make, проверка и образцы - после него."""
    text = SKILL.read_text(encoding="utf-8")
    sections = text.split("\n## ")[1:]
    assert sections[0].startswith("1.") and "app_make" in sections[0].splitlines()[0]
    assert "sandbox_run" not in sections[0] and "awesome-selfhosted" not in sections[0]


async def test_app_worker_on_an_estimate_of_three_reaches_app_make_and_reports():
    """SAM-209: воркер по скиллу make-app - load_skill, оценка 3 вместе с app_make, проверка, итог - на настоящем
    цикле воркера и настоящем учёте шагов доходит до app_make и отчитывается. Подменены модель и исполнители."""
    import functools

    from gateway import agent_loop as al
    from gateway import worker
    from tests.fakes_agents import WorkerRig, _resp as resp, _tc, report

    budgets, made = Budgets(), []

    def short_id() -> str:
        return (worker.worker_session_var.get(None) or "")[len("task:"):]

    async def load_skill(args):
        return SKILL.read_text(encoding="utf-8")

    async def estimate(args):
        return budgets.estimate(short_id(), int(args["steps"]))

    async def app_make(args):
        made.append(args["name"])
        return "Приложение создано: страница /apps/t1/"

    async def sandbox_run(args):
        return "код выхода 0"

    page = {"name": "Температура", "command": "python -m http.server 8000", "files": {"index.html": "<canvas>"}}
    rig = WorkerRig([
        resp(tool_calls=[_tc("c0", "load_skill", {"name": "make-app"})]),
        resp(tool_calls=[_tc("c1", "estimate_steps", {"steps": 3}), _tc("c2", "app_make", page)]),
        resp(tool_calls=[_tc("c3", "sandbox_run", {"command": "curl -s localhost:8000"})]),
        report("r4", "приложение готово"),
    ], tools={"load_skill": load_skill, "estimate_steps": estimate, "app_make": app_make, "sandbox_run": sandbox_run})
    loop = dataclasses.replace(rig.loop, step_budget=lambda s: budgets.step(s[len("task:"):]),
                               turn_done=lambda s, tools: budgets.turn_done(s[len("task:"):], tools))
    rig.deps = dataclasses.replace(rig.deps, agent_loop=functools.partial(al.run_agent_loop, loop))
    task_id = rig.new_task("Сделай живое приложение: график температуры за неделю")
    short = task_id[:8]
    budgets.open(short, None)
    rig.handles[task_id] = "handle"
    rig.context[f"task:{short}"] = ("voice", short)
    await worker.run_worker_task(rig.deps, task_id, "Сделай живое приложение: график температуры за неделю", "pc",
                                 {"source": "test", "model": "m"}, "voice")

    assert made == ["Температура"]
    assert rig.task(task_id)["result"] == "приложение готово"
