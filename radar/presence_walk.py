"""Переход человека на новое место по реперным точкам (SAM-105): по найденному маршруту отмечается несколько условных
точек, их число растёт с длиной пути; человек перескакивает от точки к точке с паузой («эффект телепортации»).

При сдвиге места строится путь до нового места через реперные точки с шагом, точки
последовательно сдвигаются от одной к другой. Нужно на случай моргания: человек был на кухне,
выключил Wi-Fi, пришёл в зал, включил - видно, как он пришёл. Скачок положения любого человека (отладка или реальные
данные) идёт по маршруту плана. Модуль чистый: маршрут даёт навигация, время и скорость приходят параметрами; страница
сама плавно догоняет облако точек до каждой следующей реперной точки.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable


def densify(route: list[tuple[float, float]], spacing_m: float) -> list[tuple[float, float]]:
    """Реперные точки вдоль маршрута не реже, чем через spacing_m: чем длиннее путь, тем их больше; вершины маршрута
    (повороты, двери) остаются точками всегда, чтобы скачок между соседними не шёл сквозь стену."""
    if len(route) < 2 or spacing_m <= 0:
        return list(route)
    out = [route[0]]
    for (x1, z1), (x2, z2) in zip(route, route[1:]):
        parts = max(1, math.ceil(math.hypot(x2 - x1, z2 - z1) / spacing_m))
        out += [(x1 + (x2 - x1) * k / parts, z1 + (z2 - z1) * k / parts) for k in range(1, parts + 1)]
    return out


log = logging.getLogger("sam.radar")
LANE_M = 0.35             # расстояние между полосами идущих по одному маршруту: каждый идёт чуть левее или правее соседа
JUMP_M = 0.8              # сдвиг оценки меньше этого - дрейф и шум, человек не шёл
REROUTE_COOLDOWN_S = 2.0  # маршрут одного человека перестраивается не чаще: дрожащая оценка цели не должна считать путь на каждом кадре (путь по сетке стоит до 0,3 с)
ROUTE_KEY_M = 0.25        # точки маршрута округляются до этой клетки: тот же путь берётся из кэша
ABSENT_ENTRY_S = 20.0     # не было на карте дольше этого и вернулся - вошёл через вход; короче - мигание оценки, не уходил
GAP_MAX_S = 600.0         # пропал дольше этого - куда он делся, неизвестно: появляется на месте без ходьбы


@dataclass
class Walk:
    points: list[tuple[float, float]]      # реперные точки от старта до цели
    t0: float
    arrive_s: list[float]                   # когда человек оказывается в точке: номер точки * (рывок + пауза в позе)
    goal: tuple[float, float] = (0.0, 0.0)  # куда идёт: сменилась цель дальше JUMP_M - маршрут строится заново

    def at(self, now: float) -> tuple[float, float, float | None, bool]:
        """Последняя достигнутая реперная точка, направление движения (радианы, 0 - к +x) и признак «пришёл»."""
        elapsed = now - self.t0
        idx = max(i for i, t in enumerate(self.arrive_s) if t <= elapsed or i == 0)
        x, z = self.points[idx]
        prev = self.points[max(0, idx - 1)]
        heading = math.atan2(z - prev[1], x - prev[0]) if (x, z) != prev else None
        return x, z, heading, idx >= len(self.points) - 1


def shift_route(route: list[tuple[float, float]], offset_m: float, snap=None, free=None) -> list[tuple[float, float]]:
    """Промежуточные точки маршрута сдвигаются вбок на offset_m от направления движения; начало и цель на месте. snap возвращает сдвинутую точку на проходимое
    место, если она попала в стену или мебель. free(a, b) проверяет отрезок: если сдвинутый маршрут где-то пересекает препятствие, возвращается исходный
    (полосы без этой проверки гнали людей сквозь стены в четверти прыжков)."""
    if offset_m == 0.0 or len(route) < 3:
        return list(route)
    out = [route[0]]
    for prev, cur, nxt in zip(route, route[1:], route[2:]):
        dx, dz = nxt[0] - prev[0], nxt[1] - prev[1]
        length = math.hypot(dx, dz) or 1.0
        moved = (cur[0] - dz / length * offset_m, cur[1] + dx / length * offset_m)
        out.append(snap(*moved) if snap else moved)
    out.append(route[-1])
    if free is not None and not all(free(a, b) for a, b in zip(out, out[1:])):
        return list(route)
    return out


def start_walk(route: list[tuple[float, float]], now: float, pause_s: float, spacing_m: float,
               goal: tuple[float, float] | None = None, burst_s: float = 0.42, free=None) -> Walk | None:
    """Шаги по маршруту: рывок роя к точке, пауза в позе, следующий рывок. None - идти некуда (маршрут из одной точки)."""
    points = densify(route, spacing_m)
    if free is not None and not all(free(a, b) for a, b in zip(points, points[1:])):
        points = list(route)                                       # подотрезок цепляет косяк двери: идти по вершинам маршрута, они свободны по построению
    if len(points) < 2 or pause_s < 0:
        return None
    period = burst_s + pause_s
    return Walk(points, now, [k * period for k in range(len(points))], goal=goal or route[-1])


class Walkers:
    """Ходьба всех людей: оценка скачнула - человек идёт по маршруту, а не появляется на новом месте."""

    def __init__(self, route: Callable[[list[tuple[float, float]]], list[tuple[float, float]]] | None, tuning, lanes: dict[str, float] | None = None, snap=None, free=None, entry=None) -> None:
        self._entry = entry                                   # где вход в квартиру: () -> (x, z) или None; вернувшийся идёт от него
        self._missing: dict[str, float] = {}                  # человек -> с какого момента его нет на карте
        self._home: set[str] = set()                          # были на карте до перезапуска: появляются на месте, а не входят через вход
        self._t0: float | None = None                         # первый вызов: при старте радара все появляются на месте, а позже новый человек приходит через вход
        self._route = route                                   # навигация плана; нет плана со стенами - ходьбы нет
        self._tuning = tuning
        self._lanes, self._snap, self._free = lanes or {}, snap, free                     # полоса каждого человека (сдвиг вбок) и привязка сдвинутой точки к проходимому месту
        self._walks: dict[str, Walk] = {}
        self._reported: dict[str, tuple[float, float, float]] = {}      # человек -> (x, z, когда)
        self._routed_at: dict[str, float] = {}                          # человек -> когда строился маршрут
        self._cache: dict[tuple, list[tuple[float, float]]] = {}        # (откуда, куда) в клетках -> маршрут

    def reset(self) -> None:
        """Все идут заново с нуля: незавершённые ходы забыты, люди встают в свои точки сразу (автопилот выключен - дохода обратно нет)."""
        self._walks.clear(); self._reported.clear(); self._routed_at.clear()

    def seed_home(self, persons) -> None:
        """Эти люди были дома до перезапуска радара: впервые на карте они встают на место без прихода от двери."""
        self._home.update(persons)

    def on_map(self) -> list[str]:
        """Кто сейчас на карте (для записи состояния между перезапусками); до первого кадра - те, кто был дома до перезапуска: пустую карту записывать нельзя, она затрёт их."""
        return sorted(self._reported) if self._t0 is not None else sorted(self._home)

    def held_back(self, person: str, x: float, z: float, now: float) -> None:
        """Последний шаг конвейера вернул человека в прежнюю точку (прыжок сквозь стену): он там, а не в цели; следующий кадр строит путь от неё."""
        self._walks.pop(person, None)
        self._reported[person] = (x, z, now)

    def is_walking(self, person: str) -> bool:
        return person in self._walks

    def apply(self, rows: list[dict], now: float) -> list[dict]:
        if self._t0 is None:
            self._t0 = now
        present = {r["person"] for r in rows if r.get("x") is not None and r.get("z") is not None}
        for person in self._reported:
            if person not in present:
                self._missing.setdefault(person, now)         # пропал с карты: засекается, насколько
                self._walks.pop(person, None)
        return [self._one(dict(r), now) if r.get("x") is not None and r.get("z") is not None else r for r in rows]

    def _one(self, row: dict, now: float) -> dict:
        person, target = row["person"], (row["x"], row["z"])
        walk, last = self._walks.get(person), self._reported.get(person)
        away_since = self._missing.pop(person, None)
        new_later = last is None and person not in self._home and self._t0 is not None and now - self._t0 >= ABSENT_ENTRY_S         # впервые на карте не при старте и не был дома до него: пришёл
        if (new_later or (away_since is not None and now - away_since >= ABSENT_ENTRY_S)) and self._entry and (door := self._entry()):
            last, walk = (*door, now), None                    # вернулся после долгого отсутствия: входит через вход и идёт к месту
        if walk and math.dist(walk.goal, target) > JUMP_M and now - self._routed_at.get(person, -1e9) >= REROUTE_COOLDOWN_S:
            walk = None                                                # цель сменилась и маршрут не строился только что: от текущей точки
        if walk is None:
            walk = self._begin(person, last, target, now)
        if walk is None:
            self._walks.pop(person, None)
            self._reported[person] = (*target, now)
            return row
        self._walks[person] = walk
        x, z, heading, done = walk.at(now)
        if done:
            self._walks.pop(person, None)                              # пришёл: встаёт в запрошенную точку (на кровать, на диван), не в ближайшую проходимую
            x, z = target
            log.info("пришёл %s: (%.1f; %.1f)", person, x, z)
        else:
            row["pose"], row["face"], row["walking"] = "stand", heading if heading is not None else row.get("face"), True    # в пути: опоры (диван, кровать) его не занимают
            row["x"], row["z"] = x, z
        self._reported[person] = (x, z, now)
        return row

    def _begin(self, person: str, last, target, now: float) -> Walk | None:
        if self._route is None or last is None or now - last[2] > GAP_MAX_S or math.dist(last[:2], target) <= JUMP_M:
            return None
        self._routed_at[person] = now
        key = tuple(round(v / ROUTE_KEY_M) for v in (*last[:2], *target))
        if key not in self._cache:
            if len(self._cache) > 256:
                self._cache.clear()                                    # маршрутов мало; сброс проще вытеснения
            started = time.perf_counter()
            self._cache[key] = self._route([last[:2], target])
            log.info("маршрут %s: (%.1f; %.1f) -> (%.1f; %.1f), %d точек, %.0f мс", person, *last[:2], *target, len(self._cache[key]), (time.perf_counter() - started) * 1000)
        route = shift_route(self._cache[key], self._lanes.get(person, 0.0), self._snap, self._free)
        lane = self._lanes.get(person, 0.0)
        pause = self._tuning.walk_pause_s * (1 + 0.2 * lane / LANE_M)                  # каждый со своим темпом шагов: идут не в такт
        return start_walk(route, now, pause, self._tuning.walk_spacing_m, goal=target,
                          burst_s=self._tuning.walk_burst_s, free=self._free)      # цель - запрошенная: маршрут кончается на ближайшей проходимой точке, а цель может лежать на мебели
