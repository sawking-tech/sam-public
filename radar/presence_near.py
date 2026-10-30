"""Связь «рядом с другим субъектом» как фактор потока положения (SAM-118): телефон часто с человеком, человек с котом, поэтому по одному можно судить о другом.

Причина в жизни обратная (человек определяет, где телефон), но первичный признак есть у того, кого угадываем первым, и связь инвертируется.
Фактор - смесь: доля `share` на близость к распределению другого субъекта и остаток на ровный фон. Фон не даёт обнулить клетки, где другого нет,
поэтому «телефон в одной комнате, человек в другой» отдельного случая не требует. Близость считается по проходам мира, а не по прямой, и стена её не
пропускает. Доля и радиус - свои у каждой пары и подбираются по испытаниям. Модуль чистый: чужое распределение приходит функцией."""
from __future__ import annotations

from typing import Callable

import numpy as np

from .presence_factors import LOG_FLOOR, Context

NEAR_SHARE = 0.7        # начальная доля времени, когда субъект рядом с другим: до подгонки по проходкам
NEAR_REACH_M = 1.5      # на таком расстоянии по проходам близость падает до 0,6 от максимума: «рядом» в одной комнате, не в соседней


class NearSubject:
    def __init__(self, other: Callable[[], np.ndarray | None], share: float = NEAR_SHARE, reach_m: float = NEAR_REACH_M) -> None:
        self._other, self._share, self._reach_m = other, share, reach_m

    def log_field(self, context: Context) -> np.ndarray | None:
        """Логарифм вероятности клетки при близости к другому субъекту; о нём ничего не известно - фактор молчит."""
        other = self._other()
        if other is None:
            return None
        distances = context.world.distances()
        kernel = np.exp(-0.5 * (np.where(np.isfinite(distances), distances, np.inf) / self._reach_m) ** 2)
        near = kernel @ other
        mixed = self._share * near / near.sum() + (1.0 - self._share) / len(near)
        return np.log(np.maximum(mixed, LOG_FLOOR))
