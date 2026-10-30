"""Своя недавняя работа в ходе любой сессии (gateway/own_work.py): задачи и приложения из всех сессий."""
from datetime import datetime, timedelta

from gateway import own_work
from gateway.runner import build_messages
from tests.test_runner import _make_deps

NOW = datetime(2026, 10, 15, 10, 30)
TASKS = [
    {"id": "3b36bf39-aaaa", "name": "", "description": "Сделать простое приложение учёта еды", "status": "done",
     "created_at": NOW - timedelta(minutes=30), "result": "Приложение готово.\n Slug: calorie-tracker"},
    {"id": "olddone1-bbbb", "name": "old_job", "description": "позавчерашнее", "status": "done",
     "created_at": NOW - timedelta(hours=48), "result": "давно"},
]
APPS = [{"slug": "calorie-tracker", "name": "calorie_tracker", "wanted": "running", "created": "2026-10-15 16:02"}]


def test_summary_names_recent_tasks_with_outcome_and_apps_but_not_old_work():
    text = own_work.render(TASKS, APPS, NOW)
    assert text.startswith(own_work.HEADER)
    assert "задача 3b36bf39 Сделать простое приложение учёта еды · done · Приложение готово. Slug: calorie-tracker" in text
    assert "приложение calorie-tracker «calorie_tracker» · running · /apps/calorie-tracker/" in text
    assert "olddone1" not in text
    assert own_work.render([], [], NOW) == ""


def test_broken_source_gives_no_summary_and_one_log_line():
    said = []

    def broken():
        raise RuntimeError("mongo down")
    collect = own_work.collector(broken, lambda: APPS, clock=lambda: NOW, log=said.append)
    assert collect() == "" and len(said) == 1 and "mongo down" in said[0]


def test_every_session_turn_gets_the_summary_in_its_system_part():
    import dataclasses
    deps = dataclasses.replace(_make_deps(), own_work=lambda: own_work.render(TASKS, APPS, NOW))
    messages = build_messages(deps, "voice", "как там трекер калорий?", [], "", {"prompt_format": "standard"})
    assert "calorie-tracker" in messages[0]["content"] and messages[0]["role"] == "system"
