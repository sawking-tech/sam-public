"""Место, заданное данными, как фактор потока положения (SAM-118): устройство посадили на место (диван, кровать), и это свидетельство о том, где оно лежит.

Место приходит данными (карта мест устройств, слова человека) и называется по имени; координаты берутся из названных мест плана. Фактор входит в распределение один раз, на выходе субъекта, а не в каждый срез окна: иначе указание учлось бы столько раз, сколько срезов. Фактор - смесь: доля `share` на пятно
вокруг места и остаток на ровный фон, чтобы устройство, которое унесли, могло перебить указание сильным измерением. Нет места или оно без координат - фактор
молчит. Модуль чистый: место приходит функцией."""
from __future__ import annotations

from typing import Callable, Mapping

import numpy as np

from .presence_factors import LOG_FLOOR, Context

PLACED_SHARE = 0.9          # доля на заданное место: слова человека о месте ставят положение с такой долей; остаток - вдруг унесли
PLACED_RADIUS_M = 1.0       # пятно вокруг места: размер дивана и кровати, не точка


class Placed:
    def __init__(self, place_of: Callable[[], str | None], place_xy: Mapping[str, tuple[float, float]], share: float = PLACED_SHARE, radius_m: float = PLACED_RADIUS_M) -> None:
        self._place_of, self._place_xy, self._share, self._radius_m = place_of, place_xy, share, radius_m

    def log_field(self, context: Context) -> np.ndarray | None:
        """Логарифм вероятности клетки при указанном месте; места нет или оно без координат - фактор молчит."""
        xy = self._place_xy.get(self._place_of() or "")
        if xy is None:
            return None
        points = context.world.points
        blob = np.exp(-0.5 * (np.hypot(points[:, 0] - xy[0], points[:, 1] - xy[1]) / self._radius_m) ** 2)
        return np.log(np.maximum(self._share * blob / blob.sum() + (1.0 - self._share) / len(points), LOG_FLOOR))
