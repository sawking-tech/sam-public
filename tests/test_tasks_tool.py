"""get_tasks (gateway/tools/tasks.py) на настоящем gateway.tasks поверх mongomock."""
from datetime import datetime, timedelta

import pytest

from gateway import tasks
from gateway.tools import tasks as tasks_tool

TASKS = [
    # (id, статус, описание, возраст в минутах, устройство)
    ("freshrun", "running", "свежая задача", 5, "workpc"),
    ("stuckrun", "running", "застрявшая задача", 120, "workpc"),
    ("vikarun1", "pending", "задача на ноутбуке", 5, "pc2"),
    ("olddone1", "done", "готовая утром", 240, "workpc"),
    ("ancient1", "done", "позавчерашняя", 60 * 48, "workpc"),
]
FULL, EMPTY, NO_MONGO = "full", "empty", "no_mongo"


def _fill(db):
    for tid, status, desc, age, device in TASKS:
        task = tasks.new_task(desc, device)
        task.update(id=tid + "-0000-0000-0000-000000000000", status=status,
                    created_at=datetime.utcnow() - timedelta(minutes=age))
        tasks.save_task(db, task)


@pytest.mark.parametrize("db, params, shown, hidden", [
    (FULL, {}, ["freshrun", "vikarun1", "Закончились за 24 ч", "olddone1"], ["stuckrun", "ancient1"]),   # застрявшая скрыта, сутки закончившихся видны
    (FULL, {"status": "done"}, ["olddone1"], ["freshrun"]),                 # старая done видна
    (FULL, {"device": "pc2"}, ["vikarun1"], ["freshrun"]),
    (FULL, {"status": "failed"}, ["Нет задач со статусом 'failed'"], []),
    (EMPTY, {}, ["Нет ни идущих задач, ни закончившихся за 24 ч"], []),
    (NO_MONGO, {}, ["MongoDB недоступен"], []),
])
def test_get_tasks_shows_fresh_and_hides_stuck(mongo_db, db, params, shown, hidden):
    if db == FULL:
        _fill(mongo_db)
    text = tasks_tool.get_tasks_impl(params, None if db == NO_MONGO else mongo_db, tasks)
    assert all(s in text for s in shown) and not any(h in text for h in hidden)
