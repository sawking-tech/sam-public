"""Цикл агента (gateway/agent_loop.py) на настоящих guard, tool_policy, provider_transform, tool_text_parse и свежей
шине; подменена только модель хода и модель судьи."""
import dataclasses
import json
import os
import subprocess
import sys

import pytest

from gateway import agent_loop as al
from gateway import llm_client, runaway, worker
from gateway.tools import apps as apps_tool
from tests.fakes_agents import FakeLLMClient, _resp, _tc, llm_cfg, loop_deps, policy
from tests.test_env import _REPO, _SYSTEM_VARS, IMPORT_TIMEOUT_S

# Соседние группы собирают цикл через эти имена
_deps = loop_deps
__all__ = ["FakeLLMClient", "_deps", "_resp", "_tc"]

USER = [{"role": "user", "content": "?"}]
IMAGE = "data:image/png;base64,iVBORw0KGgo="


def _returns(value):
    async def impl(args):
        return value
    return impl


def _recording(ran, name, value="ok"):
    async def impl(args):
        ran.append((name, args))
        return value
    return impl


def _raises(exc):
    async def impl(args):
        raise exc
    return impl


async def _run(deps, tools, messages=None, cfg=None, **kw):
    return await al.run_agent_loop(deps, list(messages or USER), cfg or llm_cfg(), list(tools), **kw)


# ──────────────── Ответ текстом ────────────────

@pytest.mark.parametrize("replies, max_turns, expected", [
    ([_resp(content="Привет!")], 20, "Привет!"),
    ([_resp(content="<think>рассуждаю</think>Ответ")], 20, "Ответ"),
    ([_resp(content=""), _resp(content="ОК")], 20, "ОК"),                     # пустой ответ - короткий повтор
    ([_resp(tool_calls=[_tc(f"c{i}", "echo", {"i": i})]) for i in range(5)], 3, al.STEP_LIMIT_REPLY),
], ids=["plain", "thinking_cut", "empty_asks_short", "steps_exhausted"])
async def test_loop_answers_text(replies, max_turns, expected):
    deps = loop_deps(llm_client=FakeLLMClient(replies), tool_impl={"echo": _returns("result")})
    text, _ = await _run(deps, ["echo"], max_turns=max_turns)
    assert text == expected


# ──────────────── Вызовы инструментов ────────────────

def _answer_by_report(deps):
    """report_status с «ОТВЕТ:» - как в шлюзе: ответ в общий holder, цикл останавливается."""
    async def report(args):
        worker.set_last_answer(args["message"][6:].strip(), deps.worker_last_answer_var)
        return al.STOP_WORKER_MARKER
    return report


