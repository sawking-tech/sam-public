"""Слой карты покрытия сети (SAM-118): полосы, текстура, стрелки и тексты coverage.js - все сценарии одним запуском node."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

BANDS = [{"min_db": -60.0, "name": "отлично"}, {"min_db": -67.0, "name": "хорошо"}, {"min_db": -75.0, "name": "средне"},
         {"min_db": -82.0, "name": "слабо"}, {"min_db": None, "name": "почти нет"}]
B = json.dumps(BANDS)
TEXTURE = {"w": 2, "h": 2, "i0": 0, "k0": 0, "cell_m": 1.0, "levels": [-50, -71, -85, -62], "bands": BANDS}
# сетка 5 x 5 по метру: уровень растёт влево на 2 дБ на клетку; правый столбец за стеной на 20 дБ ниже; нижняя строка вне пола
SLOPE = {"w": 5, "h": 5, "i0": 0, "k0": 0, "cell_m": 1.0, "bands": BANDS,
         "levels": [-40 - 2 * x - (20 if x == 4 else 0) for _ in range(5) for x in range(5)]}
ROUTER, RW, RH = (3.0, 2.0), 16, 12
AROUND_ROUTER = {"w": RW, "h": RH, "i0": 0, "k0": 0, "cell_m": 0.5, "bands": BANDS,
                 "levels": [-40 - 6 * ((((x + 0.5) * 0.5 - ROUTER[0]) ** 2 + ((y + 0.5) * 0.5 - ROUTER[1]) ** 2) ** 0.5) for y in range(RH) for x in range(RW)]}
WALK = {"cells": [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]], "speed_m_s": 1.4, "noise_db": 5.5,
        "fields": [{"seconds": 1, "to": [1, 1, 1], "delta_db": [8.0, 0.0, -2.0], "median_db": 3.0, "above_noise": 0.5, "falling": 0.5}]}
PLACES = [{"label": "рядом", "dbm": -50.2, "band": "отлично"}, {"label": "середина", "dbm": -66, "band": "хорошо"}, {"label": "далеко", "dbm": -81, "band": "слабо"}]
MODELS = [{"key": "phy1-ap0", "label": "5 ГГц", "iface": "phy1-ap0"}, {"key": "phy0-ap0", "label": "2,4 ГГц", "iface": "phy0-ap0"}]
CONNECTED = [{"name": "Redmi-Note-13-Pro-5G", "iface": "phy0-ap0"}, {"name": "S24-Ultra", "iface": "phy1-ap0"}, {"name": "X", "iface": "phyZ"}]
SCENARIOS = {
    "bands": f"[-50, -60, -66.9, -70, -80, -99].map(v => m.bandName(v, {B}))",
    "texture": f"Array.from(m.coverageTexture({json.dumps(TEXTURE)}, (x, z) => !(x > 1 && z > 1)).data)",
    "stops": f"m.colourStops({B}).map(s => s.level)",
    "colours": f"[-71, -54, -94, -67.25, -40, -120].map(v => m.bandColour(v, {B}))",
    "steps": f"Array.from({{length: 60}}, (_, k) => m.bandColour(-60 - k * 0.5, {B}))",
    "slope_arrows": f"m.gradientArrows({json.dumps(SLOPE)}, (x, z) => z < 4, 1, 0.5)",
    "flat_arrows": f"m.gradientArrows({json.dumps({**SLOPE, 'levels': [-50.0] * 25})}, () => true, 1, 0.5)",
    "sparse_arrows": f"m.gradientArrows({json.dumps(SLOPE)}, () => true, 3).length",
    "router_arrows": f"m.gradientArrows({json.dumps(AROUND_ROUTER)}, () => true, 1)",
    "walk_arrows": f"m.walkArrows({json.dumps(WALK)}, 1)",
    "walk_arrows_other_second": f"m.walkArrows({json.dumps(WALK)}, 7)",
    "summary": f"m.coverageSummary({json.dumps(PLACES)}, 2)",
    "connected": f"m.connectedText({json.dumps(CONNECTED)}, {json.dumps(MODELS)})",
    "walk_summary": f"m.walkSummary({json.dumps(WALK)}, 1)",
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("coverage.js", SCENARIOS)


def test_coverage_texture_colours_cells_smoothly_by_band_and_hides_non_floor(js):
    assert js["bands"] == ["отлично", "отлично", "хорошо", "средне", "слабо", "почти нет"]   # первая достигнутая полоса, нижняя - всё ниже
    data = js["texture"]
    assert data[(1 * 2 + 0) * 4:(1 * 2 + 0) * 4 + 4] == [31, 157, 85, 150]         # клетка (0,0) -50 дБ: нижняя строка текстуры, зелёная
    assert data[(1 * 2 + 1) * 4:(1 * 2 + 1) * 4 + 4] == [242, 201, 76, 150]        # клетка (1,0) -71 дБ: опорный жёлтый
    assert data[(0 * 2 + 1) * 4 + 3] == 0                                          # клетка (1,1) вне пола: прозрачная
    assert js["stops"] == [-54.0, -63.5, -71.0, -78.5, -94.0]                      # середины полос, крайние на 6 и 12 дБ от границы
    assert js["colours"] == [[242, 201, 76], [31, 157, 85], [214, 69, 69], [191, 198, 75], [31, 157, 85], [214, 69, 69]]
    steps = js["steps"]
    assert max(abs(a[i] - b[i]) for a, b in zip(steps, steps[1:]) for i in range(3)) <= 10        # соседние полдецибела: плавно


def test_gradient_arrows_point_to_the_router_and_skip_flat_cells_walls_and_outside(js):
    arrows = js["slope_arrows"]
    cells = {(a["x"], a["z"]): a for a in arrows}
    assert all(a["dx"] == pytest.approx(-1.0) and a["dz"] == pytest.approx(0.0) and a["slope"] == pytest.approx(2.0) for a in arrows)
    assert (2.5, 2.5) in cells and (2.5, 4.5) not in cells                         # середина поля; нижняя строка вне пола
    assert cells[(3.5, 2.5)]["slope"] == pytest.approx(2.0)                        # сосед за стеной отброшен, производная по левому
    assert js["flat_arrows"] == [] and js["sparse_arrows"] == 2                    # шаг 3: столбец за стеной без пары соседей пропущен
    far = [a for a in js["router_arrows"] if ((ROUTER[0] - a["x"]) ** 2 + (ROUTER[1] - a["z"]) ** 2) ** 0.5 > 0.5]
    assert len(far) > 100 and all(a["dx"] * (ROUTER[0] - a["x"]) + a["dz"] * (ROUTER[1] - a["z"]) > 0 for a in far)


def test_walk_arrows_point_to_the_step_end_coloured_by_noise(js):
    assert [(a["x"], a["dx"], a["kind"]) for a in js["walk_arrows"]] == [(0.0, 1.0, "above"), (2.0, -1.0, "falling")]   # клетка роутера без стрелки
    assert js["walk_arrows_other_second"] == []


@pytest.mark.parametrize("scenario, text", [
    ("summary", "лучше всего: рядом -50 дБм (отлично); середина -66 дБм (хорошо). Слабее всего: далеко -81 дБм (слабо)"),
    ("connected", "Redmi - 2,4 ГГц; S24 - 5 ГГц; X - phyZ"),
    ("walk_summary", "шаг к роутеру 1 с (1.4 м): медиана разницы 3 дБ, выше шума 5.5 дБ в 50% клеток, падение в 50%"),
])
def test_coverage_texts_name_places_devices_and_the_walk(js, scenario, text):
    assert js[scenario] == text
