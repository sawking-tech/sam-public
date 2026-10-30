"""Набор голосовых случаев для прогона (SAM-81): обобщённые фразы без личного, ожидание проверяет код.

Боевой набор собирается из истории Mongo (gateway.evals.cases_from_sessions, scripts/eval_voice.py)
и лежит вне git; эти случаи - образец формы и проверка прогонщика.
"""
from gateway.evals import EvalCase

VOICE_EVAL_CASES = (
    EvalCase("greeting", "привет, как дела", expect_spawn=False, expect_silent=False),
    EvalCase("open-browser", "открой браузер", expect_spawn=True),
    EvalCase("search", "найди прогноз погоды на завтра", expect_spawn=True),
    EvalCase("arithmetic", "сколько будет семь умножить на восемь", expect_spawn=False, expect_silent=False),
    EvalCase("thanks", "спасибо", expect_spawn=False),
)
