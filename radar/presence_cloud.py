"""Облако положения (SAM-118): апостериор по клеткам пола как произведение полей и положение как выборка из него.

Поля: кольца измерения (уровень против радио-карты, перемножаются по секундам), привычное поле по людям и времени, память о прошлом положении.
Ответ - облако из n выборок и его пятна с долями; одной «лучшей точки» нет: у облака из нескольких пятен середина лежит там, где вероятности нет.
Модуль чистый: массивы приходят параметрами, случайность через переданный генератор."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BLOCK_S = 3               # секунд в блоке: медиана по блоку, соседние секунды почти повторяют друг друга
SECOND_WEIGHT = 0.3       # вес блока в произведении колец: корреляция соседних секунд даёт n_eff/n 0,46-0,62; при 0,5 области были слишком узкими
RING_SD_DB = 5.0          # разброс уровня в клетке вместе с шумом окна, карта по многостенной модели: на 7 долгих стоянках при 4,5 дБ области 50/80/90% накрывали 51/71/80%, при 5,85 - 61/83/95%
DEFAULT_SAMPLES = 200
LOBE_LINK_M = 3.6         # клетки ближе этого по проходам - одно пятно
LOBE_MIN_SHARE = 0.15     # клетка в пятне, если её вероятность не ниже этой доли максимума
NEAR_PREVIOUS_M = 6.0     # на таком расстоянии от прошлой координаты вес пятна падает в e раз: человек чаще остаётся рядом с прежним местом
STAY_PER_S = 0.995          # шанс остаться на месте за секунду: средняя стоянка около 200 с (стоянки проходок 37-230 с, у привычек дольше)
PACE_MPS = 1.2             # скорость ходьбы: обычная 1,2 м/с, медленная по проходкам 0,2; ход растягивает пятно на темп * время
JUMP_SHARE = 0.02          # доля вероятности, которую переход оставляет на «где угодно»: человек может исчезнуть из поля зрения и появиться далеко
TRIM_SHARE = 0.1          # доля самых далёких от центра выборок, отбрасываемая перед усреднением пятна
HOLD_RATIO = 2.0          # липкость пятна точки: другое пятно перебивает прежнее, только если его вес больше во столько раз
HOLD_TICKS = 3            # и держит этот перевес столько тиков подряд: смок треков 11 окт дал скачки точки дальше 2,1 м в 2% тиков при мгновенной смене пятна (SAM-118)
HOLD_REACH_M = 1.4        # пятно считается «своим», если его ближайшая клетка не дальше этого от прошлой точки по проходам: радиус локальности за срез; пятна из ста бросков разрежены, и клетка точки в них часто не входит
POINT_STEP_M = 1.4        # точка за тик идёт к центру пятна по проходам не дальше этого (радиус локальности за срез): центр широкого пятна (склейка до 3,6 м) уезжал на 2-4 м за тик при перетекании массы внутри него (смок 11 окт: 32 из 33 скачков S24)


@dataclass(frozen=True)
class Hold:
    """Память липкости между шагами: пятно, в котором стоит точка (по его клеткам), и сколько тиков подряд его перебивает претендент."""
    cells: frozenset[int] = frozenset()
    challenger: frozenset[int] = frozenset()
    streak: int = 0


@dataclass(frozen=True)
class Lobe:
    cells: tuple[int, ...]
    share: float                  # доля облака в пятне
    centre: int                   # клетка пятна, ближайшая по проходам к остальным его клеткам с учётом весов


def ring_log_field(levels_db: np.ndarray, expected_db: np.ndarray, sd_db: np.ndarray | float = RING_SD_DB, block_s: int = BLOCK_S, weight: float = SECOND_WEIGHT) -> np.ndarray:
    """Логарифм произведения колец: сумма по блокам времени логарифмов правдоподобия уровня в каждой клетке; ряд уровней должен быть очищен."""
    log_p = np.zeros(len(expected_db))
    for i in range(0, len(levels_db), block_s):
        y = float(np.median(levels_db[i:i + block_s]))
        log_p += weight * (-0.5 * ((y - expected_db) / sd_db) ** 2 - np.log(sd_db))
    return log_p


def transition_prior(prev_post: np.ndarray, geodesic: np.ndarray, dt_s: float, stay_per_s: float = STAY_PER_S, pace_mps: float = PACE_MPS,
                     jump_share: float = JUMP_SHARE, jump_field: np.ndarray | None = None) -> np.ndarray:
    """Где человек теперь, если он был по прошлому апостериору: остался на месте с вероятностью stay_per_s за секунду или прошёл не дальше pace_mps * dt_s по проходам.

    Это сильная связь с прошлой траекторией: масса прошлого пятна переходит почти целиком в то же место или в ближайшие по пути, а не ложится гауссом на центр.
    jump_share остаётся на фоне jump_field (по умолчанию ровном; привычное поле направляет прыжок к местам, где человек бывает), чтобы сильное измерение могло перебить переход."""
    stay = stay_per_s ** max(dt_s, 0.0)
    reach = max(pace_mps * dt_s, 1e-6)
    kernel = np.exp(-0.5 * (np.where(np.isfinite(geodesic), geodesic, 1e9) / reach) ** 2)
    kernel /= kernel.sum(axis=1, keepdims=True)
    moved = prev_post @ kernel
    out = stay * prev_post + (1.0 - stay) * moved
    background = np.full(len(out), 1.0 / len(out)) if jump_field is None else jump_field / jump_field.sum()
    return (1.0 - jump_share) * out / out.sum() + jump_share * background


def filter_step(alpha: np.ndarray, block_log: np.ndarray, geodesic: np.ndarray, dt_s: float, floor_mask: np.ndarray | None = None,
                jump_field: np.ndarray | None = None, **transition) -> np.ndarray:
    """Один шаг фильтра по блокам времени: переход от прошлого шага, потом измерение блока (логарифм правдоподобия по клеткам); пол вне допустимого обнуляется."""
    predicted = transition_prior(alpha, geodesic, dt_s, jump_field=jump_field, **transition)
    post = normalised(np.log(np.maximum(predicted, 1e-300)) + block_log)
    if floor_mask is not None:
        post = post * floor_mask
    return post / post.sum()


def normalised(log_field: np.ndarray) -> np.ndarray:
    p = np.exp(log_field - log_field.max())
    return p / p.sum()


def posterior(ring_log: np.ndarray, floor_mask: np.ndarray, habit: np.ndarray | None = None, memory: np.ndarray | None = None) -> np.ndarray:
    """Произведение колец, привычного поля и памяти о прошлом; клетки вне допустимого пола получают нуль."""
    log_p = ring_log.copy()
    for field in (habit, memory):
        if field is not None:
            log_p += np.log(np.maximum(field, 1e-12))
    p = normalised(log_p) * floor_mask
    return p / p.sum()


def sample_cloud(post: np.ndarray, n: int = DEFAULT_SAMPLES, rng: np.random.Generator | None = None) -> np.ndarray:
    """Номера клеток n частиц, выбранных по плотности апостериора."""
    return (rng or np.random.default_rng()).choice(len(post), size=n, p=post)


def cloud_lobes(counts: np.ndarray, geodesic: np.ndarray, link_m: float = LOBE_LINK_M, min_share: float = LOBE_MIN_SHARE) -> list[Lobe]:
    """Пятна облака: связные по проходам группы клеток с заметным числом частиц, по убыванию доли; counts - частиц в каждой клетке или вероятность."""
    total = counts.sum()
    cells = [int(i) for i in np.argsort(-counts) if counts[i] >= min_share * counts.max() and counts[i] > 0]
    seen: set[int] = set()
    lobes = []
    for c in cells:
        if c in seen:
            continue
        group = [i for i in cells if i not in seen and geodesic[c, i] <= link_m]
        seen.update(group)
        weights = counts[group] / counts[group].sum()
        centre = group[int(np.argmin((geodesic[np.ix_(group, group)] * weights[None, :]).sum(axis=1)))]
        lobes.append(Lobe(tuple(group), float(counts[group].sum() / total), centre))
    return sorted(lobes, key=lambda lobe: -lobe.share)


def cloud_summary(post: np.ndarray, geodesic: np.ndarray, n: int = DEFAULT_SAMPLES, rng: np.random.Generator | None = None) -> list[Lobe]:
    """Облако из n выборок и его пятна с долями."""
    counts = np.bincount(sample_cloud(post, n, rng), minlength=len(post)).astype(float)
    return cloud_lobes(counts, geodesic)


def choose_lobe(lobes: list[Lobe], geodesic: np.ndarray, previous_cell: int | None = None, near_m: float = NEAR_PREVIOUS_M) -> Lobe | None:
    """Пятно с наибольшим весом: доля облака, умноженная на близость центра к прошлой координате по проходам; прошлой нет - просто доля."""
    if not lobes:
        return None
    return max(lobes, key=lambda lobe: lobe_weight(lobe, geodesic, previous_cell, near_m))


def lobe_weight(lobe: Lobe, geodesic: np.ndarray, previous_cell: int | None, near_m: float = NEAR_PREVIOUS_M) -> float:
    """Вес пятна при выборе точки: доля облака, умноженная на близость центра к прошлой координате по проходам."""
    return lobe.share if previous_cell is None else float(lobe.share * np.exp(-geodesic[previous_cell, lobe.centre] / near_m))


def hold_lobe(lobes: list[Lobe], geodesic: np.ndarray, previous_cell: int | None, hold: Hold, ratio: float = HOLD_RATIO, ticks: int = HOLD_TICKS,
              near_m: float = NEAR_PREVIOUS_M, reach_m: float = HOLD_REACH_M) -> tuple[Lobe | None, Hold]:
    """Пятно точки с липкостью: своё пятно это то, чьи клетки пересекаются с выбранным на прошлом тике, а без памяти - ближайшее к прошлой точке не дальше reach_m по проходам;
    оно остаётся выбранным, и самое тяжёлое пятно перебивает его, только если его доля облака больше в ratio раз ticks тиков подряд (претендент считается тем же, пока его
    клетки пересекаются с прежним претендентом).
    Перевес считается по голой доле: вес с близостью занижает дальнее пятно, и точка залипла бы навсегда. Прошлой точки нет или рядом с ней пятна нет - обычный выбор по весу.
    Так точка не перескакивает между модами распределения, которые делят массу примерно поровну, а настоящий уход на новое место проходит с задержкой ticks."""
    if not lobes:
        return None, Hold()
    current = next((lobe for lobe in lobes if hold.cells & frozenset(lobe.cells)), None)       # своё пятно по памяти: клетки пересекаются с прошлым выбором; точка может ещё идти к нему шагом
    if current is None and previous_cell is not None:
        nearest = min(lobes, key=lambda lobe: geodesic[previous_cell, list(lobe.cells)].min())
        current = nearest if geodesic[previous_cell, list(nearest.cells)].min() <= reach_m else None
    if current is None:
        best = max(lobes, key=lambda lobe: lobe_weight(lobe, geodesic, previous_cell, near_m))
        return best, Hold(frozenset(best.cells))
    best = max(lobes, key=lambda lobe: lobe.share)
    if best is current or best.share < ratio * current.share:
        return current, Hold(frozenset(current.cells))
    streak = hold.streak + 1 if hold.challenger & frozenset(best.cells) else 1
    if streak >= ticks:
        return best, Hold(frozenset(best.cells))
    return current, Hold(frozenset(current.cells), frozenset(best.cells), streak)


def step_towards(previous_cell: int | None, target: int, geodesic: np.ndarray, step_m: float = POINT_STEP_M) -> int:
    """Клетка точки после одного тика: цель, если она не дальше step_m по проходам от прошлой точки, иначе клетка в пределах step_m от прошлой, ближайшая по проходам к цели.
    Прошлой точки нет - цель. Так точка движется как человек, шагом, а не прыгает за центром пятна."""
    if previous_cell is None or geodesic[previous_cell, target] <= step_m:
        return target
    within = np.nonzero(geodesic[previous_cell] <= step_m)[0]
    return int(within[np.argmin(geodesic[within, target])])


def trimmed_centre(samples: np.ndarray, lobe: Lobe, geodesic: np.ndarray, trim: float = TRIM_SHARE) -> int:
    """Центр пятна по выборкам: самые далёкие от пятна частицы отбрасываются, затем берётся клетка с наименьшим средним путём до оставшихся."""
    inside = samples[np.isin(samples, lobe.cells)]
    if len(inside) == 0:
        return lobe.centre
    far = geodesic[lobe.centre, inside]
    kept = inside[far <= np.quantile(far, 1.0 - trim)]
    cells = np.array(sorted(set(kept.tolist())))
    weights = np.array([(kept == c).sum() for c in cells], dtype=float)
    return int(cells[int(np.argmin((geodesic[np.ix_(cells, cells)] * weights[None, :]).sum(axis=1)))])
