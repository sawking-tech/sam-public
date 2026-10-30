"""
SAM Catalog — MongoDB каталог сущностей дома.

Типы:
  person  — люди (домашние)
  pet     — питомцы (каждый кот отдельно)
  device  — компьютеры, телефоны (с IP, MAC, MCP)
  place   — комнаты, зоны (опционально)
"""

import os
from datetime import datetime, timedelta
from typing import Optional

COLLECTION = "catalog"
# Устройство этой машины в каталоге: адресат по умолчанию, когда online никого нет, и сессия монитора без истории
LOCAL_DEVICE = os.getenv("SAM_LOCAL_DEVICE", "workpc")
DEVICE_OFFLINE_AFTER = timedelta(minutes=5)


def upsert(db, entity_type: str, name: str, data: dict) -> dict:
    """Создать или обновить запись. name — уникальный ключ внутри типа."""
    doc = {
        "type": entity_type,
        "name": name.lower(),
        "display_name": data.pop("display_name", name),
        **data,
        "updated_at": datetime.utcnow(),
    }
    db[COLLECTION].update_one(
        {"type": entity_type, "name": name.lower()},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return db[COLLECTION].find_one({"type": entity_type, "name": name.lower()}, {"_id": 0})


def get(db, entity_type: str, name: str) -> Optional[dict]:
    return db[COLLECTION].find_one(
        {"type": entity_type, "name": name.lower()}, {"_id": 0}
    )


def delete(db, entity_type: str, name: str) -> bool:
    return db[COLLECTION].delete_one({"type": entity_type, "name": name.lower()}).deleted_count > 0


def list_all(db, entity_type: str = None) -> list:
    query = {"type": entity_type} if entity_type else {}
    return list(db[COLLECTION].find(query, {"_id": 0}))


# ─── Устройства ───────────────────────────────────────────────────────────────

def register_device(db, name: str, ip: str, data: dict) -> dict:
    """Регистрация/обновление устройства при старте client_agent."""
    return upsert(db, "device", name, {
        "ip": ip,
        "last_seen": datetime.utcnow(),
        "online": True,
        **data,
    })


def heartbeat(db, name: str, presence_score: float = 0):
    """Обновить last_seen и presence_score устройства."""
    db[COLLECTION].update_one(
        {"type": "device", "name": name.lower()},
        {"$set": {
            "last_seen": datetime.utcnow(),
            "online": True,
            "presence_score": presence_score,
        }},
    )


def mark_offline(db, name: str):
    db[COLLECTION].update_one(
        {"type": "device", "name": name.lower()},
        {"$set": {"online": False}},
    )


def get_online_devices(db) -> list:
    cutoff = datetime.utcnow() - DEVICE_OFFLINE_AFTER
    return list(db[COLLECTION].find(
        {"type": "device", "last_seen": {"$gte": cutoff}},
        {"_id": 0},
    ))


def get_best_device(db, capability: str = None) -> Optional[dict]:
    """Онлайн-устройство с наибольшим presence_score."""
    devices = get_online_devices(db)
    if capability:
        devices = [d for d in devices if capability in d.get("capabilities", [])]
    if not devices:
        return None
    return max(devices, key=lambda d: d.get("presence_score", 0))
