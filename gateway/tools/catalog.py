"""Catalog tools — get_devices / get_catalog / wake_device (SAM-40).

Работают через catalog module + DB напрямую. Wake-on-LAN — pure socket op.
"""

from __future__ import annotations

import json
import socket

from .. import household


def get_devices_impl(params: dict, db, catalog) -> str:
    """Список устройств. online_only=True (default) — только онлайн."""
    if db is None:
        return "MongoDB недоступен"
    online_only = params.get("online_only", True)
    devices = (catalog.get_online_devices(db) if online_only
               else catalog.list_all(db, "device"))
    if not devices:
        return "Нет онлайн-устройств" if online_only else "Каталог пуст"
    lines = []
    for d in devices:
        caps = ", ".join(d.get("capabilities", []))
        lines.append(
            f"- {d['display_name']} [{d.get('ip', '')}] "
            f"score={d.get('presence_score', 0)} caps=[{caps}]"
        )
    return "\n".join(lines)


def get_catalog_impl(params: dict, db, catalog) -> str:
    """Получить сущность по имени+типу, либо весь каталог по типу."""
    if db is None:
        return "MongoDB недоступен"
    name = params.get("name", "")
    entity_type = params.get("entity_type", "")
    if name and entity_type:
        doc = catalog.get(db, entity_type, name)
        return (json.dumps(doc, ensure_ascii=False, default=str)
                if doc else f"'{name}' не найден")
    docs = catalog.list_all(db, entity_type or None)
    return json.dumps(docs, ensure_ascii=False, default=str)


def set_household_member_impl(params: dict, db, changed=lambda: None) -> str:
    """Занести человека или питомца в состав дома; поля, которых нет в params, не меняются."""
    if db is None:
        return "MongoDB недоступен"
    data = {k: v for k, v in params.items() if k in ("kind", "name", "role", "about") and v}
    try:
        member = household.save(db, data)
    except household.HouseholdError as exc:
        return f"Не записано: {exc}"
    changed()
    return f"Записано: {member['name']}"


def wake_device_impl(params: dict, db, catalog) -> str:
    """Wake-on-LAN: посылает magic packet по MAC из каталога."""
    if db is None:
        return "MongoDB недоступен"
    device = params.get("device", "")
    if not device:
        return "Нужно имя устройства"
    doc = catalog.get(db, "device", device)
    if not doc:
        return f"Устройство '{device}' не найдено"
    mac = doc.get("mac", "")
    if not mac:
        return f"MAC-адрес '{device}' не задан"
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        magic = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(magic, ("255.255.255.255", 9))
        return f"WoL пакет отправлен на {device} ({mac}). Подождите 30-60 секунд."
    except Exception as e:
        return f"WoL ошибка: {e}"
