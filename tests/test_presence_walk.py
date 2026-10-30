"""Ходьба по реперным точкам (SAM-105): скачок положения идёт по маршруту, дрейф и долгий пропуск - нет.
Ходьба отдельно получает маршрут выдуманной функцией (навигация - её вход), через хаб - настоящую навигацию выдуманного плана со стенами."""
import math

import pytest

from radar import presence_hub as ph
from radar import presence_tuning as tune
from radar import presence_walk as pw
from radar.presence_nav import Nav
from tests.fakes_presence import two_room_layout

ROUTE = [(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)]          # кухня -> проём -> зал: поворот в двери
DOOR = (0.0, 0.0)


def fake_route(points):
    return ROUTE if points[0] == ROUTE[0] and points[-1] == ROUTE[-1] else list(points)


def _walkers(route=fake_route, **kw):
    return pw.Walkers(route, tune.Tuning(walk_pause_s=0.5, walk_spacing_m=1.5, walk_burst_s=0.42), **kw)


def _row(x, z, person="resident-a"):
    return {"person": person, "x": x, "z": z, "pose": "sit", "face": 1.0}


def test_jump_walks_route_step_by_step_and_arrives_in_own_pose():
    w = _walkers()                                                        # шаг = рывок 0,42 с + пауза 0,5 с = 0,92 с
    assert w.apply([_row(0.0, 0.0)], 100.0)[0]["x"] == 0.0               # первое появление: без ходьбы
    seen = []
    for t in (100.0, 100.8, 101.1, 103.0, 120.0):
        r = w.apply([_row(3.0, 4.0)], t)[0]
        seen.append((round(r["x"], 2), round(r["z"], 2), r["pose"]))
    assert seen[0] == seen[1] == (0.0, 0.0, "stand")                      # старт с места, откуда он пропал; первый шаг через 0,92 с
    assert seen[2] == (1.5, 0.0, "stand")                                 # шаг в первую реперную точку
    assert seen[3][0] == 3.0 and 0 < seen[3][1] < 4                       # на четвёртой точке: в проёме, свернул к залу
    assert seen[-1] == (3.0, 4.0, "sit")                                  # пришёл: его настоящая поза


@pytest.mark.parametrize("route, start_t, move_t, goal", [
    (fake_route, 100.0, 101.0, (0.3, 0.2)),                               # дрейф оценки меньше скачка
    (fake_route, 200.0, 200.0 + pw.GAP_MAX_S + 1, (3.0, 4.0)),            # пропал надолго: где был, неизвестно
    (None, 1.0, 2.0, (3.0, 4.0)),                                         # плана нет: идти не по чему
], ids=["drift", "long_absence", "no_plan"])
def test_no_walk_for_drift_long_absence_or_no_plan(route, start_t, move_t, goal):
    """Человек появляется в новой точке сразу, без ходьбы."""
    w = _walkers(route)
    w.apply([_row(0.0, 0.0)], start_t)
    got = w.apply([_row(*goal)], move_t)[0]
    assert (got["x"], got["z"]) == goal and "walking" not in got


def test_moving_goal_restarts_from_current_point_without_rebuilding_every_frame():
    """Новая цель посреди хода - путь от текущей точки; дрожащая цель и цель на мебели не перестраивают путь каждый кадр;
    человек, возвращённый непрерывностью, идёт снова от того места, где стоит."""
    w = _walkers()
    w.apply([_row(0.0, 0.0)], 100.0)
    w.apply([_row(3.0, 4.0)], 100.0)
    mid = w.apply([_row(3.0, 4.0)], 102.0)[0]
    back = w.apply([_row(0.0, 0.0)], 102.0)[0]                            # цель сменилась: маршрут заново от текущей точки
    assert (back["x"], back["z"]) == (mid["x"], mid["z"])

    calls = []
    w = _walkers(lambda points: calls.append(points) or fake_route(points))
    w.apply([_row(0.0, 0.0)], 100.0)
    for k in range(40):                                                   # цель дрожит на метр каждые 0,2 с в течение 8 с
        w.apply([_row(3.0 + (k % 2), 4.0)], 100.0 + k * 0.2)
    assert len(calls) <= 5                                                # перестроение не чаще одного раза в REROUTE_COOLDOWN_S, а не 40

    target, builds = (3.0, 4.0), []
    w = _walkers(lambda points: builds.append(points) or [(0.0, 0.0), (3.0, 0.0), (3.0, 3.0)])    # маршрут кончается в метре от цели на кровати
    w.apply([_row(0.0, 0.0)], 100.0)
    seen = [w.apply([_row(*target)], 100.0 + k * 0.5)[0] for k in range(30)]
    assert len(builds) == 1                                               # маршрут построен один раз, не каждые две секунды
    assert (seen[-1]["x"], seen[-1]["z"]) == target and "walking" not in seen[-1]
    assert [round(r["x"], 1) for r in seen[:6]] != [0.0] * 6              # ход идёт, а не стоит на старте

    calls = []
    w = _walkers(lambda pts: calls.append(pts) or [pts[0], (5.0, 0.0), pts[1]])
    w.apply([{"person": "a", "x": 10.0, "z": 0.0}], 0.0)
    w.held_back("a", 10.0, 0.0, 0.0)
    out = w.apply([{"person": "a", "x": 0.0, "z": 0.0, "pose": "lie"}], 1.0)
    assert w.is_walking("a") and out[0]["pose"] == "stand" and calls[-1][0] == (10.0, 0.0)


