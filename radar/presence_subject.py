"""Субъект потока положения (SAM-118): телефон, человек или кот. Тип субъекта задают данные: окно со своим переходом и свидетельствами, факторы выхода, число выборок.

Шаг субъекта: окно принимает срез и отдаёт распределение последней точки; факторы выхода (карта предпочтений) умножаются на него один раз и в окно не возвращаются,
иначе предпочтение учлось бы в каждом срезе и росло от шага к шагу; из итогового распределения берутся сто путей окна (только законные), концы путей дают пятна по доле,
точка - усечённое среднее пятна, ближайшего к прошлой точке, с липкостью: пятно, где стоит точка, уступает другому только при кратном перевесе несколько тиков подряд; к центру
пятна точка идёт шагом не дальше радиуса локальности за тик. Итог шага
доступен другим субъектам: связь «человек рядом с телефоном» берёт его как свидетельство. Модуль чистый: время и случайность приходят параметрами."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import numpy as np

from . import presence_cloud as pc
from .presence_factors import LOG_FLOOR, Context, Factor
from .presence_report import PlaceReport
from .presence_window import Window

SAMPLES = 100        # выборок на шаг: сотня прогонов, из них пятна по доле
SECOND_SAMPLES = 40  # выборок на точку каждой прошлой секунды окна: эти точки рисуются как ход уточнения, итоговая считается по полной сотне


@dataclass(frozen=True)
class Estimate:
    distribution: np.ndarray          # распределение последней точки по клеткам мира, сумма единица
    lobes: list[pc.Lobe]              # пятна по убыванию доли выборок
    places: list[tuple[str, float]] = field(default_factory=list)       # названные места с вероятностью по убыванию: выход для модели SAM
    paths: np.ndarray | None = None   # пути окна по срезам, клетки мира (путей x срезов); None - окно ещё пусто
    point: int | None = None          # клетка точки: среднее пятна, ближайшего к прошлой точке
    previous_point: int | None = None
    parts: dict[str, np.ndarray] = field(default_factory=dict)         # вклад факторов выхода (карта предпочтений) по имени класса, нормированный
    first_point: int | None = None    # точка первой секунды окна: итог бросков по полю этой секунды, ближайший к прошлой точке; остальные точки секунд считает `Subject.second_points` по запросу слоя
    before_point: int | None = None   # позиция до окна: точка первой секунды прошлого шага, которая теперь вышла из окна; None - окно прошлого шага было неполным


class Subject:
    def __init__(self, name: str, window: Window, output: Sequence[Factor] = (), samples: int = SAMPLES, rng: np.random.Generator | None = None,
                 report: PlaceReport | None = None, hold_ratio: float = pc.HOLD_RATIO, hold_ticks: int = pc.HOLD_TICKS, step_m: float = pc.POINT_STEP_M) -> None:
        """hold_ratio и hold_ticks - липкость пятна точки (`presence_cloud.hold_lobe`): другое пятно забирает точку, только если его доля больше в hold_ratio раз hold_ticks тиков подряд.
        step_m - дальше этого точка за тик не уходит: к центру пятна она идёт шагом по проходам (`presence_cloud.step_towards`)."""
        self.name = name
        self._window, self._output, self._samples, self._rng, self._report = window, tuple(output), samples, rng, report
        self._hold_ratio, self._hold_ticks, self._hold, self._step_m = hold_ratio, hold_ticks, pc.Hold(), step_m
        self._diag_rng = None if rng is None else np.random.default_rng(rng.integers(0, 2**63))      # слой объяснения берёт свой поток: запросы страницы не сдвигают числа расчёта
        self.distribution: np.ndarray | None = None       # итог последнего шага; None - шагов ещё не было
        self.point: int | None = None                      # клетка точки последнего шага
        self._leaving: int | None = None                   # точка первой секунды последнего шага, пока окно полное: на следующем шаге эта секунда выйдет из окна

    @property
    def window(self) -> Window:
        return self._window

    def start_at(self, cell: int) -> None:
        """Положение известно извне: окно начинается в этой клетке, точка тоже. Прежние срезы забываются."""
        self.reset()
        start = np.zeros(len(self._window.world))
        start[cell] = 1.0
        self._window.set_start(start)
        self.point = cell

    def reset(self) -> None:
        self._window.reset()
        self.distribution, self.point, self._leaving, self._hold = None, None, None, pc.Hold()

    def step(self, now: datetime) -> Estimate:
        latest = self._window.step(now)
        context = Context(world=self._window.world, now=now, previous=latest)
        log, parts = np.log(np.maximum(latest, LOG_FLOOR)), {}
        for factor in self._output:
            extra = factor.log_field(context)
            if extra is not None:
                log = log + extra
                parts[type(factor).__name__.lower()] = _normalised(extra)
        weights = np.exp(log - log.max())
        self.distribution = weights / weights.sum()
        world, previous = self._window.world, self.point
        paths = self._window.sample_paths(self._samples, self.distribution, self._rng)
        lobes = pc.cloud_lobes(np.bincount(paths[:, -1], minlength=len(world)).astype(float), world.distances())
        before, history = self._leaving, len(self._window.history)
        first = self._second_point(0, previous, self._rng) if history > 1 else None            # точка первой секунды нужна следующему шагу как точка до окна
        lobe, self._hold = pc.hold_lobe(lobes, world.distances(), previous, self._hold, self._hold_ratio, self._hold_ticks)
        self.point = pc.step_towards(previous, pc.trimmed_centre(paths[:, -1], lobe, world.distances()), world.distances(), self._step_m) if lobe else previous
        self._leaving = (first if history > 1 else self.point) if history == self._window.size else None
        return Estimate(self.distribution, lobes, self._report(self.distribution) if self._report else [], paths, self.point, previous, parts,
                        first if history > 1 else self.point, before)

    def second_points(self, estimate: Estimate) -> list[int | None]:
        """Точка каждой секунды окна, последняя итоговая: слою для цепочки точек. Для секунды k сто бросков (меньше) по полю за первые k + 1 секунд, пятно ближе к точке прошлой секунды, в нём среднее без выбросов.
        Считается по запросу: по всем секундам минутного окна это квадрат от числа срезов, на каждом шаге не нужен."""
        history = len(self._window.history)
        if history == 1:
            return [estimate.point]
        held, points = estimate.first_point, [estimate.first_point]
        for k in range(1, history - 1):
            held = self._second_point(k, held, self._diag_rng)
            points.append(held)
        return [*points, estimate.point]

    def _second_point(self, k: int, held: int | None, rng: np.random.Generator | None) -> int | None:
        world = self._window.world
        ends = self._window.sample_paths(SECOND_SAMPLES, None, rng, upto=k)[:, -1]
        lobes = pc.cloud_lobes(np.bincount(ends, minlength=len(world)).astype(float), world.distances())
        lobe = pc.choose_lobe(lobes, world.distances(), held)
        return pc.trimmed_centre(ends, lobe, world.distances()) if lobe else held

    def field_upto(self, k: int) -> list[np.ndarray]:
        """Как уточнялось поле секунды k: распределение каждого среза 0..k по законным путям первых k + 1 секунд; клетки с нулём исключены. У последней секунды конец это итоговое распределение. Считается по запросу: для минутного окна по всем секундам сразу это квадрат от числа срезов."""
        last = len(self._window.history) - 1
        return self._window.smoothed(self.distribution if k == last else None, upto=k)

    def paths_upto(self, k: int, n: int | None = None) -> np.ndarray:
        """n путей окна (по умолчанию столько же, сколько бросков на шаг), которые кончаются на секунде k: законные по первым k + 1 секундам, конец берётся из распределения этой секунды."""
        return self._window.sample_paths(self._samples if n is None else n, None, self._diag_rng, upto=k)


def _normalised(log_field: np.ndarray) -> np.ndarray:
    weights = np.exp(log_field - log_field.max())
    return weights / weights.sum()
