"""Привычное поле и облако положения (SAM-118). Люди, места и числа выдуманные."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_cloud as pc
from radar import presence_habits as ph

HABITS = {"people": {"p1": [
    {"id": "sleep", "share": 0.9, "places": {"bed": 1.0}, "when": [{"days": "all", "from": "23:00", "to": "07:00"}]},
    {"id": "washing", "share": 0.4, "places": {"bath": 0.5, "toilet": 0.5}, "when": [{"days": "all", "from": "07:00", "to": "07:30"}]},
    {"id": "toilet", "share": 0.04, "places": {"toilet": 1.0}, "when": [{"days": "all", "from": "07:30", "to": "23:00"}]},
    {"id": "lunch", "share": 0.5, "places": {"kitchen": 1.0}, "when": [{"days": "all", "from": "12:00", "to": "13:00"}]},
    {"id": "work", "base": True, "weight": 1.0, "places": {"desk": 0.8, "sofa": 0.2}, "when": [{"days": "weekdays", "from": "09:00", "to": "18:00"}]},
    {"id": "evening", "base": True, "weight": 1.0, "places": {"sofa": 1.0}, "when": [{"days": "all", "from": "18:00", "to": "23:00"}]},
    {"id": "classes", "share": 0.9, "places": {ph.AWAY: 1.0}, "when": [{"days": ["tue", "wed"], "from": "10:00", "to": "16:00"}]},
]}}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 5 + day, hour, minute)          # 5 окт 2026 - понедельник


LUNCH = {"kitchen": 0.5, "toilet": 0.04, "desk": 0.46 * 0.8, "sofa": 0.46 * 0.2}     # обед и туалет заданы, остаток рабочему времени по весу
CLASSES = {"toilet": 0.04, ph.AWAY: 0.9, "desk": 0.06 * 0.8, "sofa": 0.06 * 0.2}     # занятия вне дома 0,9, остаток 0,06 - работе


@pytest.mark.parametrize("person, when, places", [
    # окно через полночь принадлежит дню начала и покрывает ночные часы; остальные 0,1 уходят в ровный фон при смешивании
    ("p1", at(2, 3, 20), {"bed": 0.9}),
    ("p1", at(2, 23, 30), {"bed": 0.9}),
    ("p1", at(0, 12, 30), LUNCH),
    # шаг времени округляет минуты вниз
    ("p1", at(0, 12, 59), LUNCH),
    # суббота не будни: рабочего стола нет
    ("p1", at(5, 10), {"toilet": 0.04}),
    # перечисленные дни недели выбирают окно, «нет дома» отдельной долей
    ("p1", at(1, 11), CLASSES),
    # чужой человек - приора нет
    ("nobody", at(0, 12), {}),
], ids=["night", "late-evening", "lunch", "time-step", "saturday", "away-days", "unknown"])
def test_habit_place_prior_for_person_at_time(person, when, places):
    prior = ph.place_prior(HABITS, person, when)
    assert prior == pytest.approx(places)
    assert ph.away_probability(prior) == pytest.approx(places.get(ph.AWAY, 0.0))


def test_habit_cell_field_spreads_place_shares():
    xs, zs = np.meshgrid(np.arange(0.0, 10.0, 0.5), np.arange(0.0, 6.0, 0.5))
    cells = np.column_stack([xs.ravel(), zs.ravel()])
    near = lambda x, z, r: np.hypot(cells[:, 0] - x, cells[:, 1] - z) < r
    # пятно на каждом известном месте по доле, ровный пол, сумма 1; «нет дома» на клетки не ложится
    field = ph.cell_field({"a": 0.75, "b": 0.25, ph.AWAY: 0.5}, cells, {"a": (2.0, 2.0), "b": (8.0, 4.0)})
    assert field.sum() == pytest.approx(1.0) and field.min() > 0
    assert field[near(2.0, 2.0, 1.5)].sum() > 2.5 * field[near(8.0, 4.0, 1.5)].sum()
    assert np.allclose(ph.cell_field({}, cells, {"a": (2.0, 2.0)}), 1.0 / len(cells))
    # место с зоной кладёт долю ровно на клетки зоны, место без зоны остаётся пятном
    sofa = (cells[:, 0] >= 1.0) & (cells[:, 0] <= 3.0) & (cells[:, 1] >= 1.0) & (cells[:, 1] <= 2.0)
    zoned = ph.cell_field({"a": 0.5, "b": 0.5}, cells, {"a": (2.0, 1.5), "b": (8.0, 4.0)}, floor_share=0.0, zones={"a": sofa})
    assert zoned.sum() == pytest.approx(1.0) and np.allclose(zoned[sofa], 0.5 / sofa.sum()) and zoned[~sofa & (cells[:, 0] < 5)].max() < 1e-4
    assert zoned[near(8.0, 4.0, 1.0)].sum() > 0.2                                                     # в метре от точки больше 40% доли 0,5
    empty_zone = ph.cell_field({"a": 1.0}, cells, {"a": (2.0, 1.5)}, zones={"a": np.zeros(len(cells), bool)})
    assert np.allclose(empty_zone, ph.cell_field({"a": 1.0}, cells, {"a": (2.0, 1.5)}))              # пустая зона - как без зоны


ROW = np.abs(np.arange(9)[:, None] - np.arange(9)[None, :]).astype(float)       # девять клеток в ряд, шаг 1 м


def at_cell(cell, n=9):
    mass = np.zeros(n)
    mass[cell] = 1.0
    return mass


def test_cloud_posterior_balances_ring_habit_and_transition():
    # кольцо уровня ровного сигнала - пик в клетке с совпавшим ожидаемым уровнем
    log_p = pc.ring_log_field(np.full(30, -70.0), np.array([-60.0, -70.0, -80.0]), 5.0)
    assert int(np.argmax(log_p)) == 1 and pc.normalised(log_p)[1] > 0.5
    # произведение кольца, привычки и памяти, вне пола ноль: привычка и память разводят три равных пятна кольца
    ring = np.log(np.array([0.30, 0.30, 0.30, 0.05, 0.03, 0.02]))
    habit = np.array([0.1, 0.6, 0.1, 0.1, 0.05, 0.05])
    memory = np.array([0.2, 0.5, 0.2, 0.05, 0.03, 0.02])
    post = pc.posterior(ring, np.array([1, 1, 1, 1, 1, 0.0]), habit, memory)
    assert post.sum() == pytest.approx(1.0) and post[5] == 0.0 and int(np.argmax(post)) == 1 and post[1] > 0.8
    # переход держит стоящего на месте и растягивает идущего по путям
    short = pc.transition_prior(at_cell(4), ROW, dt_s=1.0)
    assert short.argmax() == 4 and short[4] > 0.9 and short.sum() == pytest.approx(1.0)
    long = pc.transition_prior(at_cell(4), ROW, dt_s=20.0, stay_per_s=0.9)                       # почти наверняка ушёл
    assert long[4] < short[4] and long[2] > short[2]
    assert pc.transition_prior(at_cell(4), ROW, dt_s=0.0, jump_share=0.0)[4] == pytest.approx(1.0)
    # сильная память перебивает слабое кольцо в стороне, сильное кольцо - память
    memory = pc.transition_prior(at_cell(2), ROW, dt_s=2.0)
    mask = np.ones(9)
    assert pc.posterior(at_cell(7) * 0.5, mask, memory=memory).argmax() == 2
    assert pc.posterior(at_cell(7) * 12.0, mask, memory=memory).argmax() == 7


def _geodesic(nodes):
    return np.hypot(nodes[:, None, 0] - nodes[None, :, 0], nodes[:, None, 1] - nodes[None, :, 1])


def test_cloud_lobes_with_shares_and_choice_by_previous_cell():
    # облако - выборка апостериорного, пятна с долями и центром внутри
    nodes = np.array([[0.0, 0.0], [0.5, 0.0], [1.0, 0.0], [10.0, 0.0], [10.5, 0.0]])
    lobes = pc.cloud_summary(np.array([0.20, 0.30, 0.10, 0.25, 0.15]), _geodesic(nodes), n=20000, rng=np.random.default_rng(1))
    assert len(lobes) == 2 and abs(lobes[0].share - 0.6) < 0.02 and abs(lobes[1].share - 0.4) < 0.02
    assert lobes[0].centre in (0, 1, 2) and lobes[1].centre in (3, 4)
    assert sum(l.share for l in lobes) == pytest.approx(1.0)
    # пятно выбирается по доле и близости к прошлой координате, центр отбрасывает отбившиеся частицы
    geodesic = _geodesic(np.array([[0.0, 0.0], [0.5, 0.0], [10.0, 0.0], [10.5, 0.0]]))
    near, far = pc.Lobe((0, 1), 0.45, 0), pc.Lobe((2, 3), 0.55, 2)
    assert pc.choose_lobe([far, near], geodesic) is far                          # прошлой координаты нет: по доле
    assert pc.choose_lobe([far, near], geodesic, previous_cell=1) is near         # рядом с прошлым местом ближнее пятно перевешивает
    assert pc.choose_lobe([], geodesic) is None
    assert pc.trimmed_centre(np.array([0] * 40 + [1] * 9), near, geodesic) == 0
    assert pc.trimmed_centre(np.array([3]), near, geodesic) == near.centre       # в пятне частиц нет: центр пятна