def test_people_on_same_route_walk_in_lanes_unless_lane_hits_obstacle():
    """Двое на одном маршруте идут разными полосами; сдвиг двигает только средние точки и возвращает их на проходимое место,
    сдвиг через препятствие - исходный маршрут; подотрезок, задевающий препятствие, не берётся - ход идёт по вершинам."""
    lanes = {"resident-a": -0.35, "resident-b": 0.35}
    w = _walkers(lanes=lanes)
    start = [{"person": p, "x": 0.0, "z": 0.0, "pose": "stand", "face": 0.0} for p in lanes]
    w.apply(start, 100.0)
    goal = [{**r, "x": 3.0, "z": 4.0} for r in start]
    w.apply(goal, 100.0)
    got = {r["person"]: r for r in w.apply(goal, 103.0)}                  # через три секунды они уже в пути
    assert all(got[p]["walking"] for p in lanes)
    assert (got["resident-a"]["x"], got["resident-a"]["z"]) != (got["resident-b"]["x"], got["resident-b"]["z"])

    moved = pw.shift_route(ROUTE, 0.5)
    assert moved[0] == ROUTE[0] and moved[-1] == ROUTE[-1] and moved[1] != ROUTE[1]
    assert pw.shift_route(ROUTE, 0.0) == ROUTE and pw.shift_route(ROUTE[:2], 0.5) == ROUTE[:2]
    assert pw.shift_route(ROUTE, 0.5, snap=lambda x, z: (9.0, 9.0))[1] == (9.0, 9.0)
    assert pw.shift_route(ROUTE, 0.5, free=lambda a, b: True) != ROUTE
    assert pw.shift_route(ROUTE, 0.5, free=lambda a, b: False) == ROUTE

    straight = [(0.0, 0.0), (6.0, 0.0)]
    assert len(pw.start_walk(straight, 100.0, 0.5, 1.5, free=lambda a, b: True).points) == 5          # 6 м при шаге 1,5 м: пять точек
    assert pw.start_walk(straight, 100.0, 0.5, 1.5, free=lambda a, b: math.dist(a, b) > 5.0).points == straight


def _back_after_long_absence(w):
    w.apply([_row(3.0, 4.0)], 100.0)                                            # был на месте
    w.apply([], 110.0)                                                          # пропал с карты
    return w.apply([_row(3.0, 4.0)], 110.0 + pw.ABSENT_ENTRY_S + 5.0)[0], DOOR


def _flicker(w):
    w.apply([_row(3.0, 4.0)], 100.0)
    w.apply([], 105.0)
    return w.apply([_row(3.0, 4.0)], 108.0)[0], (3.0, 4.0)                     # три секунды - мигание оценки, не уходил


def _at_startup(w):
    return w.apply([_row(3.0, 4.0)], 100.0)[0], (3.0, 4.0)                     # старт: все на своих местах


def _late_first_sight(w):
    w.apply([_row(3.0, 4.0)], 100.0)
    rows = w.apply([_row(3.0, 4.0), _row(3.0, 4.0, "guest")], 100.0 + pw.ABSENT_ENTRY_S + 1)
    return [r for r in rows if r["person"] == "guest"][0], DOOR


@pytest.mark.parametrize("case", [_back_after_long_absence, _flicker, _at_startup, _late_first_sight],
                         ids=["long_absence", "flicker", "startup", "late_first_sight"])
def test_entry_walk_after_absence_or_late_first_sight_not_flicker(case):
    """Вернувшийся после долгого отсутствия и впервые увиденный позже старта входят от входа; мигание и старт - на месте."""
    w = _walkers(entry=lambda: DOOR)
    row, where = case(w)
    assert (row["x"], row["z"]) == where
    assert (row["pose"] == "stand") is (where == DOOR)


def test_hub_set_person_walks_with_walking_flag_until_arrival():
    """Через хаб на выдуманном плане со стенами: человек, переставленный в другую комнату, идёт через дверь с флагом walking,
    ни один шаг не проходит сквозь стену или мебель; пришёл - флага нет, стоит в месте."""
    layout = two_room_layout({"resident-a": {}})
    nav = Nav(layout)
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: 100.0)
    hub.set_person("resident-a", 0.9, "угол спальни")
    start = hub.identities(100.0)[0]
    hub.set_person("resident-a", 0.9, "угол зала")
    rows = [hub.identities(100.0 + k * 0.25)[0] for k in range(200)]
    goal = hub.place_points()["угол зала"]
    assert rows[0].get("walking") is True and (rows[0]["x"], rows[0]["z"]) == (start["x"], start["z"])     # ещё на старте, но в пути
    on_way = [r for r in rows if r.get("walking")]
    assert len(on_way) > 4 and any(abs(r["x"] - 3.0) < 0.6 for r in on_way)                         # шёл, и через дверь
    points = [(start["x"], start["z"])] + [(r["x"], r["z"]) for r in rows]
    assert all(nav.segment_free(a, b) for a, b in zip(points, points[1:]) if a != b)
    assert "walking" not in rows[-1] and (rows[-1]["x"], rows[-1]["z"]) == pytest.approx((goal["x"], goal["z"]), abs=0.3)
