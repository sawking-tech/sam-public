"""
Meta packet assembly for SAM Gateway.
Собирает мета-информацию о запросе: устройство, пользователь, местоположение, активность.
"""

import os
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class MetaPacket(BaseModel):
    """Мета-пакет запроса"""
    device_id: str
    user_id: Optional[str] = None
    location: Optional[str] = None
    audio_level: float = 0.0
    monitor_on: bool = False
    mouse_activity: float = 0.0
    keyboard_activity: float = 0.0
    timestamp: datetime
    pc_activity: float = 0.0
    speaker_match: float = 0.0
    wifi_rssi: int = 0
    accelerometer_data: float = 0.0


async def collect_meta(
    device_id: str,
    ha_url: str,
    ha_token: str
) -> MetaPacket:
    """
    Собирает мета-пакет из различных источников:
    - Home Assistant: presence, location
    - Audio: уровень громкости
    - Система: активность мыши/клавиатуры, монитор
    """
    
    meta = MetaPacket(
        device_id=device_id,
        timestamp=datetime.utcnow()
    )
    
    # TODO: Интеграция с Home Assistant для presence и location
    # TODO: Чтение audio_level из Wyoming
    # TODO: Чтение активности системы (mouse/keyboard/monitor)
    # TODO: Speaker ID匹配
    
    return meta


async def get_ha_presence(
    ha_url: str,
    ha_token: str
) -> dict:
    """Получает данные о присутствии из Home Assistant"""
    # TODO: Реализовать запрос к HA API
    return {}


async def get_audio_level(device_id: str) -> float:
    """Получает текущий уровень аудио с устройства"""
    # TODO: Реализовать получение из Wyoming
    return 0.0


async def get_system_activity() -> dict:
    """Получает активность системы (мышь, клавиатура, монитор)"""
    # TODO: Реализовать через MCP или прямой опрос системы
    return {
        "mouse_activity": 0.0,
        "keyboard_activity": 0.0,
        "monitor_on": False
    }
