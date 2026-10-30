"""Окно путей потока положения (SAM-118): распределение последней точки по последним срезам и пути, которые они допускают.

На каждом шаге приходит новое измерение. Окно хранит свидетельства последних `size` срезов и заново проходит их по клеткам мира вперёд: на первом срезе
стартовое распределение, дальше переход по правилу локальности (клетка достижима в радиусе R и без стены) и умножение на свидетельства среза. Пути, которые
пересекают стену или рвут локальность, теряют массу сразу; чем больше срезов, тем меньше допустимых концов. Начало окна - распределение, которое окно
держало в этом срезе раньше, в смеси с ровным: `start_certainty` - вероятность, что прошлое верно (единица - обычный рекурсивный фильтр, нуль - ровное начало).
Внутри окна все пути законны, ошибочную оценку исправляет граница окна. Свидетельства считаются один раз, в момент прихода среза, поэтому источникам не нужна история; вклад каждого фактора в каждый срез хранится отдельно и нормированно, чтобы слой страницы показывал, откуда взялась вероятность. Переход тоже
наблюдает срез (разность уровня с прошлым срезом): наблюдения и матрица перехода хранятся по срезам, и проход, выборка путей и сглаживание берут матрицу
того среза, в который переходят. Карта предпочтений в окно не входит: поток умножает на неё результат один раз на выходе, иначе она учлась бы в каждом срезе. Модуль чистый."""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Sequence

import numpy as np

from .presence_factors import LOG_FLOOR, Context, Factor, Transition
from .presence_world import World

WINDOW_SLICES = 14         # срезов в окне при срезе 1 с: 11 окт кривая по длине окна при боевом старте (SAM-118): 14 срезов - качели 25% против 62% на 7, верная вершина 65%, отставание после прихода 21 с; 60 срезов хуже по всем метрикам (вершина 39%, отставание 27 с); размер боевого окна - SAM_PRESENCE_WINDOW_SLICES
START_CERTAINTY = 0.5      # уверенность в начале окна: до подбора по проходкам
START_MIN_SHARE = 0.0      # клетки начала окна с вероятностью ниже этой доли максимума отбрасываются: пути стартуют только с точки прошлого и ещё нескольких вероятных; нуль - ничего не отбрасывается