TOOL_CASES = {
    # имя: (шаги модели, инструменты(ran, deps), ожидаемые вызовы, что модель получила по id, итог)
    "one_call": (
        [[_tc("c1", "search", {"q": "x"})]],
        lambda ran, deps: {"search": _recording(ran, "search", "found: 42")},
        [("search", {"q": "x"})], {"c1": "found: 42"}, "итог"),
    "parallel": (
        [[_tc("c1", "a", {}), _tc("c2", "b", {})]],
        lambda ran, deps: {"a": _recording(ran, "a", "A"), "b": _recording(ran, "b", "B")},
        [("a", {}), ("b", {})], {"c1": "A", "c2": "B"}, "итог"),
    "unknown_tool": (
        [[_tc("c1", "nonexistent", {})]], lambda ran, deps: {},
        [], {"c1": "Инструмента nonexistent нет"}, "итог"),
    "tool_raises": (
        [[_tc("c1", "boom", {})]], lambda ran, deps: {"boom": _raises(RuntimeError("kaboom"))},
        [], {"c1": "Ошибка boom: kaboom"}, "итог"),
    "args_broken_json": (
        [[_tc("c1", "spawn_agent", '{"description": "open')]],
        lambda ran, deps: {"spawn_agent": _recording(ran, "spawn_agent")},
        [], {"c1": "не разобраны"}, "итог"),
    "args_not_object": (
        [[_tc("c1", "spawn_agent", '["open"]')]],
        lambda ran, deps: {"spawn_agent": _recording(ran, "spawn_agent")},
        [], {"c1": "не разобраны"}, "итог"),
    "args_empty": (
        [[_tc("c1", "spawn_agent", "")]],
        lambda ran, deps: {"spawn_agent": _recording(ran, "spawn_agent")},
        [("spawn_agent", {})], {"c1": "ok"}, "итог"),
    "action_repeat_dropped": (
        [[_tc("c1", "act", {"x": 1})], [_tc("c2", "act", {"x": 1})]],
        lambda ran, deps: {"act": _recording(ran, "act", "did it")},
        [("act", {"x": 1})], {"c1": "did it", "c2": "дублирующий"}, "итог"),
    "read_repeats": (
        [[_tc("c1", "search_web", {"q": "x"})], [_tc("c2", "search_web", {"q": "x"})]],
        lambda ran, deps: {"search_web": _recording(ran, "search_web", "results")},
        [("search_web", {"q": "x"})] * 2, {"c1": "results", "c2": "results"}, "итог"),
    "answer_by_report_in_parallel": (
        [[_tc("c1", "report_status", {"message": "ОТВЕТ: калькулятор открыт"}), _tc("c2", "noop", {})]],
        lambda ran, deps: {"report_status": _answer_by_report(deps), "noop": _recording(ran, "noop")},
        [("noop", {})], {}, "калькулятор открыт"),
}


@pytest.mark.parametrize("steps, tools, expected_ran, fed, final", TOOL_CASES.values(), ids=TOOL_CASES.keys())
async def test_loop_runs_tools_and_feeds_results(steps, tools, expected_ran, fed, final):
    llm = FakeLLMClient([_resp(tool_calls=calls) for calls in steps] + [_resp(content="итог")])
    deps, ran = loop_deps(llm_client=llm), []
    deps.tool_impl.update(tools(ran, deps))
    deps.worker_last_answer_var.set({"text": None})   # так делает run_worker
    text, used = await _run(deps, deps.tool_impl)
    assert text == final
    assert sorted(ran, key=str) == sorted(expected_ran, key=str)
    seen = {m["tool_call_id"]: m["content"] for call in llm.calls for m in call["messages"] if m.get("role") == "tool"}
    for call_id, piece in fed.items():
        assert piece in seen[call_id]
    assert {c.function.name for calls in steps for c in calls} <= used
    assert "tool_result" in deps.event_bus.names()


# ──────────────── Предел ответа (SAM-210) ────────────────

CUT_PAGE = '{"name": "temp-week", "files": {"index.html": "<html><body><script>const days = ['


@pytest.mark.parametrize("finish, cut", [("length", True), ("tool_calls", False), (None, False)],
                         ids=["cut_by_limit", "finished_call", "no_finish_reason"])
async def test_loop_names_truncated_call(monkeypatch, finish, cut):
    """Обрезанный пределом вызов: модель получает причину с пределом и советом инструмента, а не «повтори JSON»."""
    monkeypatch.setattr(al, "MAX_TOKENS_TOOLS", 4321)
    llm = FakeLLMClient([_resp(tool_calls=[_tc("c1", "app_make", CUT_PAGE)], finish=finish), _resp(content="итог")])
    deps, ran = loop_deps(llm_client=llm), []
    deps.tool_impl["app_make"] = _recording(ran, "app_make")
    deps.split_hints = apps_tool.SPLIT_HINTS
    await _run(deps, ["app_make"])
    fed = next(m["content"] for m in llm.calls[1]["messages"] if m.get("tool_call_id") == "c1")
    assert ran == []
    assert (fed == f"Вызов app_make обрезан по длине ответа (предел 4321 токенов): {apps_tool.SPLIT_ADVICE}.") is cut
    assert ("не разобраны" in fed) is not cut


