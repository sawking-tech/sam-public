"""Слой потока положения на странице (SAM-118): кольца срезов, зона по клеткам, пути, точка, цвет вероятности и маска пола.
Все сценарии flow.js и fields.js одним запуском node; контекст 2d - записывающий прокси, сценарий сводит записанное к сравнимому итогу."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

BOX = "{minX: 0, minZ: 0, w: 2, d: 1}"
CELLS = "[[0.5, 0.5], [1.0, 0.5], [1.5, 0.5]]"
PRELUDE = """
const recorder = () => { const calls = []; const g = new Proxy({}, { get: (t, k) => k in t ? t[k] : (...a) => calls.push([k, ...a]),
  set: (t, k, v) => { calls.push(['set', k, v]); t[k] = v; return true; } }); return [calls, g]; };
const count = (calls, k) => calls.filter(c => c[0] === k).length;
const styles = calls => calls.filter(c => c[0] === 'set' && c[1] === 'fillStyle').map(c => c[2]);
const draw = paint => { const [calls, g] = recorder(); paint(g); return calls; };
"""
SLICES_TWO = "[{ parts: { rings: [1, 1, 0] }, gamma: [1, 0, 0] }, { parts: { rings: [0, 1, 1] }, gamma: [0, 0.5, 0.5] }]"
SECOND_SLICES = [{"parts": {"rings": [1]}, "upto": [[0.1, 0.9]]}, {"parts": {"rings": [1]}, "upto": [[0.2, 0.8], [0.3, 0.7]]},
                 {"parts": {"rings": [1]}, "upto": [[0.4, 0.6], [0.5, 0.5], [0.6, 0.4]]}]
TRAIL = [{"x": 0.5, "z": 0.5}, None, {"x": 1.0, "z": 0.5}, {"x": 1.5, "z": 0.5}]
LAYERS = {                                                       # слой: сценарий и итог
    # доля максимума: клетки не ниже 0,4 от пика
    "cells_above": ("[m.cellsAbove([0.1, 0.5, 1.0, 0.4], 0.4), m.cellsAbove([0, 0], 0.4)]", [[1, 2, 3], []]),
    # зона по относительной вероятности: тонкая клетка пропущена
    "zone": (f"count(draw(g => m.paintZone(g, {CELLS}, [1.0, 0.5, 0.01], {BOX}, 0.5, 10)), 'fillRect')", 2),
    # поле: каждая доступная клетка, ноль чёрный
    "field_view": (f"{{ const s = styles(draw(g => m.paintZone(g, {CELLS}, [1.0, 0.5, 0.0], {BOX}, 0.5, 10, 0))); return [s.length, s[2].slice(0, 11)]; }}", [3, "rgba(0,0,0,"]),
    # исключена одна клетка контура (тёмная), законные клетки контура - три, цветом вероятности
    "excluded_and_legal": (f"{{ const c = draw(g => {{ m.paintExcluded(g, {CELLS}, {SLICES_TWO}, {BOX}, 0.5, 10); m.paintLegal(g, {CELLS}, {SLICES_TWO}, {BOX}, 0.5, 10); }}); "
                           "const s = styles(c); return [count(c, 'fillRect'), s[0].startsWith('rgba(14,20,44,'), s.slice(1).some(x => x.startsWith('rgba(14,20,44,') || x.startsWith('rgba(205,240,255,'))]; }",
                           [4, True, False]),
    # выбранный срез ограничивает рисунок, срез без колец пропущен
    "chosen_slice_and_no_rings": (f"[count(draw(g => m.paintLegal(g, {CELLS}, [{{ gamma: [1, 0, 0], parts: {{ rings: [1, 0, 0] }} }}, {{ gamma: [0, 1, 0], parts: {{ rings: [0, 1, 0] }} }}], {BOX}, 0.5, 10, 0)), 'fillRect'), "
                                  f"draw(g => m.paintExcluded(g, {CELLS}, [{{ gamma: [1, 0, 0], parts: {{}} }}], {BOX}, 0.5, 10)).length, "
                                  f"draw(g => m.paintLegal(g, {CELLS}, [{{ gamma: [1, 0, 0], parts: {{}} }}], {BOX}, 0.5, 10)).length]", [1, 0, 0]),
    # контур - тонкая полоса клеток колец у их максимума
    "contour": (f"[m.contourCells({{ parts: {{ rings: [1.0, 0.9, 0.5, 0.79] }} }}), m.contourCells({{ parts: {{}} }}), "
                f"count(draw(g => m.paintContour(g, {CELLS}, {{ parts: {{ rings: [1.0, 0.85, 0.2] }} }}, {BOX}, 0.5, 10)), 'fillRect')]", [[0, 1], [], 2]),
    # контуры секунд до выбранной включительно
    "contours_upto": (f"count(draw(g => m.paintContours(g, {CELLS}, [{{ parts: {{ rings: [1, 0, 0] }} }}, {{ parts: {{ rings: [0, 1, 0] }} }}, {{ parts: {{ rings: [0, 0, 1] }} }}], {BOX}, 0.5, 10, 1)), 'fillRect')", 2),
    # маска пола непрозрачна только на полу
    "floor_mask": (f"{{ const k = fields.floorMask((x, z) => x < 1.0, {BOX}, 10); return [k.w, k.h, k.data[3], k.data[15 * 4 + 3]]; }}", [20, 10, 255, 0]),
}
MARKS = {
    "point_and_previous": (f"count(draw(g => m.paintPoint(g, {{x: 1.0, z: 0.5}}, {{x: 0.5, z: 0.5}}, {BOX}, 10)), 'arc')", 2),
    "no_point": (f"draw(g => m.paintPoint(g, null, null, {BOX}, 10)).length", 0),
    # диаметр в метрах не больше телефона, холст 40 пикселей на метр
    "point_phone_size": (f"Math.max(...draw(g => m.paintPoint(g, {{x: 1.0, z: 0.5}}, {{x: 0.5, z: 0.5}}, {BOX}, 40)).filter(c => c[0] === 'arc').map(c => 2 * c[3] / 40)) <= 0.16", True),
    # секунда 2 без точки пропущена, секунда 4 не дошла
    "trail": (f"{{ const c = draw(g => m.paintTrail(g, {json.dumps(TRAIL)}, {BOX}, 40, 2)); return [count(c, 'arc'), count(c, 'moveTo'), count(c, 'lineTo'), c.filter(x => x[0] === 'fillText').map(x => x[1])]; }}",
              [2, 1, 1, ["1", "3"]]),
    # цепочка идёт от нулевой точки до окна через первую ко второй
    "trail_from_zero": (f"{{ const c = draw(g => m.paintTrail(g, [{{x: 1, z: 0.5}}, {{x: 1.5, z: 0.5}}], {BOX}, 10, 1, {{x: 0.5, z: 0.5}})); return [c.filter(x => x[0] === 'fillText').map(x => x[1]), count(c, 'lineTo')]; }}",
                        [["0", "1", "2"], 2]),
    "paths": (f"{{ const c = draw(g => m.paintPaths(g, {CELLS}, [[0, 1, 2], [0, 1, 1]], {BOX}, 10, 40)); return [count(c, 'moveTo'), count(c, 'lineTo'), count(c, 'arc')]; }}", [2, 4, 2]),
    # роутер слева: конец стрелки на 0,6 м влево от точки
    "ascent": (f"{{ const l = draw(g => m.paintAscent(g, {{x: 1.0, z: 0.5}}, {{dx: -1, dz: 0, db_per_m: 8.2}}, {BOX}, 10)).filter(c => c[0] === 'lineTo'); return [l.length, l[0][1], l[0][2]]; }}", [3, 4, 5]),
    "ascent_flat": (f"draw(g => m.paintAscent(g, {{x: 1.0, z: 0.5}}, {{dx: 0, dz: 0, db_per_m: 0}}, {BOX}, 10)).length", 0),
    "ascent_no_point": (f"draw(g => m.paintAscent(g, null, {{dx: -1, dz: 0, db_per_m: 8.2}}, {BOX}, 10)).length", 0),
}
SCENARIOS = {
    "rgb": "[0, 0.12, 0.5, 1, 7, -1].map(fields.probabilityRgb)",
    "blob_colour": ("{ const c = []; const g = new Proxy({}, { get: (t, k) => k === 'createRadialGradient' ? () => ({ addColorStop: (o, s) => c.push(s) }) : () => {}, set: () => true }); "
                    f"fields.paintBlobs(g, [{{x: 1, z: 1, p: 1.0}}], fields.probabilityRgb, {BOX}, 10, 1); return c[0]; }}"),
    "legend": "[m.legendText([0.02, 0.314, 0.1]), m.legendGradient()]",
    "second_1": f"m.secondSlices({json.dumps(SECOND_SLICES)}, 1).map(s => s.gamma)",
    "second_2": f"m.secondSlices({json.dumps(SECOND_SLICES)}, 2).map(s => s.gamma)",
    "second_bounds": f"[m.secondSlices({json.dumps(SECOND_SLICES)}, 99).length, m.secondSlices({json.dumps(SECOND_SLICES)}, 0).length]",
    "cell_at": f"[m.cellAt({CELLS}, 1.05, 0.52, 0.5), m.cellAt({CELLS}, 1.0, 0.95, 0.5)]",
    **{f"layer:{k}": expr for k, (expr, _) in LAYERS.items()},
    **{f"mark:{k}": expr for k, (expr, _) in MARKS.items()},
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("flow.js", SCENARIOS, extra={"fields": "fields.js"}, prelude=PRELUDE)


def test_probability_colour_scale_and_legend(js):
    zero, low, mid, high, over, under = js["rgb"]
    assert zero == [0, 0, 0] and low[2] > low[0] and mid[1] > 150 and high[0] > high[2]        # чёрный, синий, зелёный, красный
    assert over == high and under == zero                                                         # зажим
    assert js["blob_colour"].startswith("rgba(255,60,40,")                                        # пятна по той же шкале
    text, gradient = js["legend"]
    assert text == "0% ... 31.4%" and gradient.startswith("linear-gradient(90deg, rgb(0,0,0) 0%")


@pytest.mark.parametrize("layer", list(LAYERS))
def test_flow_layer_paints_the_expected_cells(js, layer):
    assert js[f"layer:{layer}"] == LAYERS[layer][1]


def test_second_view_picks_the_slices_and_cell_under_the_pointer(js):
    assert js["second_1"] == [[0.1, 0.9]] and js["second_2"] == [[0.2, 0.8], [0.3, 0.7]]           # законные клетки выбранной секунды, не всего окна
    assert js["second_bounds"] == [3, 1]
    assert js["cell_at"] == [1, -1]                                                               # стена и мебель без клетки


@pytest.mark.parametrize("case", list(MARKS))
def test_flow_marks_point_trail_paths_and_ascent(js, case):
    got, expected = js[f"mark:{case}"], MARKS[case][1]
    assert got == (pytest.approx(expected) if isinstance(expected, list) and all(isinstance(v, (int, float)) for v in expected) else expected)
