"""Люди не накладываются (SAM-105): общая механика отталкивания по позам, свободный пол и место перед лицом сидящего. План - выдуманный."""
import math

import pytest

from radar import presence_hub as ph
from radar import presence_nav as pnav
from radar import presence_seats as seats
from radar import presence_separate as ps
from tests.fakes_presence import two_room_layout

SOFA = {"title": "Диван", "kind": "furniture", "x": 4.0, "z": 1.0, "w": 2.0, "d": 0.9, "angle": 0.0}


def _row(person, x, z, pose="sit", **extra):
    return {"person": person, "x": x, "z": z, "pose": pose, **extra}


def _dist(rows, a, b):
    by = {r["person"]: r for r in rows}
    return math.hypot(by[a]["x"] - by[b]["x"], by[a]["z"] - by[b]["z"])


def _apart(rows, minimum):
    people = [r["person"] for r in rows]
    return all(_dist(rows, p, q) >= minimum - 1e-6 for i, p in enumerate(people) for q in people[i + 1:])


def _after_seats(rows):
    return seats.settle(rows, [SOFA])


@pytest.mark.parametrize("src, prepare, check", [
    ([_row("a", 5.0, 3.0), _row("b", 5.0, 3.0)], None,
     lambda out, src: abs(_dist(out, "a", "b") - ps.MIN_SEP_M) < 1e-6 and out[0]["x"] < out[1]["x"] and out[0]["pose"] == "sit"),    # двое в точке - на минимум
    ([_row("a", 0.0, 0.0), _row("b", 3.0, 0.0)], None, lambda out, src: out == src and out is not src),                          # далёкие не двигаются, вход не мутирует
    ([_row(p, 1.0, 1.0) for p in "abc"], None, lambda out, src: _apart(out, ps.MIN_SEP_M * 0.8) and ps.separate(src) == out),     # трое расходятся, стабильно
    ([{"person": "a", "x": None, "z": None}, _row("b", 1.0, 1.0)], None, lambda out, src: out[0]["x"] is None and out[1]["x"] == 1.0),   # без места не трогается
    ([_row("a", 0.0, 0.0, "stand"), _row("b", 0.4, 0.0, "stand")], None, lambda out, src: out == src),                           # стоящим 0,4 м хватает
    ([_row("a", 1.0, 1.0), _row("b", 1.2, 1.0, "stand")], None,
     lambda out, src: (out[0]["x"], out[0]["z"]) == (1.0, 1.0) and abs(_dist(out, "a", "b") - ps.MIN_SEP_M) < 1e-6),              # сидящий на месте, стоящий отходит
    ([_row("a", 0.0, 0.0, "stand", walking=True), _row("b", 0.5, 0.0, "stand", walking=True)], None,
     lambda out, src: abs(_dist(out, "a", "b") - ps.WALK_SEP_M) < 1e-6),                                                      # идущие держат больше
    ([_row("a", 0.0, 0.0, "lie", hug="b"), _row("b", 0.45, 0.0, "lie")], None, lambda out, src: out == src),                    # обнимающиеся не разводятся
    ([_row(p, 5.0, 1.45, "stand") for p in "abc"], _after_seats, lambda out, src: _apart(out, ps.MIN_SEP_M)),                    # после рассадки на диване
], ids=["two-in-a-spot", "far", "three-stable", "no-place", "standing-close", "sitter-stays", "walking", "hug", "after-seats"])
def test_people_are_pushed_apart_by_pose_distance(src, prepare, check):
    rows = prepare(src) if prepare else src
    assert check(ps.separate(rows), rows)


def _wall_at_x_one(a, b):
    return a[0] < 1.0 and b[0] < 1.0                                                  # справа от x=1 стена: туда не ходят


def _lattice(x, z, r, step=0.1):
    return [(i * step, j * step) for i in range(-20, 20) for j in range(-20, 20) if math.hypot(i * step - x, j * step - z) <= r and i * step < 1.0]


def _blocked_square(a, b):
    return not (0.0 <= b[0] <= 1.0 and 0.0 <= b[1] <= 1.0)                           # квадрат 1x1 непроходим