async def test_loop_truncated_call_without_hint_gets_general_advice():
    """Совет про файлы приложения - только инструментам приложений; остальным - разделить аргументы."""
    llm = FakeLLMClient([_resp(tool_calls=[_tc("c1", "remember", '{"text": "дли')], finish="length"),
                         _resp(content="итог")])
    deps = loop_deps(llm_client=llm, tool_impl={"remember": _returns("ok")})
    deps.split_hints = apps_tool.SPLIT_HINTS
    await _run(deps, ["remember"])
    fed = next(m["content"] for m in llm.calls[1]["messages"] if m.get("tool_call_id") == "c1")
    assert fed == f"Вызов remember обрезан по длине ответа (предел {al.MAX_TOKENS_TOOLS} токенов): {al.SPLIT_ADVICE}."


TEXT_CUT_CASES = {
    # имя: (обрезанный текст модели, ожидаемая причина)
    "named_call": ('<tool_call>{"name": "app_make", "arguments": {"files": {"index.html": "<html>',
                   f"[SYSTEM] Вызов app_make обрезан по длине ответа (предел 4321 токенов): {apps_tool.SPLIT_ADVICE}."),
    "no_name": ("Сейчас соберу страницу, вот код: <html><body>",
                f"[SYSTEM] Ответ обрезан по длине (предел 4321 токенов): {al.SPLIT_ADVICE}."),
}


@pytest.mark.parametrize("text, reason", TEXT_CUT_CASES.values(), ids=TEXT_CUT_CASES.keys())
async def test_loop_truncated_text_call_gets_reason(monkeypatch, text, reason):
    """Вызов текстом (custom, Harmony, qwen в LM Studio), обрезанный пределом: обрезка не уходит ответом,
    модель получает причину с именем инструмента, если оно видно."""
    monkeypatch.setattr(al, "MAX_TOKENS_TOOLS", 4321)
    llm = FakeLLMClient([_resp(content=text, finish="length"), _resp(content="итог")])
    deps = loop_deps(llm_client=llm, tool_impl={"app_make": _returns("ok"), "app": _returns("ok")})
    deps.split_hints = apps_tool.SPLIT_HINTS
    answer, _ = await _run(deps, ["app_make", "app"])
    assert answer == "итог"
    sent = llm.calls[1]["messages"]
    assert sent[-1] == {"role": "user", "content": reason}
    assert all(m.get("content") != text for m in sent)


async def test_loop_truncated_text_in_answer_only_turn_is_the_answer():
    """В ходе только с ответом (force_finish) обрезанный текст - последнее, что есть: он и уходит."""
    same = [_resp(tool_calls=[_tc(f"c{i}", "echo", {"i": i})]) for i in range(5)]
    llm = FakeLLMClient(same + [_resp(content="длинный ответ, обре", finish="length")])
    deps = loop_deps(llm_client=llm, tool_impl={"echo": _returns("r")})
    answer, _ = await _run(deps, ["echo"])
    assert answer == "длинный ответ, обре"


async def test_loop_whole_call_at_limit_runs():
    """Вызов, который разобрался, исполняется и при finish=length: обрезки аргументов не было."""
    llm = FakeLLMClient([_resp(tool_calls=[_tc("c1", "app_make", {"name": "x"})], finish="length"),
                         _resp(content="итог")])
    deps, ran = loop_deps(llm_client=llm), []
    deps.tool_impl["app_make"] = _recording(ran, "app_make", "создано")
    await _run(deps, ["app_make"])
    assert ran == [("app_make", {"name": "x"})]


