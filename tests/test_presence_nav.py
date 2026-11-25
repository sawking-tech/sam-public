"""Карта проходимости радара (SAM-105): наружные стены по плану, двери, мебель, маршрут и привязка. План выдуманный:
квартира 6 x 4 м, внутренняя стена на x = 3 с дверью, диван в левой комнате."""
import math

import pytest

from radar import presence_nav as nav

OUTER = [(0, 0, 6, 0), (6, 0, 6, 4), (6, 4, 0, 4), (0, 4, 0, 0)]


def wall(x1, z1, x2, z2, t=0.2, h=2.5):
    return {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": t, "h": h}


def door(cx, cz, width=0.8, angle=math.pi / 2, title="Дверная рама"):
    return {"id": "d", "title": title, "kind": "opening", "x": cx - width / 2, "z": cz - 0.05, "w": width, "d": 0.1, "h": 2.0, "angle": angle}


def sofa(cx, cz, w=2.0, d=0.9, title="Шкаф"):
    """Предмет-преграда для путей; диван преградой не считается, его берут тесты ниже явно."""
    return {"id": "s", "title": title, "kind": "furniture", "x": cx - w / 2, "z": cz - d / 2, "w": w, "d": d, "h": 0.87, "angle": 0.0}


def layout(objects=None):
    walls = [wall(*o) for o in OUTER] + [wall(3, 0, 3, 4, t=0.1)]
    return {"walls": walls, "objects": [door(3, 2.0)] + (objects or [])}


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def samples(route, step=0.05):
    """Точки вдоль маршрута через каждые step метров: выпрямленный путь состоит из отрезков, вершины мало что говорят."""
    out = []
    for a, b in zip(route, route[1:]):
        n = max(1, int(dist(a, b) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    return out + route[-1:]


def crosses_wall_x3_outside_the_door(route):
    pts = samples(route)
    return any((a[0] - 3) * (b[0] - 3) < 0 and abs((a[1] + b[1]) / 2 - 2.0) > 0.4 for a, b in zip(pts, pts[1:]))


def passable_at(n, x, z):
    """Проходима ли точка: отрезок нулевой длины свободен."""
    return n.segment_free((x, z), (x, z))


def furniture(title, x, z, w=1.0, d=1.0, h=0.9, **extra):
    return {"id": title, "title": title, "kind": "furniture", "x": x, "z": z, "w": w, "d": d, "h": h, "angle": 0.0, **extra}


def test_nav_outer_walls_floor_and_exterior():
    n = nav.Nav(layout())
    # наружные стены - периметр, внутренняя стена не наружная и без списка комнат (его у плана нет)
    assert n.outer_walls() == [0, 1, 2, 3]
    # снаружи плана - exterior, комнаты нет
    assert n.is_exterior(-0.3, 2.0) and not n.is_exterior(1.5, 2.0) and not n.is_exterior(4.5, 2.0)
    # пол - то, что огорожено стенами, без наружности
    floor = n.floor_runs()
    covered = sum(b - a for _, a, b in floor["runs"]) * floor["cell"] ** 2
    assert 5.5 * 3.5 < covered < 6.5 * 4.5                   # квартира 6 x 4 м, стены с обеих сторон в пределах допуска
    assert floor["runs"] and all(0 <= r < floor["h"] for r, _, _ in floor["runs"])
    # страница получает наружные стены и пол; без стен - ничего, а карта без стен отвергается с причиной
    d = nav.describe(layout())
    assert d["outer_walls"] == [0, 1, 2, 3] and d["floor"]["runs"]
    assert nav.describe({"walls": [], "objects": []}) == {}
    with pytest.raises(ValueError, match="нет стен"):
        nav.Nav({"walls": [], "objects": []})


def test_route_goes_through_the_door_and_around_obstacles():
    # между комнатами - через дверь, не сквозь стену
    route = nav.Nav(layout()).route([(1.0, 0.7), (5.0, 3.3)])
    assert not crosses_wall_x3_outside_the_door(route)
    assert any(abs(p[0] - 3) < 0.05 and abs(p[1] - 2.0) < 0.4 for p in samples(route))
    # окно стену не открывает: пути нет, и участок остаётся прямым сквозь стену
    window_only = nav.Nav({"walls": layout()["walls"], "objects": [door(3, 2.0, title="Окно, двойное")]})
    assert crosses_wall_x3_outside_the_door(window_only.route([(1.0, 0.7), (5.0, 0.7)]))
    # мебель - в обход, концы у заданных точек
    route = nav.Nav(layout([sofa(1.5, 2.0)])).route([(0.5, 2.0), (2.7, 2.0)])
    assert not any(abs(p[0] - 1.5) < 1.0 and abs(p[1] - 2.0) < 0.45 for p in samples(route))
    assert dist(route[0], (0.5, 2.0)) < 0.3 and dist(route[-1], (2.7, 2.0)) < 0.3
    # низкое не мешает: путь над ноутбуком прямой
    laptop = furniture("Ноутбук", 1.0, 1.0, w=0.6, d=0.6, h=0.19)
    straight = nav.Nav(layout([laptop])).route([(0.5, 1.3), (2.6, 1.3)])
    assert straight and all(abs(p[1] - 1.3) < 0.05 for p in straight)


@pytest.mark.parametrize("thing, passable", [
    (furniture("Стул вращающийся", 1.2, 1.7, w=0.6, d=0.6, h=0.85), True),        # на стул садятся с места
    (sofa(1.5, 2.0, title="Диван угловой"), False),                               # диваны обходят, садятся из точки рядом
    (sofa(1.5, 2.0), False),                                                      # шкаф
    (furniture("Тумба", 1.0, 1.5, h=0.2, solid=True), False),                     # метка solid сильнее правила размера
    (furniture("Тумба", 1.0, 1.5, h=0.2), True),                                  # без метки низкий предмет проходим
    (furniture("Арка", 1.0, 1.5, h=2.0, solid=False), True),                      # высокий, но открытый по метке
], ids=["chair", "sofa", "wardrobe", "low-solid", "low", "tall-open"])
def test_obstacles_by_kind_height_and_solid_mark(thing, passable):
    assert passable_at(nav.Nav(layout([thing])), 1.5, 2.0) is passable


def test_points_snapped_out_of_walls_and_furniture():
    n = nav.Nav(layout([sofa(1.5, 2.0)]))
    # точка в мебели уходит из неё
    x, z = n.snap(1.5, 2.0)
    assert not (abs(x - 1.5) <= 1.0 and abs(z - 2.0) <= 0.45)
    # призрачная точка в стене (x = 3, не в проёме) уходит на пол, на диване - остаётся: место законное
    x, z = n.floor_point(3.0, 0.5)
    assert abs(x - 3.0) > 0.05 and not crosses_wall_x3_outside_the_door([(x, z), (x, z)])
    assert n.floor_point(1.5, 2.0) == (1.5, 2.0)


SINK = furniture("Тумбы с раковиной", 4.0, 3.0, d=0.6)
STOVE = furniture("Газовая плита", 4.3, 3.4, w=0.5, d=0.5, solid=True)
TABLE = furniture("Стол", 4.3, 2.2, w=0.5, d=0.5, h=0.75, solid=True)
TOILET = furniture("Унитаз", 2.8, 0.0, w=0.4, d=0.65, h=0.78)                      # бачок у стены z = 0
SOUTH = math.pi / 2


def face(objects, x, z):
    return nav.Nav(layout(objects)).face_at(x, z)


def front(obj, **kwargs):
    n = nav.Nav(layout([obj]))
    return n.front_of(n.objects[-1], **kwargs)


@pytest.mark.parametrize("facing, expected, tol", [
    (lambda: face([SINK], 4.5, 2.4), SOUTH, 0.01),                               # у тумб лицом к ним (они на +z)
    (lambda: face([SINK], 1.0, 0.3), SOUTH, 0.1),                                # у наружной стены z = 0 - лицом в комнату
    (lambda: face([SINK], 1.5, 2.0), None, None),                                # в середине комнаты подсказки нет
    (lambda: front(sofa(1.5, 0.6, title="Диван")), SOUTH, 1e-6),                # спинка у стены z = 0: смотрит в комнату
    (lambda: front(sofa(1.5, 3.4, title="Диван")), -SOUTH, 1e-6),               # спинка у стены z = 4
    (lambda: front(TOILET, along_long=True), SOUTH, 1e-6),                      # унитаз - вдоль длинной стороны, от бачка
    (lambda: face([STOVE, TABLE], 4.55, 3.25), SOUTH, 0.01),                    # у плиты лицом к ней, хотя стол близко за спиной
], ids=["object-of-interest", "away-from-wall", "open-room", "sofa-north", "sofa-south", "toilet", "stove"])
def test_person_facing_at_place(facing, expected, tol):
    got = facing()
    assert got is None if expected is None else abs(got - expected) < tol


def test_timed_track_keeps_pauses_and_goes_through_doors():
    n = nav.Nav(layout())
    track = n.timed_track([{"t": 100.0, "x": 1.0, "z": 0.7}, {"t": 130.0, "x": 1.0, "z": 0.7}, {"t": 190.0, "x": 5.0, "z": 3.3}])
    assert [p["t"] for p in track] == sorted(p["t"] for p in track)
    assert track[0]["t"] == 100.0 and track[-1]["t"] == 190.0
    assert any(p["t"] == 130.0 for p in track)               # стоянка 100-130 не потерялась
    assert not crosses_wall_x3_outside_the_door([(p["x"], p["z"]) for p in track])


def test_nav_front_of_bed_is_the_free_side_in_a_narrow_room():
    """Кровать в 0,1 м от стены в узкой комнате: щуп дальше полуметра вылетал за противоположную стену, и свободная сторона выбиралась наугад."""
    walls = [wall(0, 0, 5.8, 0), wall(5.8, 0, 5.8, 2.65), wall(5.8, 2.65, 0, 2.65), wall(0, 2.65, 0, 0)]
    bed = {"id": "b", "title": "Кровать", "kind": "furniture", "x": 3.32, "z": 0.2, "w": 1.6, "d": 2.0, "h": 0.89, "angle": math.pi / 2}   # одна сторона у стены, от другой до стены около 0,9 м
    assert abs(nav.Nav({"walls": walls, "objects": [bed]}).front_of(bed) - SOUTH) < 1e-6                                                # свободная сторона на +z
