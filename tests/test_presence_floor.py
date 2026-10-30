"""Пол по комнатам (SAM-118): тип пола в точке, резка клеток по типам, рисунок типа и наложения; все сценарии floor_texture.js одним запуском node."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

ROOMS = [{"x": 0, "z": 0, "w": 6, "d": 3, "floor_kind": "parquet"}, {"x": 6, "z": 0, "w": 3, "d": 3, "floor_kind": "checker"},
         {"x": 5, "z": 1, "w": 2, "d": 1, "floor_kind": "linoleum"}, {"x": 20, "z": 0, "w": 1, "d": 1}]
CUT = {"x0": 0, "z0": 0, "cell": 1, "w": 12, "h": 4, "runs": [[1, 0, 9]]}                 # строка z=1..2, клетки x=0..8; комнаты добавляют строки 0 и 2
GAP = {"x0": 0, "z0": 0, "cell": 1, "w": 12, "h": 4, "runs": [[1, 0, 2], [1, 4, 6]]}      # проходимые клетки с дырой x=2..3 (мебель)
KINDS = {"parquet": "#b89c7b", "planks": "#c4ab8c", "linoleum": "#cdc3aa", "checker": "#cfcfc4", "tile_small": "#d9b8ae"}
SCENARIOS = {
    "kinds": f"[[1,1],[7.5,1.5],[5.5,1.5],[50,50],[20.5,0.5]].map(([x, z]) => m.floorKindAt({json.dumps(ROOMS)}, x, z))",
    "cut": f"m.floorCellsByKind({json.dumps(CUT)}, {json.dumps(ROOMS)})",
    "gap": f"m.floorCellsByKind({json.dumps(GAP)}, {json.dumps(ROOMS[:1])})",
    "paint": ("{ const out = {}; for (const kind of Object.keys(m.FLOOR_KINDS)) { const calls = []; "
              "const g = { set fillStyle(v) { calls.push(['fill', v]); }, fillRect(...a) { calls.push(['rect', ...a]); } }; "
              "m.paintFloor(g, kind, 160, 120, 80, 0, 0); out[kind] = { first: calls[0][1], rects: calls.filter(c => c[0] === 'rect').length }; } return out; }"),
    "joints": ("{ const out = {}; for (const kind of ['parquet', 'planks']) { let rects = 0; "
               "const g = { set fillStyle(v) {}, fillRect() { rects++; } }; m.paintFloor(g, kind, 800, 600, 80, 0, 0); out[kind] = rects; } return out; }"),
    "checker": ("{ const rects = []; let fill = null; const g = { set fillStyle(v) { fill = v; }, fillRect(x, y, w, h) { rects.push([fill, w]); } }; "
                "m.paintFloor(g, 'checker', 48, 24, 80, 0, 0); return rects.slice(1, 4); }"),
    "stipple": ("{ const modes = []; let arcs = 0, fills = 0; const g = { set globalCompositeOperation(v) { modes.push(v); }, set fillStyle(v) {}, beginPath() {}, "
                "moveTo() {}, arc() { arcs++; }, fill() { fills++; } }; m.stipple(g, 160, 160, 160, 0, 0); return { modes, arcs, fills }; }"),
    "stipple_window": ("{ const radii = []; const g = { set globalCompositeOperation(v) {}, set fillStyle(v) {}, beginPath() {}, moveTo() {}, "
                       "arc(x, y, r) { radii.push(r); }, fill() {} }; m.stipple(g, 320, 320, 160, 0, 0, { x0: 0, y0: 0, x1: 80, y1: 80 }); return { arcs: radii.length, r: radii[0] }; }"),
    "glow": ("{ const modes = []; let fill = null, rect = null; const g = { set globalCompositeOperation(v) { modes.push(v); }, set fillStyle(v) { fill = v; }, "
             "fillRect(...a) { rect = a; } }; m.glow(g, 'checker', 100, 50); return { modes, fill, rect }; }"),
    "patch": ("{ const log = []; let fill = null, dx = 0, dy = 0; const g = { save() { log.push('save'); }, restore() { log.push('restore'); }, "
              "translate(x, y) { dx += x; dy += y; log.push(['translate', x, y]); }, set fillStyle(v) { fill = v; }, set globalCompositeOperation(v) {}, "
              "beginPath() {}, moveTo() {}, arc() {}, fill() {}, fillRect(x, y, w, h) { log.push(['rect', fill, x + dx, y + dy]); } }; "
              "m.paintPatch(g, 'checker', { x0: 24, y0: 0, x1: 72, y1: 24 }, 80, 0, 0); return log; }"),
    "speckle": ("{ const dots = (fn) => { const out = []; let dx = 0, dy = 0; const g = { save() {}, restore() {}, translate(x, y) { dx += x; dy += y; }, "
                "set fillStyle(v) {}, set globalCompositeOperation(v) {}, beginPath() {}, moveTo() {}, arc() {}, fill() {}, "
                "fillRect(x, y, w, h) { if (w === 2 && h === 1) out.push(`${(x + dx).toFixed(3)},${(y + dy).toFixed(3)}`); } }; fn(g); return out; }; "
                "const full = dots(g => m.paintFloor(g, 'linoleum', 240, 120, 80, 0.5, 0.25)); "
                "const part = dots(g => m.paintPatch(g, 'linoleum', { x0: 90, y0: 30, x1: 210, y1: 120 }, 80, 0.5, 0.25)); "
                "const inner = p => { const [x, y] = p.split(',').map(Number); return x >= 90 && x < 210 && y >= 30 && y < 120; }; "
                "return { full: full.filter(inner).length, missing: full.filter(inner).filter(p => !part.includes(p)).length }; }"),
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("floor_texture.js", SCENARIOS)


def covered(runs):
    return {(r, i) for r, a, b in runs for i in range(a, b)}


def test_floor_kind_comes_from_the_smallest_room_and_cells_are_cut_by_kind(js):
    assert js["kinds"] == ["parquet", "checker", "linoleum", "linoleum", "linoleum"]   # меньшая комната решает, вне комнаты и без типа - по умолчанию
    pieces = [p for runs in js["cut"].values() for p in runs]
    assert sum(b - a for _, a, b in pieces) == 27 and len(covered(pieces)) == 27       # три строки по девять клеток, без повторов
    row = {kind: [p for p in runs if p[0] == 1] for kind, runs in js["cut"].items()}
    assert row["linoleum"] == [[1, 5, 7]] and row["checker"] == [[1, 7, 9]] and row["parquet"] == [[1, 0, 5]]
    floor = covered(js["gap"]["parquet"])
    assert {(1, 2), (1, 3), (0, 0), (2, 5)} <= floor                                   # пол под мебелью внутри комнаты есть
    assert (1, 8) not in floor and (3, 0) not in floor                                 # вне комнаты и вне проходимых клеток нет


@pytest.mark.parametrize("kind", list(KINDS))
def test_each_floor_kind_paints_its_base_and_pattern(js, kind):
    painted = js["paint"][kind]
    assert painted["first"] == KINDS[kind] and painted["rects"] > 10                  # своя основа и рисунок на весь холст
    if kind == "planks":
        assert js["joints"]["planks"] < js["joints"]["parquet"] / 3                    # доски длиннее паркета: стыков меньше
    if kind == "checker":
        assert [c for c, _ in js["checker"]] == ["#b9cde0", "#e8dec6", "#b9cde0"]      # голубой и бежевый вперемежку
        assert js["checker"][0][1] == pytest.approx(0.3 * 80 + 1)                      # квадрат размером с плитку


def test_floor_overlays_stipple_and_glow_within_window_and_restore_the_mode(js):
    dots = js["stipple"]
    assert dots["modes"] == ["destination-in", "source-over"] and dots["fills"] == 1 and dots["arcs"] >= 400     # крап одним путём, режим вернулся
    window = js["stipple_window"]
    assert window["r"] == pytest.approx(0.007 * 160) and 100 < window["arcs"] < 200   # окно 80 на 80 пикселей: около 12 на 12 точек, не вся сетка
    assert js["glow"] == {"modes": ["destination-over", "source-over"], "fill": "rgba(207,207,196,0.22)", "rect": [0, 0, 100, 50]}    # подсветка под рисунком


def test_floor_patch_paints_only_its_window_with_seams_of_the_whole_canvas(js):
    log = js["patch"]
    assert log[0] == "save" and log[1] == ["translate", 24, 0] and log[-1] == "restore"          # начало в углу окна, состояние вернулось
    tiles = [(fill, x, y) for _, fill, x, y in (e for e in log if e[0] == "rect")][1:3]          # первый прямоугольник - основа окна
    assert tiles == [("#e8dec6", 24, 0), ("#b9cde0", 48, 0)]      # плитки там же и того же цвета, что на всём холсте (24 пикселя - плитка 0.3 м при 80)
    assert len([e for e in log if e[0] == "rect"]) < 10                                          # окно 48 на 24, не весь холст


def test_linoleum_speckle_in_a_patch_is_the_speckle_of_the_whole_canvas(js):
    assert js["speckle"]["full"] > 50 and js["speckle"]["missing"] == 0      # крапинка привязана к миру, не к углу куска