class Window:
    def __init__(self, world: World, motion: Transition, evidence: Sequence[Factor], size: int = WINDOW_SLICES, start_certainty: float = START_CERTAINTY,
                 start_min_share: float = START_MIN_SHARE) -> None:
        if size < 1 or not 0.0 <= start_certainty <= 1.0 or not 0.0 <= start_min_share < 1.0:
            raise ValueError(f"окно из {size} срезов с уверенностью {start_certainty} и долей отсечения {start_min_share}: срезов нужно не меньше одного, уверенность от нуля до единицы, доля от нуля до единицы")
        self._world, self._motion, self._evidence, self._size, self._certainty, self._min_share = world, motion, tuple(evidence), size, start_certainty, start_min_share
        self._emissions: deque[np.ndarray] = deque()           # логарифм свидетельств каждого среза окна
        self._parts: deque[dict[str, np.ndarray]] = deque()    # вклад каждого свидетельства в срез, нормированный, по имени класса источника
        self._observed: deque[tuple | None] = deque()          # наблюдения перехода в каждый срез окна (у первого среза окна они не используются)
        self._matrices: deque[np.ndarray] = deque()            # матрица перехода в каждый срез, M[c', c]: без наблюдений общая матрица, с ними своя
        self._alphas: list[np.ndarray] = []                    # распределение каждого среза окна по последнему проходу
        self._start: np.ndarray | None = None                  # распределение среза, вышедшего из окна
        self._transition: np.ndarray | None = None             # что говорит последнему срезу переход от предыдущего, нормированно

    @property
    def world(self) -> World:
        return self._world

    @property
    def size(self) -> int:
        """Сколько срезов держит окно, когда набралось полностью."""
        return self._size

    @property
    def history(self) -> tuple[np.ndarray, ...]:
        """Распределения по срезам окна от старого к новому."""
        return tuple(self._alphas)

    @property
    def evidence_history(self) -> tuple[dict[str, np.ndarray], ...]:
        """Вклад каждого свидетельства в каждый срез окна от старого к новому: имя класса источника в нижнем регистре и его нормированное поле."""
        return tuple(dict(parts) for parts in self._parts)

    @property
    def transition(self) -> np.ndarray | None:
        """Что говорит последнему срезу переход от предыдущего среза; первый срез окна - None."""
        return self._transition

    def sample_paths(self, n: int, end: np.ndarray | None = None, rng: np.random.Generator | None = None, upto: int | None = None) -> np.ndarray:
        """n путей по срезам окна (от старого среза к новому) из распределения допустимых путей: конец берётся из end (по умолчанию из распределения последнего среза), каждый
        предыдущий срез выбирается обратным ходом из его распределения с весом перехода в уже выбранную клетку. upto - последний срез пути (по умолчанию последний срез окна).
        Путь через стену или с разрывом локальности получить нельзя."""
        if not self._alphas:
            raise ValueError("в окне нет срезов: сначала step")
        rng = rng or np.random.default_rng()
        alphas, matrices = (self._alphas if upto is None else self._alphas[:upto + 1]), list(self._matrices)
        paths = np.empty((n, len(alphas)), dtype=int)
        paths[:, -1] = rng.choice(len(self._world), size=n, p=alphas[-1] if end is None else end)
        for k in range(len(alphas) - 2, -1, -1):
            weights = matrices[k + 1][paths[:, k + 1]] * np.maximum(alphas[k], LOG_FLOOR)[None, :]          # матрица среза, в который переходим; пол не даёт нулевой суммы; нелегальный предшественник остаётся с нулевым весом перехода
            cumulative = weights.cumsum(axis=1)
            paths[:, k] = (cumulative < rng.random((n, 1)) * cumulative[:, -1:]).sum(axis=1)
        return paths

    def smoothed(self, end: np.ndarray | None = None, upto: int | None = None) -> list[np.ndarray]:
        """Распределение каждого среза по законным путям, которые доходят до среза upto (по умолчанию последний): обратный ход точного сглаживания. Клетка среза с нулём не лежит
        ни на одном законном пути к концу. end - распределение конечного среза (по умолчанию из окна). Нужен слою: светлые клетки на путях, остальные исключены."""
        if not self._alphas:
            raise ValueError("в окне нет срезов: сначала step")
        last = len(self._alphas) - 1 if upto is None else upto
        alphas, matrices = self._alphas[:last + 1], list(self._matrices)
        gammas = [None] * len(alphas)
        gammas[-1] = alphas[-1] if end is None else end
        for k in range(len(alphas) - 2, -1, -1):
            matrix = matrices[k + 1]                                        # матрица перехода в срез k+1
            predicted = matrix @ alphas[k]                                  # что срез k даёт каждой клетке среза k+1
            ratio = np.divide(gammas[k + 1], predicted, out=np.zeros_like(predicted), where=predicted > 0)
            smoothed = alphas[k] * (matrix.T @ ratio)
            gammas[k] = smoothed / smoothed.sum()
        return gammas

    def progress(self, end: np.ndarray | None = None) -> list[list[np.ndarray]]:
        """Как уточнялось поле: для каждого k законные пути по первым k + 1 срезам окна (срезы 0..k с концом в k); последний набор - по всему окну с концом end. Слой показывает
        поля за первую секунду, за первые две и так далее до итога."""
        last = len(self._alphas) - 1
        return [self.smoothed(end if k == last else None, upto=k) for k in range(last + 1)]

    def set_start(self, distribution: np.ndarray) -> None:
        """Задать начало окна извне: положение известно (слова человека, событие), путь продолжается от него, а не от ровного фона."""
        self._start = np.asarray(distribution, dtype=float) / np.sum(distribution)

    def reset(self) -> None:
        """Забыть срезы: после долгого молчания источника прошлое распределение ничего не говорит, человек мог уйти куда угодно."""
        self._emissions.clear()
        self._parts.clear()
        self._observed.clear()
        self._matrices.clear()
        self._motion.reset()
        self._alphas, self._start, self._transition = [], None, None

    def step(self, now: datetime) -> np.ndarray:
        """Принять новый срез и вернуть распределение его точки."""
        total, parts = self._slice_evidence(now)
        observed = self._motion.observe(now)
        self._emissions.append(total)
        self._parts.append(parts)
        self._observed.append(observed)
        self._matrices.append(self._motion.into_matrix(self._world, observed))
        if len(self._emissions) > self._size:
            self._emissions.popleft()
            self._parts.popleft()
            self._observed.popleft()
            self._matrices.popleft()
            self._start = self._alphas[0]
        self._alphas = self._pass(now)
        return self._alphas[-1]

    def _slice_evidence(self, now: datetime) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        total, parts = np.zeros(len(self._world)), {}
        for factor in self._evidence:
            field = factor.log_field(Context(world=self._world, now=now))
            if field is not None:
                total += field
                parts[type(factor).__name__.lower()] = _normalised(field)
        return total, parts

    def _start_prior(self) -> np.ndarray | None:
        if self._start is None or self._certainty == 0.0:
            return None
        start = self._start
        if self._min_share > 0.0:                                                                  # нужны только точка прошлого и вероятные рядом с ней: слабые гипотезы начала уходят
            start = np.where(start >= self._min_share * start.max(), start, 0.0)
            start = start / start.sum()
        return self._certainty * start + (1.0 - self._certainty) / len(start)         # смесь: прошлое верно с вероятностью certainty, иначе ровное

    def _pass(self, now: datetime) -> list[np.ndarray]:
        alphas, previous, self._transition = [], self._start_prior(), None
        for emission, observed in zip(self._emissions, self._observed):
            if previous is None:
                previous = _normalised(emission)
            else:
                moved = self._motion.log_field(Context(world=self._world, now=now, previous=previous, observed=observed))
                self._transition = _normalised(moved)
                previous = _normalised(moved + emission)
            alphas.append(previous)
        return alphas


def _normalised(log_field: np.ndarray) -> np.ndarray:
    weights = np.exp(log_field - log_field.max())
    return weights / weights.sum()
