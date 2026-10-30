"""
SAM Task System — структурированные задачи с этапами и DoD.

Task flow:
  create_task() → TaskPlanner LLM → MongoDB → TaskRunner → streaming feedback

Stage statuses: pending → running → done | failed | skipped
Task statuses:  pending → running → done | failed
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional
from pymongo.collection import Collection

# TTL после завершения
TTL_DONE_H   = 24   # часов — выполненные задачи
TTL_FAILED_H = 2    # часов — упавшие
TTL_FALSE_M  = 15   # минут — не запустились


def ensure_indexes(db):
    """Создать TTL-индекс на expires_at (MongoDB удалит документ автоматически)."""
    db.tasks.create_index("expires_at", expireAfterSeconds=0)
    db.tasks.create_index("status")
    db.tasks.create_index("created_at")   # список задач монитора, свежие первыми


def new_task(
    description: str,
    device: str,
    stages: list[dict] = None,
    *,
    name: str = "",
    creator: str = "",
    level: str = "",
    tools: list[str] | None = None,
    kind: str = "",
    env: str = "",
) -> dict:
    """name — короткое семантическое имя ("открыть_word") от того кто
    создаёт таску. Можно потом ссылаться: send_to_task(name='открыть_word').
    creator — кто создал: "orchestrator" (main agent) или "task:abc12345"
    если воркер другой таски создал эту. level - метка сложности, tools - права агента
    (SAM-91, gateway/tools/agents.py); пусто - воркер spawn_agent со всеми инструментами."""
    now = datetime.utcnow()
    return {
        "id":          str(uuid.uuid4()),
        "name":        name.strip() or "",
        "creator":     creator.strip() or "orchestrator",
        "description": description,
        "device":      device,
        "status":      "pending",
        "created_at":  now,
        "expires_at":  now + timedelta(minutes=TTL_FALSE_M),  # продлится при старте
        "stages":      [_normalize_stage(i, s) for i, s in enumerate(stages or [])],
        "current_stage": 0,
        "result":      None,
        "error":       None,
        **({"level": level} if level else {}),
        **({"tools": list(tools)} if tools is not None else {}),
        **({"kind": kind} if kind else {}),
        **({"env": env} if env else {}),
    }


def _normalize_stage(idx: int, s: dict) -> dict:
    return {
        "idx":        idx,
        "name":       s.get("name", f"stage_{idx}"),
        "description": s.get("description", ""),
        "action":     s["action"],           # {"tool": "...", "args": {...}}
        "dod":        s.get("dod", ""),      # описание критерия готовности
        "dod_check":  s.get("dod_check"),    # опц: "regex:...", "mcp:tool:args", "llm"
        "on_fail":    s.get("on_fail", "retry"),  # retry | skip | replan | ask_user | abort
        "max_retries": s.get("max_retries", 2),
        "timeout_s":  s.get("timeout_s", 30),
        "status":     "pending",
        "retries":    0,
        "result":     None,
        "error":      None,
        "started_at": None,
        "done_at":    None,
    }


# ─── CRUD ─────────────────────────────────────────────────────────────────────

def save_task(db, task: dict) -> str:
    db.tasks.insert_one(task)
    return task["id"]


def get_task(db, task_id: str) -> Optional[dict]:
    return db.tasks.find_one({"id": task_id}, {"_id": 0})


FINAL_STATUSES = frozenset({"done", "unverified", "failed", "cancelled"})


def update_task_status(db, task_id: str, status: str, error: str = None, result: str = None):
    now = datetime.utcnow()
    update = {"status": status}
    if error:
        update["error"] = error
    if result:
        update["result"] = result

    if status in FINAL_STATUSES:
        update["finished_at"] = now   # время работы агента (agent_info)
    # Обновить TTL в зависимости от финального статуса
    if status in ("done", "unverified"):
        update["expires_at"] = now + timedelta(hours=TTL_DONE_H)
    elif status == "failed":
        update["expires_at"] = now + timedelta(hours=TTL_FAILED_H)

    db.tasks.update_one({"id": task_id}, {"$set": update})


def update_stage(db, task_id: str, stage_idx: int, update: dict):
    db.tasks.update_one(
        {"id": task_id},
        {"$set": {
            f"stages.{stage_idx}.{k}": v for k, v in update.items()
        }}
    )


def advance_stage(db, task_id: str, next_idx: int):
    db.tasks.update_one(
        {"id": task_id},
        {"$set": {"current_stage": next_idx}}
    )


def mark_task_running(db, task_id: str):
    now = datetime.utcnow()
    db.tasks.update_one(
        {"id": task_id},
        {"$set": {
            "status": "running",
            "started_at": now,
            # Продлить TTL — задача реально запустилась
            "expires_at": now + timedelta(hours=TTL_FAILED_H),
        }}
    )


def cancel_task(db, task_id: str) -> bool:
    """Пометить задачу как отменённую. Поддерживает и полный UUID, и 8-символьный префикс."""
    now = datetime.utcnow()
    result = db.tasks.update_one(
        {**_id_filter(task_id), "status": {"$in": ["pending", "running"]}},
        {"$set": {
            "status": "cancelled",
            "error": "Отменено пользователем",
            "expires_at": now + timedelta(hours=TTL_FAILED_H),
        }}
    )
    return result.modified_count > 0


def _id_filter(task_id: str) -> dict:
    """Полный UUID или короткий префикс из монитора."""
    return {"id": {"$regex": f"^{task_id}"}} if len(task_id) < 36 else {"id": task_id}


def find_task(db, task_id: str) -> Optional[dict]:
    """Задача по полному id или префиксу; свежая, если префикс неоднозначен."""
    return db.tasks.find_one(_id_filter(task_id), {"_id": 0}, sort=[("created_at", -1)])


def add_question(db, task_id: str, text: str) -> Optional[dict]:
    """Вопрос агента создателю - в задачу: при завершении видно, что спрашивал. Задача после записи."""
    task = find_task(db, task_id)
    if task is None:
        return None
    db.tasks.update_one({"id": task["id"]}, {"$push": {"questions": {"text": text, "at": datetime.utcnow()}}})
    return find_task(db, task["id"])


def list_tasks(db, status: str = None, device: str = None, limit: int = 10) -> list[dict]:
    """Список задач с фильтрацией по статусу и устройству."""
    if status:
        q = {"status": status}
    else:
        q = {"status": {"$in": ["pending", "running"]}}
    if device:
        q["device"] = device
    return list(db.tasks.find(q, {"_id": 0}).sort("created_at", -1).limit(limit))


def list_finished_since(db, since: datetime, limit: int = 10) -> list[dict]:
    """Закончившиеся задачи (любой статус, кроме идущих), начатые не раньше since, свежие первыми."""
    q = {"status": {"$nin": ["pending", "running"]}, "created_at": {"$gte": since}}
    return list(db.tasks.find(q, {"_id": 0}).sort("created_at", -1).limit(limit))


# Поля задачи, которые видит монитор: панель задач и шапка секции агента после F5 (события task_* в историю не
# пишутся, правда о задаче - здесь)
MONITOR_FIELDS = ("name", "description", "device", "creator", "status", "result", "error", "level")


def _iso(when: Optional[datetime]) -> Optional[str]:
    return when.isoformat() + "Z" if when else None


def recent_for_monitor(db, limit: int) -> list[dict]:
    """Последние `limit` задач любого статуса, свежие первыми: id - короткий, как в событиях и сессии `task:<id>`."""
    rows = db.tasks.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
    return [{"task_id": t["id"][:8], **{k: t.get(k) for k in MONITOR_FIELDS},
             "created_at": _iso(t.get("created_at")), "started_at": _iso(t.get("started_at"))}
            for t in rows if t.get("id")]


def list_pending_tasks(db, device: str = None) -> list[dict]:
    q = {"status": {"$in": ["pending", "running"]}}
    if device:
        q["device"] = device
    return list(db.tasks.find(q, {"_id": 0}).sort("created_at", 1))


def format_task_summary(task: dict) -> str:
    """Краткое состояние задачи для агента (token-efficient).
    Включает name для удобного referencing через send_to_task."""
    stages = task.get("stages", [])
    name = task.get("name", "")
    name_part = f" name='{name}'" if name else ""
    creator = task.get("creator", "")
    creator_part = f" by={creator}" if creator and creator != "orchestrator" else ""
    model_part = f" model={task['source']}:{task['model']}" if task.get("model") else ""   # создатель меняет её на лету
    lines = [
        f"Task [{task['id'][:8]}]{name_part}{creator_part}{model_part} "
        f"{task['status']}: {task['description']}"
    ]
    for s in stages:
        icon = {"pending": "○", "running": "◉", "done": "✓", "failed": "✗", "skipped": "→"}.get(s["status"], "?")
        err = f" — {s['error'][:60]}" if s.get("error") else ""
        lines.append(f"  {icon} [{s['idx']}] {s['name']}{err}")
    return "\n".join(lines)
