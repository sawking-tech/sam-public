"""Карта посещений на странице радара (SAM-105): чистый heat.js проверяется в node без браузера, один запуск node на файл."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

MANY = [0.3] * 300 + [2.0] * 200 + [8.0] * 200 + [30.0] * 60 + [420.0]
TRAIL = [[0, 0, 0.4], [1, 0, 0.4], [2, 0, 0.4], [3, 0, 0.4], [10, 10, 0.4], [20, 20, 5.0], [30, 30, 0.1]]
RECORD = [[i, 0, 3.0 + i * 0.1] for i in range(40)] + [[50, 0, 400.0]]


def scale(values, probes):
    return f"(() => {{ const t = m.heatScale({json.dumps(values)}); return {json.dumps(probes)}.map(v => t(v)); }})()"


SCENARIOS = {
    "small": scale([0.1, 0.4, 3, 9, 30], [0.1, 0.4, 3, 20]),
    "many": scale(MANY, [0.1, 0.3, 2.0, 8.0, 30.0, 420.0]),
    "few": scale([1.5] * 10, [1.5]),
    "trail": f"m.trailCells({json.dumps(TRAIL)}).map(c => c.slice(0, 2).join(','))",
    "bell": "{ const f = m.heatField([[6, 6, 40]], 0, 0, 13, 13); const row = Array.from(f.values.slice(32 * f.w, 33 * f.w)); "
            "const two = m.heatField([[6, 6, 40], [7, 6, 40]], 0, 0, 13, 13); "
            "return {w: f.w, h: f.h, row, max: Math.max(...f.values), mid: two.values[32 * two.w + 35], top: Math.max(...Array.from(two.values))}; }",
    "capped": f"m.capCells({json.dumps(RECORD)})",
    "capped_small": "m.capCells([[0, 0, 400], [1, 0, 1]])",
    "thin_trail": "{ const f = m.heatField([[6, 6, 0.5], [7, 6, 0.5], [8, 6, 0.5], [9, 6, 0.5]], 0, 0, 16, 13); const v = Array.from(f.values); "
                  "const t = m.heatScale(v, m.HEAT_FIELD_FLOOR_S); return {peak: Math.max(...v), seen: v.filter(x => t(x) !== null).length}; }",
}


@pytest.fixture(scope="module")
def answers():
    return run_node_scenarios("heat.js", SCENARIOS)


def _small_data_scale_grows_with_time(t):
    return t[0] is None and 0 < t[1] < t[2] < t[3] <= 1                 # мало данных: шум не рисуется, запасная шкала по секундам


def _top_tenth_is_hot(t):
    # 30 с - верхняя десятая (оранжевая), 2 с - синяя-зелёная, рекорд 420 с шкалу не съедает, шум не рисуется
    return t[0] is None and t[1] > 0 and t[2] < 0.8 and t[3] > t[2] and t[4] > 0.9 and t[5] == 1


def _few_points_are_not_orange(t):
    return t[0] < 0.5                                                   # запасной шкале нужно двадцать секунд


@pytest.mark.parametrize("scenario, holds", [
    ("small", _small_data_scale_grows_with_time),
    ("many", _top_tenth_is_hot),
    ("few", _few_points_are_not_orange),
])
def test_heat_scale_is_relative_with_a_noise_floor(answers, scenario, holds):
    assert holds(answers[scenario])


def test_heat_trail_cells_keep_trails_and_drop_flicker(answers):
    kept = answers["trail"]
    assert "1,0" in kept and "2,0" in kept                              # цепочка слабых клеток - дорожка
    assert "10,10" not in kept and "30,30" not in kept                  # одиночка и совсем слабая - шум
    assert "20,20" in kept                                              # где стояли - рисуется и без соседей


def test_heat_field_is_a_smooth_capped_bell_where_a_trail_survives(answers):
    bell = answers["bell"]
    assert bell["w"] == 65 and bell["h"] == 65
    row = bell["row"]
    peak = row.index(max(row))
    assert 30 <= peak <= 34 and all(a <= b for a, b in zip(row[:peak], row[1:peak + 1])) and all(a >= b for a, b in zip(row[peak:], row[peak + 1:]))   # колокол без ступенек клетки
    assert 0.03 < bell["max"] / 40 < 0.5 and row[0] < 0.05 * max(row)                                                                                # размыто по соседям и спадает к краям
    assert bell["mid"] > 0.9 * bell["top"]                                                                                                            # соседние клетки сливаются в одну зону
    capped = answers["capped"]
    peak_s = max(c[2] for c in capped)
    assert peak_s < 10 and capped[-1][2] == peak_s                      # рекорд 400 с срезан до верхнего края обычных клеток до размытия
    assert answers["capped_small"] == [[0, 0, 400], [1, 0, 1]]          # на малых данных процентиль не трогает ничего
    thin = answers["thin_trail"]
    assert thin["peak"] < 0.5 and thin["seen"] > 250                    # дорожка в клетку размыта слабее 0,5 с, но мягкий порог поля её оставляет
