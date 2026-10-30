"""Прогонщик голосовых случаев (SAM-81) на поддельном ходе: выполненное и нарушенное ожидание видны."""
from types import SimpleNamespace

from gateway import evals
from gateway.evals import EvalCase


def _turn(answers):
    async def _run(case):
        voice, tools = answers[case.case_id]
        if voice is None:
            raise RuntimeError("Все LLM провайдеры недоступны")
        return SimpleNamespace(voice=voice, tools=tools)
    return _run


async def test_eval_run_reports_met_and_broken_expectations():
    cases = [EvalCase("open", "открой браузер", expect_spawn=True),
             EvalCase("hello", "привет", expect_spawn=False),
             EvalCase("promise", "открой почту", expect_spawn=True),
             EvalCase("cat", "Нина, иди сюда", expect_silent=True),
             EvalCase("ask", "какой сегодня день", expect_silent=False),
             EvalCase("down", "раз"),
             EvalCase("after", "два")]
    out = await evals.run_cases(cases, _turn({
        "open": ("Открываю.", ["spawn_agent"]),
        "hello": ("Привет!", []),
        "promise": ("Сейчас открою почту.", []),
        "cat": ("Иду!", []),
        "ask": ("", []),
        "down": (None, []),                 # упавший ход валит свой случай, прогон идёт дальше
        "after": ("ок", []),
    }))
    failures = {c.case_id: o.failures for c, o in zip(cases, out)}
    assert failures["promise"] == ["ждали spawn_agent", "обещание действия без spawn_agent"]
    assert failures["cat"] == ["ждали молчание"] and failures["ask"] == ["ответа нет"]
    assert "ход упал" in failures["down"][0]
    report = evals.summary(out)
    assert report["passed"] == 3 and [cid for cid, _ in report["failed"]] == ["promise", "cat", "ask", "down"]
    assert evals.summary([]) == {"cases": 0, "passed": 0, "failed": [], "latency_ms_median": 0, "latency_ms_max": 0}


def test_cases_from_session_history():
    docs = [
        {"_id": 1, "session_id": "s", "role": "user", "content": "открой браузер"},
        {"_id": 2, "session_id": "s", "role": "tool_call", "tool_name": "spawn_agent"},
        {"_id": 3, "session_id": "s", "role": "assistant", "content": "<voice>Открываю</voice>"},
        {"_id": 4, "session_id": "s", "role": "user", "content": "[TASK_DONE:abcd] готово"},
        {"_id": 5, "session_id": "s", "role": "user", "content": "спасибо"},
        {"_id": 6, "session_id": "s", "role": "assistant", "content": "<voice>Пожалуйста</voice>"},
    ]
    cases = evals.cases_from_sessions(docs)
    assert [(c.phrase, c.expect_spawn) for c in cases] == [("открой браузер", True), ("спасибо", False)]