async def test_loop_limit_tools_turn_and_text_turn(monkeypatch):
    """Ход с инструментами - MAX_TOKENS_TOOLS, ход только с ответом (tool_choice="none") - MAX_TOKENS_TEXT."""
    monkeypatch.setattr(al, "MAX_TOKENS_TOOLS", 8000)
    monkeypatch.setattr(al, "MAX_TOKENS_TEXT", 1000)
    same = [_resp(tool_calls=[_tc(f"c{i}", "echo", {"i": i})]) for i in range(5)]
    llm = FakeLLMClient(same + [_resp(content="итог")])
    deps = loop_deps(llm_client=llm, tool_impl={"echo": _returns("r")})
    await _run(deps, ["echo"])
    assert [(c["tool_choice"], c["max_tokens"]) for c in llm.calls] == [("auto", 8000)] * 5 + [("none", 1000)]


@pytest.mark.parametrize("env, expected", [({}, (8192, 2048)),
                                           ({"SAM_LOOP_MAX_TOKENS": "12000", "SAM_LOOP_MAX_TOKENS_TEXT": "900"},
                                            (12000, 900))], ids=["default", "from_env"])
def test_loop_limits_come_from_env(env, expected):
    """Пределы читаются из окружения при импорте; процесс отдельный, чтобы не перезагружать модуль другим тестам."""
    code = "import gateway.agent_loop as m; print(m.MAX_TOKENS_TOOLS, m.MAX_TOKENS_TEXT)"
    clean = {k: os.environ[k] for k in _SYSTEM_VARS if k in os.environ}
    clean.update({"MONGODB_URI": "mongodb://127.0.0.1:1", "PYTHONIOENCODING": "utf-8", **env})
    res = subprocess.run([sys.executable, "-c", code], cwd=_REPO, capture_output=True, text=True, encoding="utf-8",
                         env=clean, timeout=IMPORT_TIMEOUT_S)
    assert res.returncode == 0, res.stderr
    assert tuple(map(int, res.stdout.split()[-2:])) == expected


async def test_loop_puts_image_after_all_tool_results():
    """OpenAI API требует ответы на все вызовы сразу за assistant; user с картинкой - после них."""
    llm = FakeLLMClient([_resp(tool_calls=[_tc("c1", "shot", {}), _tc("c2", "other", {})]),
                         _resp(content="вижу экран")])
    deps = loop_deps(llm_client=llm, tool_impl={"shot": _returns(f"screen {IMAGE}"), "other": _returns("B")})
    await _run(deps, ["shot", "other"], cfg=llm_cfg(model="gpt-4o"))
    sent = llm.calls[1]["messages"]
    start = next(i for i, m in enumerate(sent) if m.get("tool_calls"))
    assert [m["role"] for m in sent[start + 1:start + 4]] == ["tool", "tool", "user"]
    assert sent[start + 3]["content"][1]["image_url"]["url"] == IMAGE


# ──────────────── Защита ────────────────

UNSAFE_CASES = {
    # имя: (вызов, правила политики, ответ судьи или None - судья не зовётся, событие guard_block)
    "pattern_rm_rf": (("shell", {"cmd": "rm -rf / now"}), (), None),
    "pattern_case_insensitive": (("shell", {"cmd": "RM -RF /home"}), (), None),
    "pattern_drop_table": (("shell", {"sql": "DROP TABLE users"}), (), None),
    "high_risk_judge_unsafe": (("claude_delegate", {"task": "x"}), (), "UNSAFE"),
    "policy_deny": (("set_env", {"key": "GEMINI_API_KEY", "value": "x"}), (("set_env(*)", "deny"),), None),
    "policy_ask_low_risk_to_judge": (
        ("mcp_call", {"device": "pc", "tool": "powershell", "args": {"script": "Remove-Item C:/ -Recurse"}}),
        (("mcp_call(powershell:*)", "ask"),), "Думаю, это опасно UNSAFE"),
}


