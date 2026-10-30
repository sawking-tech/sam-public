"""tune_self / get_self_config — агент подкручивает свои runtime-настройки.

Использование агентом (примеры):
- «говори быстрее» → tune_self(voice_speed=1.2)
- «говори помедленнее» → tune_self(voice_speed=0.85)
- «звук погромче» → tune_self(voice_volume=1.3)
- «верни как было» → tune_self(reset=True)
"""
from __future__ import annotations


def tune_self_impl(params: dict, self_cfg) -> str:
    """Подкрутить один ключ self-конфига.

    params:
        key (required, если reset не указан) — имя поля
        value (required, если reset не указан) — новое значение
        reset (optional, bool) — если true, сбрасывает всё к дефолтам
    """
    if params.get("reset"):
        self_cfg.reset()
        return f"Сброшено к дефолтам: {self_cfg.all()}"

    key = params.get("key", "").strip()
    if not key:
        return (
            "Нужен key. Доступные: voice_speed, voice_pitch, voice_volume, "
            "persona_warmth"
        )

    if "value" not in params:
        return "Нужно value"

    ok, msg = self_cfg.tune(key, params["value"])
    if not ok:
        return f"Не удалось: {msg}"
    return f"OK: {msg}. Применится со следующей фразы."


def get_self_config_impl(params: dict, self_cfg) -> str:
    """Показать текущее состояние self-конфига."""
    values = self_cfg.all()
    if not values:
        return "Пусто"
    lines = [f"{k}: {v}" for k, v in values.items()]
    return "\n".join(lines)