@pytest.mark.parametrize("src, free, nodes, check", [
    ([_row("a", 0.9, 0.0, "stand"), _row("b", 0.9, 0.0, "stand")], _wall_at_x_one, _lattice,
     lambda out: all(r["x"] < 1.0 for r in out) and _dist(out, "a", "b") >= ps.STAND_SEP_M - 1e-6),                  # толчок в стену - в ближайший свободный узел
    ([_row("a", 0.0, 0.0, "stand"), _row("b", 0.0, 0.1, "stand")], lambda a, b: b[1] >= 0.0, lambda x, z, r: [],
     lambda out: (out[0]["x"], out[0]["z"]) == (0.0, 0.0) and _dist(out, "a", "b") >= ps.STAND_SEP_M - 1e-6),        # зажатый стоит, толчок берёт пришедший
    ([_row("a", 0.0, 0.0, "stand"), _row("b", 0.1, 0.0, "stand")], lambda a, b: False, lambda x, z, r: [],
     lambda out: [(r["x"], r["z"]) for r in out] == [(0.0, 0.0), (0.1, 0.0)]),                                         # свободного места нет - никто не двигается
    ([_row("a", 0.5, 0.5), _row("b", 0.5, 0.5, "stand")], _blocked_square, lambda x, z, r: [(1.2, 0.5), (1.4, 0.5), (0.5, 1.6)],
     lambda out: _blocked_square((out[1]["x"], out[1]["z"]), (out[1]["x"], out[1]["z"])) and _dist(out, "a", "b") >= ps.MIN_SEP_M - 1e-6),   # застрявший выходит на свободное
], ids=["into-wall", "squeezed", "no-free-spot", "stuck-steps-out"])
def test_push_never_ends_in_an_obstacle(src, free, nodes, check):
    assert check(ps.separate(src, free=free, nodes=nodes))


def _sitter(face=0.0):
    return _row("a", 0.0, 0.0, "sit", face=face)


def _in_front_of(sitter, row):
    dx, dz = row["x"] - sitter["x"], row["z"] - sitter["z"]
    turn = math.atan2(dz, dx) - sitter["face"]
    return 0 < math.hypot(dx, dz) <= ps.FRONT_R_M and abs(math.atan2(math.sin(turn), math.cos(turn))) <= ps.FRONT_HALF_RAD


def test_nobody_is_left_standing_in_front_of_a_seated_person():
    """Стоящий перед лицом сидящего отходит (даже дальше минимума); сзади, без взгляда и на ходу не двигается; толчок у стены не кончается перед лицом."""
    rows = ps.separate([_sitter(), _row("b", 0.9, 0.0, "stand")])
    assert not _in_front_of(rows[0], rows[1]) and _dist(rows, "a", "b") >= ps.MIN_SEP_M
    for sitter, other in ((_sitter(), _row("b", -0.9, 0.0, "stand")), (_sitter(), _row("b", 0.9, 0.0, "stand", walking=True)),
                          (_row("a", 0.0, 0.0, "sit"), _row("b", 0.9, 0.0, "stand"))):
        assert ps.separate([sitter, other])[1]["x"] == other["x"]
    forced = ps.separate([_sitter(), _row("b", 0.8, 0.05, "stand")], free=lambda a, b: b[1] >= -0.1,
                         nodes=lambda x, z, r: [(0.9, 0.0), (0.2, 0.9), (0.2, -0.05)])
    assert not _in_front_of(forced[0], forced[1]) and forced[1]["z"] >= -0.1


def test_separation_on_a_plan_keeps_five_people_on_free_floor():
    """Пятеро в одной стоячей точке у каждого места плана расходятся только по свободному полу."""
    layout = two_room_layout()
    nav = pnav.Nav(layout)
    points = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: 0.0).place_points()
    standing = {k: p for k, p in points.items() if p["pose"] == "stand" and nav.segment_free((p["x"], p["z"]), (p["x"], p["z"]))}
    assert len(standing) >= 3
    stuck = []
    for key, point in sorted(standing.items()):
        rows = [_row(f"p{i}", point["x"], point["z"], "stand") for i in range(5)]
        stuck += [(key, r["person"]) for r in ps.separate(rows, free=nav.segment_free, nodes=nav.free_nodes)
                  if not nav.segment_free((r["x"], r["z"]), (r["x"], r["z"]))]
    assert stuck == []