@pytest.mark.parametrize("native", [True, False], ids=["native", "from_text"])
@pytest.mark.parametrize("call, rules, verdict", UNSAFE_CASES.values(), ids=UNSAFE_CASES.keys())
async def test_loop_blocks_unsafe_calls(call, rules, verdict, native):
    name, args = call
    step = (_resp(tool_calls=[_tc("c1", name, args)]) if native
            else _resp(content=f'<tool_call>{{"name": "{name}", "arguments": {json.dumps(args)}}}</tool_call>'))
    judge = FakeLLMClient([_resp(content=verdict)] if verdict else [])
    ran = []
    deps = loop_deps(llm_client=FakeLLMClient([step, _resp(content="не стала")]), judge=judge,
                     tool_impl={name: _recording(ran, name)}, policy=policy(*rules))
    text, _ = await _run(deps, [name])
    assert ran == [] and text == "не стала"
    assert len(judge.calls) == (1 if verdict else 0)
    assert deps.event_bus.of("guard_block")[0]["tool"] == name


async def test_loop_judge_lets_a_safe_risky_call_run():
    ran = []
    judge = FakeLLMClient([_resp(content="SAFE")])
    deps = loop_deps(llm_client=FakeLLMClient([_resp(tool_calls=[_tc("c1", "claude_delegate", {"task": "x"})]),
                                               _resp(content="сделала")]),
                     judge=judge, tool_impl={"claude_delegate": _recording(ran, "claude_delegate")})
    assert (await _run(deps, ["claude_delegate"]))[0] == "сделала"
    assert ran == [("claude_delegate", {"task": "x"})] and "guard_pass" in deps.event_bus.names()


# ──────────────── Нет прогресса ────────────────

async def test_loop_hints_after_three_same_calls():
    llm = FakeLLMClient([_resp(tool_calls=[_tc(f"c{i}", "act", {"i": i})]) for i in range(5)]
                        + [_resp(content="finish")])
    deps = loop_deps(llm_client=llm, tool_impl={"act": _returns("nothing useful")})
    await _run(deps, ["act"], max_turns=10)
    assert deps.event_bus.of("loop_guard")[0]["streak"] == 3
    # подсказка годится любому инструменту: имя и число вызовов подряд, без «результатов поиска»
    assert llm.calls[3]["messages"][-1] == {"role": "user", "content": "Ты вызываешь act 3-й раз подряд; результаты "
                                                                       "выше - используй их или ответь."}


@pytest.mark.parametrize("tool, pending_answer", [
    ("spawn_agent", True),    # фоновая работа хода ещё идёт - ответ статусом без модели
    ("noop", False),          # без фоновой работы модель зовётся за ответом
])
async def test_loop_finishes_without_progress(tool, pending_answer):
    same = [_resp(tool_calls=[_tc(f"c{i}", tool, {"description": "место"})]) for i in range(al.NO_PROGRESS_LIMIT + 2)]
    llm = FakeLLMClient(same + [_resp(content="Свободно 124 ГБ.")] * 3)
    deps = dataclasses.replace(loop_deps(llm_client=llm, tool_impl={tool: _returns("Задача abc создана.")}),
                               pending_tools=frozenset({"spawn_agent"}), pending_reply="Ещё проверяю.")
    text, used = await _run(deps, [tool])
    assert "no_progress" in deps.event_bus.names() and tool in used
    assert (text == "Ещё проверяю.") is pending_answer
    assert any(c.get("tool_choice") == "none" for c in llm.calls) is not pending_answer


# ──────────────── Поток и инъекции ────────────────

async def test_loop_takes_an_inject_into_the_next_step():
    llm = FakeLLMClient([_resp(content="ok")])
    deps = loop_deps(llm_client=llm)
    deps.event_bus.inject("sess1", "вот тебе подсказка")
    await _run(deps, [], session_id="sess1")
    assert {"role": "user", "content": f"{worker.INJECT_PREFIX} вот тебе подсказка"} in llm.calls[0]["messages"]


