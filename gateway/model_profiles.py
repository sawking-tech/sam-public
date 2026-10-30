"""Профили моделей для выбора модели агенту: уровень, локальная ли, инструменты, контекст.

Файл gateway/model_profiles.json, свой - SAM_MODEL_PROFILES_FILE. Ключ как в книге цен: "поставщик/модель".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PROFILES_FILE_ENV = "SAM_MODEL_PROFILES_FILE"
DEFAULT_PROFILES_FILE = Path(__file__).with_name("model_profiles.json")
TIER_MIN, TIER_MAX = 1, 3


# Границы рейтинга (индекс интеллекта Artificial Analysis, безразмерный) для уровня модели: не ниже границы - этот
# уровень. Выше уровень - выше цена ошибки на задаче, поэтому границы правятся вместе с профилями
RATING_BANDS = ((44.0, 3), (38.0, 2), (0.0, 1))


def tier_for_rating(rating: float) -> int:
    return next(tier for bound, tier in RATING_BANDS if rating >= bound)


@dataclass(frozen=True)
class ModelProfile:
    tier: int            # 1 простое, 2 обычное, 3 сложное (код, правка SAM); из рейтинга, если он известен
    local: bool          # на этом ПК: токены бесплатны, но видеокарта общая с голосом
    tools: bool          # вызов инструментов
    context: int         # окно в токенах
    tps_hint: float | None = None   # токенов выхода в секунду, пока нет замера
    rating: float | None = None     # публичный рейтинг модели (Artificial Analysis), None - не найден, уровень задан вручную


def load_profiles(path: str | os.PathLike | None = None) -> dict[str, ModelProfile]:
    target = Path(path or os.getenv(PROFILES_FILE_ENV) or DEFAULT_PROFILES_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))["models"]
        profiles = {key: ModelProfile(**{**fields, "tier": fields.get("tier") or tier_for_rating(fields["rating"])})
                    for key, fields in raw.items()}
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise ValueError(f"профили моделей {target} не читаются ({e}); поправь файл или {PROFILES_FILE_ENV}") from e
    bad = [k for k, p in profiles.items() if not TIER_MIN <= p.tier <= TIER_MAX]
    if bad:
        raise ValueError(f"профили моделей {target}: tier от {TIER_MIN} до {TIER_MAX}, не так у {', '.join(bad)}")
    return profiles
