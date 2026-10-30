"""Runtime self-config SAM.

Агент может подкручивать сам себя через tool `tune_self`: voice speed,
pitch, persona intensity и т.д. Пользователь говорит «говори быстрее» →
агент дёргает tune_self(voice_speed=1.15) → voice_client на следующем
speak забирает новое значение.

Хранилище: dict в памяти + Mongo collection `_self_config` (single doc
с _id="current"). При старте gateway читает из Mongo; tune_self пишет
обратно.

Все знают про SelfConfig — voice_client (на client PC) тянет через
GET /sam/config, agent через tools.
"""
from __future__ import annotations

import threading
from typing import Any


# Дефолты + рейнджи для валидации
DEFAULTS: dict[str, Any] = {
    "voice_speed": 1.2,            # 0.6 (медленно) — 1.6 (быстро). Time-stretch factor.
    "voice_pitch": 1.0,            # 0.7 — 1.3. Pitch shift через resample.
    "voice_volume": 1.0,           # 0.0 — 2.0 множитель амплитуды PCM.
    "persona_warmth": 1.0,         # будущее: насколько тёплый/деловой тон.
    "tts_enabled": True,           # глобальный выключатель TTS
    "tts_announce_chat": True,     # озвучивать ответы из монитора/REST тоже
    "plan_critic_enabled": True,         # LLM-критик плана воркера (макс 2 блока)
    "response_critic_enabled": True,     # LLM-критик ответа оркестратора перед отдачей (внутренний диалог)
    "debug_mode": False,                 # debug: НЕ сохранять chat history в Mongo (только явный remember). Полезно для отладки промптов.
    "voice_gender": "female",            # род речи SAM по голосу: female | male (SAM-78)
}

# Ключи с перечнем допустимых значений
CHOICES: dict[str, tuple[str, ...]] = {
    "voice_gender": ("female", "male"),
}

LIMITS: dict[str, tuple[float, float]] = {
    "voice_speed": (0.5, 2.0),
    "voice_pitch": (0.5, 1.5),
    "voice_volume": (0.0, 2.5),
    "persona_warmth": (0.0, 2.0),
}

BOOLEAN_KEYS = {"tts_enabled", "tts_announce_chat", "plan_critic_enabled", "response_critic_enabled", "debug_mode"}


class SelfConfig:
    """Thread-safe runtime-конфиг с опциональной Mongo-персистентностью."""

    def __init__(self, db: Any = None):
        self._db = db
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULTS)
        if db is not None:
            self._load_from_mongo()

    def _load_from_mongo(self) -> None:
        try:
            doc = self._db["_self_config"].find_one({"_id": "current"})
            if doc:
                for k, v in doc.items():
                    if k in DEFAULTS:
                        self._values[k] = v
                print(f"[SelfConfig] loaded from Mongo: {self._values}")
        except Exception as e:
            print(f"[SelfConfig] load failed: {e}")

    def _persist(self) -> None:
        if self._db is None:
            return
        try:
            self._db["_self_config"].replace_one(
                {"_id": "current"},
                {"_id": "current", **self._values},
                upsert=True,
            )
        except Exception as e:
            print(f"[SelfConfig] persist failed: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, default)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def tune(self, key: str, value: Any) -> tuple[bool, str]:
        """Возвращает (ok, message). value может быть число, str, bool."""
        if key not in DEFAULTS:
            return False, (
                f"Unknown key '{key}'. Available: {sorted(DEFAULTS.keys())}"
            )

        if key in CHOICES and value not in CHOICES[key]:
            return False, f"'{key}' - одно из: {', '.join(CHOICES[key])}; получено {value!r}"

        if key in LIMITS:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False, f"'{value}' не число — для '{key}' нужно число"
            lo, hi = LIMITS[key]
            if not (lo <= v <= hi):
                return False, (
                    f"'{key}' должно быть в диапазоне [{lo}, {hi}], получено {v}"
                )
            value = v

        with self._lock:
            self._values[key] = value
            self._persist()

        return True, f"{key} = {value}"

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key:
                if key in DEFAULTS:
                    self._values[key] = DEFAULTS[key]
            else:
                self._values = dict(DEFAULTS)
            self._persist()


# Singleton — gateway создаёт один раз, voice_client дёргает через REST
_instance: SelfConfig | None = None


def init(db: Any = None) -> SelfConfig:
    global _instance
    _instance = SelfConfig(db)
    return _instance


def get_instance() -> SelfConfig:
    global _instance
    if _instance is None:
        _instance = SelfConfig()
    return _instance