async def test_loop_speaks_harmony_channels_and_stops():
    llm = FakeLLMClient([_resp(content="ok")])
    await _run(loop_deps(llm_client=llm), [], messages=[{"role": "system", "content": "You are SAM"}],
               cfg=llm_cfg(prompt_format="harmony"))
    assert "Valid channels" in llm.calls[0]["messages"][0]["content"]
    assert llm.calls[0]["stop"] == ["<|call|>", "<|return|>"]


async def test_loop_stream_reset_reaches_the_monitor():
    """SAM-73: поставщик сменился посреди стрима - монитор получает сброс показанного."""
    class _Resetting(FakeLLMClient):
        async def make_llm_call(self, llm_config, **kwargs):
            kwargs["on_delta"]("При")
            kwargs["on_reset"]()
            return await super().make_llm_call(llm_config, **kwargs)

    deps = loop_deps(llm_client=_Resetting([_resp(content="Привет")]))
    await _run(deps, [], session_id="s1")
    names = deps.event_bus.names()
    assert names.index("assistant_delta") < names.index("assistant_stream_reset")
    assert deps.event_bus.of("assistant_stream_reset")[0]["session"] == "s1"


async def test_loop_lingering_tool_sends_its_whole_result_others_a_preview():
    body = "Скилл radar. " * 100
    llm = FakeLLMClient([_resp(tool_calls=[_tc("c1", "load_skill", {"name": "radar"}), _tc("c2", "radar_where", {})]),
                         _resp(content="Готово.")])
    deps = dataclasses.replace(loop_deps(llm_client=llm, tool_impl={"load_skill": _returns(body),
                                                                    "radar_where": _returns("кухня " * 1000)}),
                               linger_tools=frozenset({"load_skill"}))
    await _run(deps, ["load_skill", "radar_where"])
    results = {e["name"]: e for e in deps.event_bus.of("tool_result")}
    assert results["load_skill"]["content"] == body and results["load_skill"]["linger"]
    assert "content" not in results["radar_where"] and len(results["radar_where"]["result"]) == al.RESULT_PREVIEW
    assert results["radar_where"]["result_len"] == len("кухня " * 1000)   # монитор знает, сколько не пришло
    assert "result_len" not in results["load_skill"]


@pytest.mark.parametrize("source, model, sent", [("lmstudio", "qwen3", True), ("gemini", "gemini-2.5-flash", False)],
                         ids=["local_gets_penalties", "gemini_gets_none"])
async def test_loop_sends_repeat_penalties_only_where_accepted(source, model, sent):
    """Вызов модели из цикла: штрафы повторов уходят локальной модели, а Gemini - нет (иначе 400 на весь ход)."""
    llm = FakeLLMClient([_resp(content="готово")])
    deps = loop_deps(llm_client=llm)
    await _run(deps, [], cfg=llm_cfg(source=source, model=model))
    assert ("frequency_penalty" in llm.calls[0]) is sent and ("presence_penalty" in llm.calls[0]) is sent


# ──────────────── Шлюзовые инструменты: повтор по вызову, не по имени ────────────────

def _gateway_call(i, **extra):
    return _tc(f"c{i}", "mcp_call", {"device": "pc", "tool": "powershell", "args": {"script": f"Get-Item {i}", **extra}})


async def test_five_different_mcp_calls_in_a_row_are_work_not_a_loop():
    """28 окт: пять разных команд подряд через mcp_call закрывали ход агента как цикл, итог «Готово.»."""
    llm = FakeLLMClient([_resp(tool_calls=[_gateway_call(i)]) for i in range(6)] + [_resp(content="сделала")])
    deps = loop_deps(llm_client=llm, tool_impl={"mcp_call": _returns("ok")})
    text, _ = await _run(deps, ["mcp_call"], max_turns=12)
    assert text == "сделала" and deps.event_bus.of("loop_guard") == []
    assert not any(c.get("tool_choice") == "none" for c in llm.calls)


