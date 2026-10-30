"""Непрерывность перемещения людей (SAM-118): последний шаг конвейера, который не пускает на экран то, чего человек сделать не может.

Инвариант: между двумя кадрами человек не пересекает стену (проёмы дверей свободны) и не уходит дальше одного шага ходьбы. Нарушивший кадр заменяется прежним положением
(человек стоит на месте, пока предыдущие шаги не дадут допустимое), нарушение пишется в журнал с обоими положениями. Исчезнувший с карты дольше ABSENT_RESET_S появляется на новом месте:
он мог выйти и вернуться, прошлого положения у него нет. Модуль чистый: проверка стены и журнал приходят параметрами."""
from __future__ import annotations

import math
from enum import StrEnum
from typing import Callable

BLOCKED_KEY = "blocked_by_continuity"     # пометка строки: положение заменено прежним


class Violation(StrEnum):
    JUMP = "прыжок дальше шага"
    WALL = "сквозь стену"


MAX_STEP_M = 4.0           # дальше за кадр не уйти: рывок ходьбы до 3,2 м по записи проходок, с запасом
ABSENT_RESET_S = 30.0      # не было на карте столько секунд - положение прежнее уже не держит


class Continuity:
    def __init__(self, crosses_wall: Callable[[tuple[float, float], tuple[float, float]], bool] | None,
                 max_step_m: float = MAX_STEP_M, absent_reset_s: float = ABSENT_RESET_S, report: Callable[[str], None] | None = None) -> None:
        self._crosses, self._max_step, self._absent, self._report = crosses_wall, max_step_m, absent_reset_s, report
        self._last: dict[str, tuple[float, float, float]] = {}      # человек -> (x, z, когда)
        self.violations = 0

    def apply(self, rows: list[dict], now: float) -> list[dict]:
        out = []
        for row in rows:
            x, z, person = row.get("x"), row.get("z"), row["person"]
            if x is None or z is None:
                out.append(row)
                continue
            last = self._last.get(person)
            if last and now - last[2] <= self._absent:
                reason = self._violation((last[0], last[1]), (x, z))
                if reason:
                    self.violations += 1
                    if self._report:
                        self._report(f"{person}: {reason}, ({last[0]:.2f}; {last[1]:.2f}) -> ({x:.2f}; {z:.2f}), остаётся на месте")
                    row = {**row, "x": last[0], "z": last[1], BLOCKED_KEY: True}
                    x, z = last[0], last[1]
            self._last[person] = (x, z, now)
            out.append(row)
        return out

    def _violation(self, a: tuple[float, float], b: tuple[float, float]) -> Violation | None:
        if math.hypot(b[0] - a[0], b[1] - a[1]) > self._max_step:
            return Violation.JUMP
        if self._crosses and self._crosses(a, b):
            return Violation.WALL
        return None
