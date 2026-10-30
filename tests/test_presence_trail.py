"""Дорожка следа (SAM-105): скругление углов и разводка проходов в trail.js, один запуск node на файл."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

CORNER = [{"x": 0, "z": 0}, {"x": 10, "z": 0}, {"x": 10, "z": 10}]
SHORT_LEGS = [{"x": 0, "z": 0}, {"x": 0.4, "z": 0}, {"x": 0.4, "z": 0.4}, {"x": 0.8, "z": 0.4}]
TWO = [{"x": 0, "z": 0}, {"x": 1, "z": 1}]
ONE = [{"x": 2, "z": 3}]


@pytest.fixture(scope="module")
def js():
    smooth = lambda points: f"m.smoothTrail({json.dumps(points)})"
    return run_node_scenarios("trail.js", {
        "corner": smooth(CORNER), "short": smooth(SHORT_LEGS), "two": smooth(TWO), "one": smooth(ONE),
        "passes": "{ const steps = []; "
                  "for (let i = 0; i < 40; i++) steps.push({x: i * 0.1, z: 0, ux: 1, uz: 0}); "
                  "for (let i = 0; i < 40; i++) steps.push({x: 3.9 - i * 0.1, z: 0, ux: -1, uz: 0}); "
                  "const out = m.spreadPasses(steps); return { first: out.slice(0, 40).map(s => s.z), back: out.slice(40).map(s => s.z) }; }",
    })


def test_trail_smoothing_and_pass_spread(js):
    """Острый угол скругляется малой дугой в радиусе, длинные отрезки прямые; короткие отрезки не дают дугам выйти за путь,
    одна и две точки не меняются; обратный проход по тому же коридору сдвинут вбок, одиночный не тронут."""
    path = js["corner"]
    assert path[0] == {"x": 0, "z": 0} and path[-1] == {"x": 10, "z": 10}
    assert min(((p["x"] - 10) ** 2 + p["z"] ** 2) ** 0.5 for p in path) > 0.1                  # вершина (10; 0) срезана дугой
    assert max(abs(p["z"]) for p in path if p["x"] < 9.6) < 1e-9                                 # длинный отрезок прямой: стены не режутся
    assert all(((p["x"] - 10) ** 2 + p["z"] ** 2) ** 0.5 <= 0.36 or abs(p["z"]) < 1e-9 or abs(p["x"] - 10) < 1e-9 for p in path)   # отклонение только у угла

    assert all(-1e-9 <= p["x"] <= 0.8 + 1e-9 and -1e-9 <= p["z"] <= 0.4 + 1e-9 for p in js["short"])
    assert js["two"] == TWO and js["one"] == ONE

    assert all(abs(z) < 1e-9 for z in js["passes"]["first"])                                   # первый проход на своём месте
    back = js["passes"]["back"][20:]
    assert all(abs(abs(z) - 0.07) < 1e-9 for z in back) and len({round(z, 6) for z in back}) == 1   # обратный сдвинут на 7 см вбок, одним боком