async def test_the_same_mcp_call_repeated_is_still_a_loop():
    llm = FakeLLMClient([_resp(tool_calls=[_tc(f"c{i}", "mcp_call", {"device": "pc", "tool": "tools/list"})]) for i in range(8)]
                        + [_resp(content="хватит")] * 3)
    deps = loop_deps(llm_client=llm, tool_impl={"mcp_call": _returns("same answer")})
    await _run(deps, ["mcp_call"], max_turns=12)
    assert deps.event_bus.of("loop_guard")[0]["streak"] == 3
    assert any(c.get("tool_choice") == "none" for c in llm.calls) or "no_progress" in deps.event_bus.names()


def test_loop_signature_separates_gateway_calls_by_arguments_and_leaves_other_tools_by_name():
    sig = lambda name, args: al.loop_signature(_tc("x", name, args))   # noqa: E731
    assert sig("mcp_call", {"tool": "a", "args": {"x": 1}}) != sig("mcp_call", {"tool": "a", "args": {"x": 2}})
    assert sig("mcp_call", {"tool": "a", "args": {"x": 1, "y": 2}}) == sig("mcp_call", {"args": {"y": 2, "x": 1}, "tool": "a"})
    assert sig("act", {"i": 1}) == sig("act", {"i": 2}) == "act"


# ──────────────── Обрыв разноса на лету (SAM-246) ────────────────

def _runaway_resp(content, sign=runaway.AGENT_TOO_LONG, detail="5000 знаков текста при пороге 1500"):
    resp = _resp(content=content, finish=llm_client.RUNAWAY_FINISH)
    resp.runaway = runaway.Runaway(sign, detail)
    return resp


async def test_agent_cut_for_long_text_is_told_to_write_files_and_retries():
    llm = FakeLLMClient([_runaway_resp("длинное рассуждение"), _resp(content="Готово, коммит abc1234.")])
    deps = loop_deps(llm_client=llm)
    final, _ = await _run(deps, [], session_id="task:x")
    assert final == "Готово, коммит abc1234."
    hint = llm.calls[1]["messages"][-1]["content"]
    assert hint.startswith("[SYSTEM] Твой ответ оборван и выброшен") and "файл" in hint
    assert "длинное рассуждение" not in str(llm.calls[1]["messages"])   # оборванное модели не возвращается


async def test_two_runaways_in_a_row_end_the_agent_without_a_model():
    llm = FakeLLMClient([_runaway_resp("раз"), _runaway_resp("два", sign=runaway.CJK, detail="иероглифы 40% знаков")])
    final, _ = await _run(loop_deps(llm_client=llm), [], session_id="task:x")
    assert final == al.RUNAWAY_REPLY.format(detail="иероглифы 40% знаков")


async def test_a_normal_turn_resets_the_runaway_count():
    llm = FakeLLMClient([_runaway_resp("раз"), _resp(tool_calls=[_tc("c1", "ping", {})]),
                         _runaway_resp("два"), _resp(content="Итог.")])
    deps = loop_deps(llm_client=llm, tool_impl={"ping": _returns("pong")})
    final, _ = await _run(deps, ["ping"], session_id="task:x")
    assert final == "Итог."


async def test_main_model_cut_goes_on_and_the_monitor_sees_a_stream_runaway():
    llm = FakeLLMClient([_runaway_resp("Начало ответа.", sign=runaway.TOO_LONG, detail="7 предложений при пороге 5")])
    deps = loop_deps(llm_client=llm)
    final, _ = await _run(deps, [], session_id="voice")
    assert final == "Начало ответа."
    event = deps.event_bus.of("generation_runaway")[0]
    assert event["stage"] == "stream" and event["sign"] == runaway.TOO_LONG and len(llm.calls) == 1


async def test_every_call_gets_a_stream_guard():
    llm = FakeLLMClient([_resp(content="ok")])
    await _run(loop_deps(llm_client=llm), [], session_id="task:x")
    assert callable(llm.calls[0]["stop_when"])
