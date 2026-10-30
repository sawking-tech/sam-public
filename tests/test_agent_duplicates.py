"""SAM-221: вторая задача о той же работе не ставится, промах send_to_agent по имени подсказывает похожее имя."""
import asyncio
import types
from datetime import datetime

from gateway import agent
from gateway.tools import tasks as tt
from tests.test_worker_session_owner import spawn  # noqa: F401  (фикстура запуска без Mongo и LLM)

RUNNING = [{"id": "86212f5a-aaaa", "name": "calorie_tracker_v2", "status": "running",
            "description": "Доработать приложение calorie_tracker (учёт еды и веса)"}]
SLUGS = ["calorie-tracker", "radar"]


def test_same_app_or_similar_name_is_a_duplicate_other_work_is_not():
    assert tt.duplicate_task("calorie_tracker_v3", "палитра и фото", RUNNING, SLUGS)["id"].startswith("86212f5a")
    assert tt.duplicate_task("", "Продолжить доработку calorie-tracker: SOLID", RUNNING, SLUGS)
    assert tt.duplicate_task("news", "найди новости про погоду", RUNNING, SLUGS) is None
    assert tt.duplicate_task("calorie_child", "часть работы calorie_tracker", RUNNING, SLUGS, own_id="86212f5a") is None


def test_close_names_for_a_missed_name():
    assert tt.closest_names("calorie_tracker_v3b", ["calorie_tracker_v3", "nutrition_metrics_research"]) == [
        "calorie_tracker_v3"]
    assert tt.closest_names("open_word", ["calorie_tracker_v3"]) == []


def test_spawn_refuses_a_second_agent_on_the_same_app_unless_parallel(spawn, monkeypatch):   # noqa: F811
    launched, _, db = spawn
    db.tasks.insert_one({**RUNNING[0], "created_at": datetime.utcnow(), "device": "pc"})
    monkeypatch.setattr(agent._apps, "scan", lambda root: ([types.SimpleNamespace(slug=s) for s in SLUGS], []))
    text = asyncio.run(agent._impl_create_task("доработай calorie_tracker: палитра", name="calorie_tracker_v3"))
    assert "Уже идёт задача 86212f5a" in text and "send_to_agent(task_id=86212f5a)" in text and launched == []
    text = asyncio.run(agent._impl_create_task("доработай calorie_tracker: палитра", name="calorie_tracker_v3",
                                               parallel=True))
    assert "Уже идёт" not in text and len(launched) == 1
