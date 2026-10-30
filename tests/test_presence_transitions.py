"""Переходы между состояниями человека на странице (SAM-105): одна таблица правил решает, когда запускается рой. Один запуск node на файл."""
import json
import math

import pytest

from tests.fakes_presence import run_node_scenarios

BASE = {"x": 1.0, "z": 1.0, "face": 0.0, "figKey": "male|stand|cool"}

# (предыдущее, новое, названные переходы, идёт ли человек, летит ли рой)
CASES = {
    "appear": (None, BASE, [], True, False),
    "same": (BASE, dict(BASE), [], True, False),
    "move": (BASE, {**BASE, "x": 1.5}, ["move"], True, True),
    "move_standing": (BASE, {**BASE, "x": 1.5}, ["move"], False, False),
    "pose": (BASE, {**BASE, "figKey": "male|lie|side"}, ["pose"], True, False),
    "turn": (BASE, {**BASE, "face": 1.0}, ["turn"], True, False),
    "noise": (BASE, {**BASE, "x": 1.1, "z": 1.05, "face": 0.2}, [], True, False),           # шум оценки и лёгкий поворот - не переход
    "short_arc": (BASE, {**BASE, "face": 6.2}, [], True, False),                           # 6,2 рад - почти тот же угол по короткой дуге
    "all": (BASE, {"x": 3.0, "z": 1.0, "face": 3.0, "figKey": "male|sit|x"}, ["move", "pose", "turn"], True, True),
}
SLIDES = [((0.5, 0), True), ((0.6, 0.7), True), ((3, 0), False), ((0, 6), False)]     # метр - скольжение, дальше сразу: через стены не скользят


@pytest.fixture(scope="module")
def js():
    scenarios = {}
    for name, (prev, nxt, _, walking, _) in CASES.items():
        scenarios[name] = (f"{{ const t = m.transitionsBetween({json.dumps(prev)}, {json.dumps(nxt)}); "
                           f"return {{ names: t, swarm: m.flightFor(t, {json.dumps(walking)}) }}; }}")
    scenarios["slides"] = f"[{', '.join(f'm.slides({a}, {b})' for (a, b), _ in SLIDES)}]"
    scenarios["turn_steps"] = ("{ let a = 3.0; const steps = []; "
                               "for (let i = 0; i < 40; i++) { a = m.smoothAngle(a, -3.0, m.TURN_RATE, 0.1); steps.push(a); } return steps; }")
    return run_node_scenarios("transitions.js", scenarios)


@pytest.mark.parametrize("case", list(CASES))
def test_state_change_names_transition_and_triggers_swarm_only_for_walk_move(js, case):
    """Появление и неизменное состояние - не переход, шум не считается; рой летит только при сдвиге на ходу, поворот и поза - плавно."""
    _, _, names, _, swarm = CASES[case]
    assert js[case]["names"] == names and js[case]["swarm"] is swarm


def test_only_a_short_shift_slides(js):
    assert js["slides"] == [expected for _, expected in SLIDES]


def test_turn_takes_shortest_arc_and_converges(js):
    steps = js["turn_steps"]
    assert all(b > a for a, b in zip([3.0] + steps, steps))                       # от 3,0 к -3,0 кратчайшим путём через π (вверх), не через ноль
    assert abs(steps[-1] - (-3.0 + 2 * math.pi)) < 0.01                            # и сходится к цели с точностью до оборота
