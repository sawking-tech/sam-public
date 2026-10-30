"""Карта проходимости квартиры (SAM-105): по плану видно, где стены, двери и мебель, и значит, где можно ходить.

Раньше трек шёл сквозь стены и диван, а стена между кухней и кабинетом пропадала. Корень один: положение считалось по прямым и по списку комнат, а в нём нет холла. Здесь план растрируется:
стены блокируют, двери их прорезают, мебель блокирует; «снаружи» - всё, куда можно попасть из-за границы плана, не пройдя
ни стену, ни проём. Наружные стены определяются по этому, а не по комнатам. Маршрут между точками ищется по свободным
клеткам и ведёт по серединам проходов. Модуль чистый: план приходит словарём, ничего не читает и не пишет.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

CELL_M = 0.05                 # клетка карты; дверь 0.7 м - 14 клеток, тонкая стена 0.08 м - 2
PAD_M = 0.6                   # запас карты вокруг габаритов стен: снаружи должна быть свободная клетка для заливки
WALL_MARGIN_M = 0.03          # стена в сетке чуть толще настоящей, чтобы по диагонали не просачивался путь
DOOR_REACH_M = 0.25           # прорезь двери выходит за модель с обеих сторон: у открытой двери центр модели смещён от стены на створку
DOOR_WORDS = ("дверь", "дверная")    # проёмы-двери по названию из проекта; окна стену не прорезают
SEAT_WORDS = ("стул", "кресл")        # сиденья: на них сидят, для пути они не преграда (как на странице, plan_scene.js)
WALK_OVER_WORDS = SEAT_WORDS               # через что путь идёт, как по свободному месту: стулья; диваны обходят
BLOCK_MIN_H_M = 0.4           # ниже - ковёр, ноутбук на столе: ходить можно
BLOCK_MIN_AREA_M2 = 0.25      # мельче - стул и тумба не перекрывают проход
NODE_STEP_M = 0.1             # шаг узлов, на которые можно переставить человека: каждая вторая клетка карты
AGENT_R_M = 0.15              # человек не идёт ближе этого к стене и мебели
CLEAR_M = 0.45                # путь предпочитает середину проходов: ближе этого к препятствию клетка дороже
CLEAR_COST = 2.0              # во сколько раз дороже клетка вплотную к препятствию
SUPPORT_WORDS = ("стул", "кресл", "диван", "кроват", "унитаз", "ванн")   # на чём можно сидеть и лежать
SUPPORT_RADIUS_M = 1.5        # опору ищем не дальше этого от устройства человека
FRONT_PROBE_M = 0.5           # на сколько отступить от длинной стороны предмета, чтобы понять, с какой стороны комната, а с какой стена или стеллаж; дальше щуп
                              # в узкой спальне вылетает за противоположную стену, и обе стороны выглядят одинаково пустыми
INTEREST_WORDS = ("стол", "тумб", "раковин", "холодил", "шкаф", "стенк", "гарнитур", "плит")   # к чему стоящий поворачивается лицом; у плиты готовят - лицом к ней, к стене
TABLE_RADIUS_M = 2.0           # стол, к которому сидящий поворачивается, не дальше этого от сиденья
INTEREST_RADIUS_M = 1.2       # предмет интереса не дальше этого от стоящего
WALL_RADIUS_M = 1.0           # стена ближе этого: стоящий лицом от неё, в комнату
PROBE_M = 0.12                # на сколько шагнуть от грани стены, чтобы понять, что за ней
PROBE_FRACTIONS = (0.15, 0.5, 0.85)   # где вдоль стены проверять, что с другой стороны
_MOVES = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


def _in_rect(xs: np.ndarray, zs: np.ndarray, cx: float, cz: float, w: float, d: float, angle: float) -> np.ndarray:
    """Клетки внутри прямоугольника плана; поворот предмета вокруг центра, как рисует страница (rotation.y = -angle)."""
    dx, dz = xs - cx, zs - cz
    lx, lz = dx * math.cos(angle) + dz * math.sin(angle), -dx * math.sin(angle) + dz * math.cos(angle)
    return (np.abs(lx) <= w / 2) & (np.abs(lz) <= d / 2)


def _near_segment(xs: np.ndarray, zs: np.ndarray, x1: float, z1: float, x2: float, z2: float, r: float) -> np.ndarray:
    vx, vz = x2 - x1, z2 - z1
    length2 = vx * vx + vz * vz
    t = np.clip(((xs - x1) * vx + (zs - z1) * vz) / length2, 0.0, 1.0) if length2 else np.zeros_like(xs)
    return (xs - (x1 + t * vx)) ** 2 + (zs - (z1 + t * vz)) ** 2 <= r * r


def _is_seat(obj: dict) -> bool:
    return any(word in (obj.get("title") or "").lower() for word in SEAT_WORDS)


def _walk_over(obj: dict) -> bool:
    return any(word in (obj.get("title") or "").lower() for word in WALK_OVER_WORDS)


def _is_solid(obj: dict) -> bool:
    """Непроходим ли предмет: явная метка `solid` в планировке важнее правила по размерам (ниже 0,4 м и мельче 0,25 кв. м - проходим, как и всё, на чём сидят)."""
    if "solid" in obj:
        return bool(obj["solid"])
    return obj.get("h", 0) >= BLOCK_MIN_H_M and obj["w"] * obj["d"] >= BLOCK_MIN_AREA_M2 and not _walk_over(obj)


def _is_door(obj: dict) -> bool:
    return obj.get("kind") == "opening" and any(word in (obj.get("title") or "").lower() for word in DOOR_WORDS)


class Nav:
    def __init__(self, layout: dict, cell: float = CELL_M) -> None:
        walls, objects = layout.get("walls") or [], layout.get("objects") or []
        self.objects = objects
        if not walls:
            raise ValueError("в плане нет стен: карту проходимости строить не из чего")
        self.cell, self.walls = cell, walls
        xs_all = [v for w in walls for v in (w["x1"], w["x2"])]
        zs_all = [v for w in walls for v in (w["z1"], w["z2"])]
        self.x0, self.z0 = min(xs_all) - PAD_M, min(zs_all) - PAD_M
        self.w, self.h = math.ceil((max(xs_all) + PAD_M - self.x0) / cell), math.ceil((max(zs_all) + PAD_M - self.z0) / cell)
        cols, rows = np.arange(self.w), np.arange(self.h)
        self._xs, self._zs = np.meshgrid(self.x0 + (cols + 0.5) * cell, self.z0 + (rows + 0.5) * cell)
        wall = np.zeros((self.h, self.w), bool)
        for s in walls:
            wall |= _near_segment(self._xs, self._zs, s["x1"], s["z1"], s["x2"], s["z2"], s["t"] / 2 + WALL_MARGIN_M)
        opening, doors, furniture = (np.zeros_like(wall) for _ in range(3))
        for o in objects:
            cx, cz, angle = o["x"] + o["w"] / 2, o["z"] + o["d"] / 2, o.get("angle", 0.0)
            if o.get("kind") == "opening":
                opening |= _in_rect(self._xs, self._zs, cx, cz, o["w"], o["d"], angle)
                if _is_door(o):
                    doors |= _in_rect(self._xs, self._zs, cx, cz, o["w"], o["d"] + 2 * DOOR_REACH_M, angle)
            elif _is_solid(o):
                furniture |= _in_rect(self._xs, self._zs, cx, cz, o["w"], o["d"], angle)
        closed = wall | opening                       # окно и дверь для «снаружи» - тоже граница: через них не заливаем
        labels, _ = ndimage.label(~closed)
        self.exterior = labels == labels[0, 0]
        self._wall = wall & ~doors
        self.blocked = self.exterior | (wall & ~doors) | furniture
        self._clear = ndimage.distance_transform_edt(~self.blocked) * cell      # метры до ближайшего препятствия
        self.passable = self._clear >= AGENT_R_M
        self._snap_index = ndimage.distance_transform_edt(~self.passable, return_indices=True)[1]
        self._csgraph: csr_matrix | None = None           # граф поиска пути, строится при первом маршруте

    def _cell(self, x: float, z: float) -> tuple[int, int]:
        return (min(self.h - 1, max(0, int((z - self.z0) / self.cell))), min(self.w - 1, max(0, int((x - self.x0) / self.cell))))

    def _point(self, row: int, col: int) -> tuple[float, float]:
        return (self.x0 + (col + 0.5) * self.cell, self.z0 + (row + 0.5) * self.cell)

    def is_exterior(self, x: float, z: float) -> bool:
        return bool(self.exterior[self._cell(x, z)])

    def floor_point(self, x: float, z: float) -> tuple[float, float]:
        """Точка остаётся, где была, если она на полу (на диване или кровати тоже); в стене или снаружи - сдвигается на ближайшее проходимое."""
        cell = self._cell(x, z)
        return self.snap(x, z) if self.exterior[cell] or self._wall[cell] else (x, z)

    def _support_near(self, x: float, z: float) -> dict | None:
        """Ближайшее сиденье, диван или кровать не дальше SUPPORT_RADIUS_M: на нём человек у устройства сидит или лежит."""
        best = None
        for o in self.objects:
            if o.get("kind") == "opening" or not any(w in (o.get("title") or "").lower() for w in SUPPORT_WORDS):
                continue
            cx, cz = o["x"] + o["w"] / 2, o["z"] + o["d"] / 2
            d = math.hypot(cx - x, cz - z)
            if d <= SUPPORT_RADIUS_M and (best is None or d < best[0]):
                best = (d, cx, cz, o)
        return None if best is None else {"cx": best[1], "cz": best[2], "angle": best[3].get("angle", 0.0)}

    def _nearest_wall_point(self, x: float, z: float) -> tuple[float, tuple[float, float]] | None:
        best = None
        for s in self.walls:
            vx, vz = s["x2"] - s["x1"], s["z2"] - s["z1"]
            l2 = vx * vx + vz * vz
            t = 0.0 if not l2 else min(1.0, max(0.0, ((x - s["x1"]) * vx + (z - s["z1"]) * vz) / l2))
            px, pz = s["x1"] + t * vx, s["z1"] + t * vz
            d = math.hypot(x - px, z - pz) - s["t"] / 2
            if best is None or d < best[0]:
                best = (d, (px, pz))
        return best

    def face_at(self, x: float, z: float, pose: str = "stand") -> float | None:
        """Куда повёрнут человек в точке по окружению (радианы плана): сидя - к ближайшему столу, иначе по сиденью; лёжа - вдоль кровати
        головой к изголовью; стоя - к ближайшему предмету интереса (стол, тумбы, шкаф), а у стены - от неё в комнату. Нет подсказки - None."""
        support = self._support_near(x, z)
        if pose == "sit" and support:
            table = self._table_near(support["cx"], support["cz"])
            return math.atan2(table[1] - support["cz"], table[0] - support["cx"]) if table else support["angle"] + math.pi / 2
        if pose == "lie" and support:
            return support["angle"] - math.pi / 2
        interest = [(math.hypot(o["x"] + o["w"] / 2 - x, o["z"] + o["d"] / 2 - z), o) for o in self.objects
                    if o.get("kind") != "opening" and any(w in (o.get("title") or "").lower() for w in INTEREST_WORDS)]
        interest = [(d, o) for d, o in interest if d <= INTEREST_RADIUS_M]
        if interest:
            o = min(interest, key=lambda r: r[0])[1]
            return math.atan2(o["z"] + o["d"] / 2 - z, o["x"] + o["w"] / 2 - x)
        wall = self._nearest_wall_point(x, z)
        return math.atan2(z - wall[1][1], x - wall[1][0]) if wall and wall[0] <= WALL_RADIUS_M else None

    def front_of(self, obj: dict, along_long: bool = False) -> float:
        """Куда смотрит сидящий на диване (радианы плана): в сторону длинной стороны, где свободнее. Спинка у стены или
        стеллажа - с той стороны, где ближе препятствия; угол модели из плана для этого ненадёжен.
        along_long - лицо вдоль длинной стороны (унитаз: бачок у стены на одном конце, сидят лицом к другому), иначе поперёк длинной (диван)."""""
        a = obj.get("angle", 0.0)
        cx, cz = obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2
        ex, ez = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))
        along, across, long_, short = (ex, ez, obj["w"], obj["d"]) if obj["w"] >= obj["d"] else (ez, ex, obj["d"], obj["w"])
        if along_long:
            along, across, long_, short = across, along, short, long_
        def room(sign: int) -> float:
            total = 0.0
            for k in (-1, 0, 1):
                r, c = self._cell(cx + along[0] * k * long_ / 3 + across[0] * sign * (short / 2 + FRONT_PROBE_M),
                                  cz + along[1] * k * long_ / 3 + across[1] * sign * (short / 2 + FRONT_PROBE_M))
                total += 0.0 if self.exterior[r, c] else float(self._clear[r, c])
            return total
        sign = 1 if room(1) >= room(-1) else -1
        return math.atan2(across[1] * sign, across[0] * sign)

    def door_direction(self, x: float, z: float) -> float | None:
        """Куда смотреть, чтобы видеть ближайшую дверь (радианы плана); дверей нет - None."""
        doors = [(o["x"] + o["w"] / 2, o["z"] + o["d"] / 2) for o in self.objects if _is_door(o)]
        if not doors:
            return None
        dx, dz = min(doors, key=lambda p: math.hypot(p[0] - x, p[1] - z))
        return math.atan2(dz - z, dx - x)

    def _table_near(self, x: float, z: float) -> tuple[float, float] | None:
        """Центр ближайшего стола не дальше SUPPORT_RADIUS_M: сидящий поворачивается к нему лицом, ноги под столом."""
        found = [(math.hypot(o["x"] + o["w"] / 2 - x, o["z"] + o["d"] / 2 - z), o) for o in self.objects
                 if o.get("kind") != "opening" and "стол" in (o.get("title") or "").lower()]
        near = [(d, o) for d, o in found if d <= TABLE_RADIUS_M]
        return None if not near else (lambda o: (o["x"] + o["w"] / 2, o["z"] + o["d"] / 2))(min(near, key=lambda r: r[0])[1])

    def outer_walls(self) -> list[int]:
        """Номера наружных стен плана: с какой-то из сторон в нескольких точках вдоль стены - «снаружи»."""
        out = []
        for i, s in enumerate(self.walls):
            dx, dz = s["x2"] - s["x1"], s["z2"] - s["z1"]
            length = math.hypot(dx, dz) or 1.0
            off = s["t"] / 2 + PROBE_M
            if any(self.is_exterior(s["x1"] + dx * f + side * -dz / length * off, s["z1"] + dz * f + side * dx / length * off)
                   for f in PROBE_FRACTIONS for side in (-1, 1)):
                out.append(i)
        return out

    def free_nodes(self, x: float, z: float, radius: float) -> list[tuple[float, float]]:
        """Узлы сетки (шаг NODE_STEP_M) в круге радиуса radius вокруг точки, где можно стоять: не стена, не мебель, не улица."""
        step = max(1, round(NODE_STEP_M / self.cell))
        r0, c0 = self._cell(x - radius, z - radius)
        r1, c1 = self._cell(x + radius, z + radius)
        nodes = []
        for row in range(r0 - r0 % step, r1 + 1, step):
            for col in range(c0 - c0 % step, c1 + 1, step):
                px, pz = self._point(row, col)
                if self.passable[row, col] and not self.exterior[row, col] and math.hypot(px - x, pz - z) <= radius:
                    nodes.append((px, pz))
        return nodes

    def stand_nodes(self, step_m: float) -> list[tuple[float, float]]:
        """Все узлы сетки с шагом step_m по всей квартире, где можно стоять: не стена, не мебель, не улица."""
        step = max(1, round(step_m / self.cell))
        rows, cols = np.nonzero((self.passable & ~self.exterior)[::step, ::step])
        return [self._point(int(r) * step, int(c) * step) for r, c in zip(rows, cols)]

    def snap(self, x: float, z: float) -> tuple[float, float]:
        """Ближайшая точка, где можно стоять: оценка положения притягивается к проходимому месту, не к стене и дивану."""
        r, c = self._cell(x, z)
        sr, sc = self._snap_index[0][r, c], self._snap_index[1][r, c]
        return self._point(int(sr), int(sc))

    def _graph(self) -> csr_matrix:
        """Граф проходимых клеток (8 соседей, диагональ только между двумя свободными), цена шага - длина на штраф за тесноту.
        Строится один раз целиком по сетке в numpy; поиск пути идёт в scipy на C, а не циклом на Python."""
        if self._csgraph is None:
            h, w = self.h, self.w
            index = np.arange(h * w).reshape(h, w)
            penalty = np.maximum(0.0, (CLEAR_M - self._clear) / CLEAR_M)
            src_all, dst_all, cost_all = [], [], []
            for dr, dc in _MOVES:
                r0, r1, c0, c1 = max(0, -dr), h - max(0, dr), max(0, -dc), w - max(0, dc)
                here = (slice(r0, r1), slice(c0, c1))
                there = (slice(r0 + dr, r1 + dr), slice(c0 + dc, c1 + dc))
                ok = self.passable[here] & self.passable[there]
                if dr and dc:
                    ok &= self.passable[r0 + dr:r1 + dr, c0:c1] & self.passable[r0:r1, c0 + dc:c1 + dc]       # между двумя препятствиями по диагонали не пролезть
                src_all.append(index[here][ok])
                dst_all.append(index[there][ok])
                cost_all.append((math.hypot(dr, dc) * (1.0 + CLEAR_COST * penalty[there]))[ok])
            self._csgraph = csr_matrix((np.concatenate(cost_all), (np.concatenate(src_all), np.concatenate(dst_all))), shape=(h * w, h * w))
        return self._csgraph

    def _astar(self, start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]] | None:
        """Самый дешёвый путь по клеткам (строка, столбец); None - клетки не связаны. Имя осталось от прежнего A*: контракт тот же."""
        source, target = start[0] * self.w + start[1], goal[0] * self.w + goal[1]
        dist, pred = dijkstra(self._graph(), directed=True, indices=source, return_predecessors=True)
        if not np.isfinite(dist[target]):
            return None
        path, node = [target], target
        while node != source:
            node = int(pred[node])
            path.append(node)
        return [divmod(n, self.w) for n in path[::-1]]

    def _line_free(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
        return all(self.passable[round(a[0] + (b[0] - a[0]) * k / n), round(a[1] + (b[1] - a[1]) * k / n)] for k in range(n + 1))

    def segment_free(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """Отрезок a-b целиком по проходимому месту: ни стены, ни мебели, ни дверного косяка по дороге."""
        return self._line_free(self._cell(*a), self._cell(*b))

    def _pull(self, path: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Выпрямление: из каждой вершины идём к самой дальней, до которой видно по свободным клеткам."""
        out, i = [path[0]], 0
        while i < len(path) - 1:
            j = next(j for j in range(len(path) - 1, i, -1) if j == i + 1 or self._line_free(path[i], path[j]))
            out.append(path[j])
            i = j
        return out

    def route(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Путь по точкам маршрута через двери и в обход мебели. Точки привязываются к проходимому месту; не нашёлся
        путь между соседними - этот участок остаётся прямой, и это видно по длине отрезка в ответе."""
        snapped = [self.snap(x, z) for x, z in points]
        out: list[tuple[float, float]] = []
        for a, b in zip(snapped, snapped[1:]):
            if a == b:
                continue
            path = self._astar(self._cell(*a), self._cell(*b))
            leg = [self._point(*c) for c in self._pull(path)] if path else [a, b]
            out.extend(leg if not out else leg[1:])
        return out or snapped

    def floor_runs(self) -> dict:
        """Пол квартиры для страницы: всё, что не «снаружи», рядами [ряд, от клетки, до клетки), клетка и начало сетки в метрах.
        Комнаты тут не нужны: пол - то, что огорожено стенами и проёмами, холл и коридор попадают в него сами."""
        inside = ~self.exterior
        runs: list[list[int]] = []
        for r in range(self.h):
            edges = np.diff(np.concatenate(([0], inside[r].astype(np.int8), [0])))
            runs += [[r, int(a), int(b)] for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])]
        return {"cell": self.cell, "x0": self.x0, "z0": self.z0, "w": self.w, "h": self.h, "runs": runs}

    def timed_track(self, waypoints: list[dict]) -> list[dict]:
        """Трек - координаты по времени: из размеченных точек {t, x, z} получается ряд {t, x, z} по проходимому пути.
        Время между двумя точками делится по длине участка, стоянка на месте остаётся двумя отсчётами с разным t."""
        out: list[dict] = []
        for prev, cur in zip([None] + waypoints, waypoints):
            x, z = self.snap(cur["x"], cur["z"])
            if prev is None:
                out.append({"t": cur["t"], "x": x, "z": z})
                continue
            start = (out[-1]["x"], out[-1]["z"])
            path = self.route([start, (x, z)]) if start != (x, z) else [start]
            path = path if path[-1] == (x, z) else path + [(x, z)]
            lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])] or [0.0]
            total, walked = sum(lengths), 0.0
            for point, length in zip(path[1:] or [(x, z)], lengths):
                walked += length
                out.append({"t": prev["t"] + (cur["t"] - prev["t"]) * (walked / total if total else 1.0), "x": point[0], "z": point[1]})
        return out


def describe(layout: dict) -> dict:
    """Что страница берёт у карты проходимости: номера наружных стен и пол. Нет стен - пусто, страница рисует как раньше."""
    try:
        nav = Nav(layout)
    except ValueError:
        return {}
    return {"outer_walls": nav.outer_walls(), "floor": nav.floor_runs()}
