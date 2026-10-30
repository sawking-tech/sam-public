"""Выдавливание плана на странице радара (SAM-105): чистые модули plan_scene.js и camera_fit.js проверяются в node без браузера и THREE,
одним запуском node на файл. План выдуманный: стена с дверью, толстая несущая стена, диван."""
import json
import math

import pytest

from tests.fakes_presence import run_node_scenarios

LAYOUT = {
    "walls": [
        {"x1": 0, "z1": 0, "x2": 6, "z2": 0, "t": 0.2, "h": 2.5},      # 0: наружная
        {"x1": 3, "z1": 0, "x2": 3, "z2": 4, "t": 0.08, "h": 2.5},     # 1: тонкая внутренняя, в ней дверь
        {"x1": 0, "z1": 2, "x2": 3, "z2": 2, "t": 0.4, "h": 2.5},      # 2: несущая
    ],
    "outer_walls": [0],
    "objects": [
        {"id": "door", "title": "Дверная рама", "kind": "opening", "x": 2.6, "z": 0.95, "w": 0.8, "d": 0.1, "h": 2.0, "angle": 1.5708},
        {"id": "win", "title": "Окно, двойное", "kind": "opening", "x": 5.0, "z": -0.1, "w": 1.0, "d": 0.2, "h": 1.4, "angle": 0},
        {"id": "sofa", "title": "Диван", "kind": "furniture", "x": 3.5, "z": 2.0, "w": 2.0, "d": 0.9, "h": 0.87, "angle": 0},
        {"id": "lamp", "title": "Лампа", "kind": "furniture", "x": 4.0, "z": 3.0, "w": 0.2, "d": 0.2, "h": 0.4, "angle": 0},
        {"id": "chair", "title": "Стул вращающийся", "kind": "furniture", "x": 5.0, "z": 3.0, "w": 0.51, "d": 0.58, "h": 0.85, "angle": 0},
    ],
}
PLAN_W, PLAN_D = 11.9, 8.5                     # план для кадра сверху
FOV_DEG = 50

PRELUDE = f"""const layout = {json.dumps(LAYOUT)};
const pts = a => Array.from({{length: a.length / 3}}, (_, i) => [a[3*i], a[3*i+1], a[3*i+2]]);
const one = (title, size) => m.elementsFromLayout({{walls: [], objects: [{{id: 'o', title, kind: 'furniture', x: 0, z: 0, ...size, angle: 0}}]}})[0];
const furniturePts = e => m.extrudePoints(e, m.STYLES.furniture);
const surfaces = [{{ kind: 'furniture', shape: 'table', h: 0.75, rect: {{ cx: -0.41, cz: 0, w: 0.6, d: 1, a: 0 }} }},
                  {{ kind: 'furniture', shape: 'sofa', h: 0.87, rect: {{ cx: 0.63, cz: 0, w: 1, d: 1, a: 0 }} }}];   // стол: край в 0,11 м к западу от (0, 0); диван: край в 0,13 м к востоку
const fit = aspect => cam.fitDistance({{dir: [0.05, 1, 0], bbox: {{minX: 0, minZ: 0, maxX: {PLAN_W}, maxZ: {PLAN_D}}}, fovDeg: {FOV_DEG}, aspect}});
"""

SCENARIOS = {
    "styles": "m.elementsFromLayout(layout).map(e => [e.id, e.style])",
    "extruded": "Object.keys(m.extrudeAll(m.elementsFromLayout(layout)))",
    "solid_mark": "m.elementsFromLayout({...layout, objects: [...layout.objects, "
                  "{id: 'stove', title: 'Газовая плита', kind: 'furniture', x: 1, z: 1, w: 0.47, d: 0.51, h: 0.9, angle: 0, solid: true}, "
                  "{id: 'tiny', title: 'Коробка', kind: 'furniture', x: 3, z: 1, w: 0.47, d: 0.51, h: 0.9, angle: 0}]})"
                  ".filter(e => ['stove', 'tiny'].includes(e.id)).map(e => [e.id, e.style])",
    "wall_points": "pts(m.extrudeAll(m.elementsFromLayout(layout)).wall)",
    "thickness": "{ const p = m.extrudeAll(m.elementsFromLayout(layout)); "
                 "return {thick: [...new Set(pts(p.bearing).map(q => +q[2].toFixed(3)))].length, thin: [...new Set(pts(p.wall).map(q => +q[0].toFixed(3)))].length}; }",
    "normals": "{ const n = {}; const p = m.extrudeAll(m.elementsFromLayout(layout), m.STYLES, n); const q = m.extrudeAll(m.elementsFromLayout(layout)); "
               "return {pts: p.outer.length / 3, nrm: n.outer.length / 2, unit: Array.from({length: n.outer.length / 2}, (_, i) => Math.hypot(n.outer[2*i], n.outer[2*i+1])), same: p.outer.length === q.outer.length}; }",
    "cap": "{ const els = m.elementsFromLayout(layout); const top = id => { const e = els.find(x => x.id === id); "
           "return m.extrudePoints(e, m.STYLES[e.style]).filter(q => Math.abs(q[1] - e.h) < 1e-9 && q[0] > e.rect.cx - e.rect.w / 4 && q[0] < e.rect.cx + e.rect.w / 4).length; }; "
           "return {sofa: top('sofa'), wall: top('wall-1')}; }",
    "sofa": "{ const e = m.elementsFromLayout(layout).find(x => x.id === 'sofa'); const p = furniturePts(e); "
            "const hi = p.filter(q => q[1] > 0.8 * e.h), lo = p.filter(q => q[1] <= 0.5 * e.h + 1e-6); "
            "return {shape: e.shape, hiZ: Math.max(...hi.map(q => q[2])) - Math.min(...hi.map(q => q[2])), loZ: Math.max(...lo.map(q => q[2])) - Math.min(...lo.map(q => q[2]))}; }",
    "wallunit": "{ const e = one('Шкаф-стенка', {w: 2.4, d: 0.4, h: 2.1}); const p = furniturePts(e); "
                "const inSec = k => p.filter(q => q[0] > 0.8 * k + 0.05 && q[0] < 0.8 * (k + 1) - 0.05).length; return [e.shape, inSec(0), inSec(1), inSec(2)]; }",
    "tub": "{ const e = one('Ванна', {w: 0.7, d: 1.5, h: 0.55}); "
           "return [e.shape, furniturePts(e).filter(q => Math.abs(q[0] - 0.35) < 0.1 && Math.abs(q[2] - 0.75) < 0.2 && q[1] > 0.2 && q[1] < 0.5).length]; }",
    "sofabook": "{ const edge = e => furniturePts(e).filter(q => q[1] > 0.5 * e.h && q[1] < 0.75 * e.h && q[0] < 0.1 && q[2] > 0.5).length; "
                "const size = {w: 2.0, d: 0.9, h: 0.87}; return [one('Диван-книжка', size).shape, edge(one('Диван-книжка', size)), edge(one('Диван', size))]; }",
    "table": "{ const p = furniturePts(one('Стол', {w: 1.2, d: 0.6, h: 0.78})); "
             "return {floorMid: p.filter(q => q[1] < 0.1 && q[0] > 0.3 && q[0] < 0.9).length, top: p.filter(q => q[1] > 0.7).length}; }",
    "unknown": "{ const e = one('Холодильник', {w: 0.6, d: 0.6, h: 1.8}); return [e.shape ?? null, furniturePts(e).some(q => q[1] < 0.1 && q[0] < 0.05 && q[2] < 0.05)]; }",
    "support": "{ const els = m.elementsFromLayout({walls: [], objects: ["
               "{id: 'b', title: 'Кровать', kind: 'furniture', x: 0, z: 0, w: 2, d: 1.6, h: 0.9, angle: 0},"
               "{id: 's', title: 'Диван', kind: 'furniture', x: 4, z: 0, w: 2, d: 0.9, h: 0.87, angle: 0},"
               "{id: 'c', title: 'Стул', kind: 'furniture', x: 8, z: 0, w: 0.5, d: 0.5, h: 0.85, angle: 0}]}); "
               "return [m.surfaceAt(els, 1, 0.8), m.surfaceAt(els, 5, 0.4), m.surfaceAt(els, 8.2, 0.2), m.surfaceAt(els, 3, 3)]; }",
    "support_pose": "{ const els = m.elementsFromLayout({walls: [], objects: ["
                    "{id: 't', title: 'Унитаз', kind: 'furniture', x: 0, z: 0, w: 0.4, d: 0.65, h: 0.75, angle: 0},"
                    "{id: 'v', title: 'Ванна', kind: 'furniture', x: 2, z: 0, w: 0.7, d: 1.5, h: 0.55, angle: 0},"
                    "{id: 's', title: 'Раковина', kind: 'furniture', x: 4, z: 0, w: 0.6, d: 0.5, h: 0.85, angle: 0}]}); "
                    "return [m.supportPose(els, 0.2, 0.3), m.supportPose(els, 2.3, 0.7), m.supportPose(els, 4.3, 0.2), m.supportPose(els, 9, 9), m.surfaceAt(els, 2.3, 0.7)]; }",
    "swivel": "{ const els = m.elementsFromLayout({walls: [], objects: ["
              "{id: 'c', title: 'Стул вращающийся', kind: 'furniture', x: 0, z: 0, w: 0.5, d: 0.5, h: 0.85, angle: 0},"
              "{id: 'd1', title: 'Дверная рама', kind: 'opening', x: 3.6, z: -0.05, w: 0.8, d: 0.1, h: 2, angle: 0},"
              "{id: 'd2', title: 'Входная дверь', kind: 'opening', x: 0, z: 8, w: 0.9, d: 0.2, h: 2, angle: 0},"
              "{id: 's', title: 'Диван', kind: 'furniture', x: 5, z: 5, w: 2, d: 0.9, h: 0.87, angle: 0}]}); "
              "return [m.swivelFace(els, 0.25, 0.25), m.swivelFace(els, 6, 5.4), m.swivelFace(els, 9, 9)]; }",
    "device_surface": "{ const els = [{kind: 'furniture', shape: 'table', h: 0.78, rect: {cx: 1, cz: 1, w: 1.2, d: 0.6, a: 0}}, {kind: 'furniture', shape: 'bed', h: 0.9, rect: {cx: 3, cz: 1, w: 1.6, d: 2, a: 0}},"
                      " {kind: 'seat', h: 0.5, rect: {cx: 5, cz: 1, w: 0.5, d: 0.5, a: 0}}, {kind: 'furniture', h: 1.8, rect: {cx: 7, cz: 1, w: 0.7, d: 0.7, a: 0}},"
                      " {kind: 'furniture', h: 1.5, rect: {cx: 13, cz: 1, w: 0.5, d: 0.5, a: 0}}, {kind: 'furniture', shape: 'tub', h: 0.55, rect: {cx: 11, cz: 1, w: 1.5, d: 0.7, a: 0}}]; "
                      "return [[1, 1], [3, 1], [5, 1], [7, 1], [9, 9], [11, 1], [13, 1]].map(([x, z]) => m.deviceSurfaceAt(els, x, z)); }",
    "shelves": "{ const els = [{kind: 'furniture', shelves: 5, h: 2.5, rect: {cx: 1, cz: 1, w: 0.4, d: 0.6, a: 0}}]; return [0.2, 0.6, 1.2, 1.9, 9].map(y => m.deviceSurfaceAt(els, 1, 1, y)); }",
    "nearest": "m.nearestSurface(surfaces, 0, 0, 0.9, 0.3)",
    "on_sofa": "m.nearestSurface(surfaces, 0.7, 0, 0.9, 0.3)",
    "nothing_near": "m.nearestSurface(surfaces, 5, 5, 0.9, 0.3) ?? null",
    "sticky_keep": "m.stickySurface(surfaces, 0.01, 0, 0.9, 0.3, { x: 0.15, z: 0 })",
    "sticky_move": "m.stickySurface(surfaces, -0.3, 0, 0.9, 0.3, { x: 0.15, z: 0 })",
    "router_height": "{ const ys = pts(m.extrudeAll(m.elementsFromLayout({...layout, router: {x: 5, z: 3.5, h: 1.6}})).router).map(q => q[1]); return [Math.min(...ys), Math.max(...ys)]; }",
    "router_flat": "{ const r = m.routerRect({walls: [{x1: 7.34, z1: 3.19, x2: 7.34, z2: 5.8, t: 0.08, h: 2.5}], objects: [], router: {x: 6.6, z: 4.0, h: 2.4}}); return [r.cx, r.cz, r.w, r.d, r.a]; }",
    "router_inner": "{ const r = m.routerRect({walls: [{x1: 5.96, z1: 3.3, x2: 5.96, z2: 8.4, t: 0.1, h: 2.5}, {x1: 7.34, z1: 3.19, x2: 7.34, z2: 5.8, t: 0.08, h: 2.5}], outer_walls: [0], objects: [], router: {x: 6.6, z: 4.0, h: 2.4}}); return [r.cx, r.cz]; }",
    "router_far": "{ const r = m.routerRect({walls: [{x1: 0, z1: 0, x2: 1, z2: 0, t: 0.2, h: 2.5}], objects: [], router: {x: 9, z: 9, h: 2.4}}); return [r.cx, r.cz, r.a]; }",
    "floor_mask": "{ const on = m.makeOnFloor({cell: 0.5, x0: 0, z0: 0, w: 4, h: 4, runs: [[0, 0, 2], [1, 0, 4]]}, [{x: 0, z: 0, w: 9, d: 9}]); "
                  "return [on(0.2, 0.2), on(1.9, 0.2), on(0.2, 0.7), on(1.9, 0.7), on(-1, 0)]; }",
    "top_view": "[fit(1.78), fit(1.0), fit(0.6)]",
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("plan_scene.js", SCENARIOS, extra={"cam": "camera_fit.js"}, prelude=PRELUDE)


def test_plan_elements_get_a_style_and_only_solid_ones_extrude(js):
    """Стиль по виду элемента и списку наружных стен; мелкий предмет с solid рисуется, без него - prop и не выдавливается."""
    assert dict(js["styles"]) == {"wall-0": "outer", "wall-1": "wall", "wall-2": "bearing", "door": "door", "win": "window", "sofa": "furniture", "lamp": "prop", "chair": "seat"}
    assert sorted(js["extruded"]) == ["bearing", "furniture", "outer", "wall"]
    assert dict(js["solid_mark"]) == {"stove": "furniture", "tiny": "prop"}


def test_walls_extrude_with_door_cuts_thickness_and_normals(js):
    """Дверь вырезает стену ниже своей высоты и оставляет перемычку; толстая стена показывает толщину, тонкая - один лист; у точек единичная нормаль."""
    in_door = lambda x, z: abs(x - 3.0) < 0.1 and abs(z - 1.0) < 0.35          # центр двери (3,0; 1,0), ширина 0,8 вдоль z
    assert not any(in_door(x, z) and y < 2.0 for x, y, z in js["wall_points"])
    assert any(in_door(x, z) and y > 2.0 for x, y, z in js["wall_points"])
    assert js["thickness"]["thick"] > 2 and js["thickness"]["thin"] == 1
    normals = js["normals"]
    assert normals["pts"] == normals["nrm"] > 0 and normals["same"] and all(abs(u - 1.0) < 1e-6 for u in normals["unit"])


@pytest.mark.parametrize("case", ["cap", "sofa", "wallunit", "tub", "sofabook", "table", "unknown"])
def test_furniture_shape_follows_its_title(js, case):
    got = js[case]
    if case == "cap":                                                             # у мебели крышка, у стены только кромка листа
        assert got["sofa"] > 0 and 0 < got["wall"] < got["sofa"] * 4
    elif case == "sofa":                                                          # сиденье, спинка, подлокотники, а не блок
        assert got["shape"] == "sofa" and got["hiZ"] <= 0.25 * 0.9 + 1e-6 and got["loZ"] > 0.8
    elif case == "wallunit":                                                      # три секции, книги в той, что у стола
        assert got[0] == "wallunit" and got[3] > 1.5 * got[1] and got[1] == got[2]
    elif case == "tub":                                                           # полая, с бортом
        assert got == ["tub", 0]
    elif case == "sofabook":                                                      # у дивана-книжки нет подлокотников
        assert got[0] == "sofabook" and got[1] == 0 and got[2] > 0
    elif case == "table":                                                         # на ножках, со столешницей
        assert got["floorMid"] == 0 and got["top"] > 20
    else:                                                                         # неизвестное - целый блок
        assert got == [None, True]


def test_person_support_height_and_pose_under_a_point(js):
    """Опора под точкой - сиденье дивана, матрас или пол; поза от опоры (унитаз - сидеть, ванна - лежать); на стуле разворот к ближней двери, на диване нет."""
    assert js["support"] == pytest.approx([0.54, 0.435, 0.425, 0], abs=1e-6)
    assert js["support_pose"][:4] == ["sit", "lie", None, None] and abs(js["support_pose"][4] - 0.165) < 1e-6
    chair, sofa, nowhere = js["swivel"]
    assert abs(chair) < 0.1 and sofa is None and nowhere is None                  # ближайшая дверь на востоке


def test_device_rests_on_the_nearest_surface_and_sticks_to_it(js):
    """Устройство на столе, кровати, полке не выше точки, не на стуле и высоком шкафу; ближайшая поверхность в радиусе; прилипание до явного выигрыша."""
    assert js["device_surface"] == [0.78, 0.54, 0, 0, 0, 0.55, 0]
    assert js["shelves"] == [0, 0.5, 1.0, 2.0, 2.5]                               # полки 0,5 ... 2,5; ниже самой низкой - нет поверхности
    nearest = js["nearest"]
    assert nearest["top"] == pytest.approx(0.75) and nearest["x"] < 0 and nearest["dist"] == pytest.approx(0.12)     # стол на кольце 0,12, диван только на 0,14
    assert js["on_sofa"]["dist"] == 0 and js["on_sofa"]["top"] == pytest.approx(0.435) and js["nothing_near"] is None
    assert js["sticky_keep"]["x"] == pytest.approx(0.15) and js["sticky_keep"]["top"] == pytest.approx(0.435)        # стол ближе на 0,02 м - меньше порога
    assert js["sticky_move"]["top"] == pytest.approx(0.75)                       # точка ушла к столу: прилипание отпускает


def test_router_hangs_flat_on_the_nearest_inner_wall(js):
    """На своей высоте, тонкой стороной к стене, внутренняя стена важнее близкой наружной, вдали от стен - точка плана."""
    low, high = js["router_height"]
    assert 1.45 < low < 1.55 and 1.65 < high < 1.75
    cx, cz, w, d, a = js["router_flat"]
    assert abs(cx - (7.34 - 0.04 - 0.06)) < 1e-6 and cz == 4.0 and d < w and abs(a - math.pi / 2) < 1e-5
    assert abs(js["router_inner"][0] - (7.34 - 0.04 - 0.06)) < 1e-6 and js["router_inner"][1] == 4.0
    assert js["router_far"] == [9, 9, 0]


def test_floor_mask_follows_the_server_runs(js):
    assert js["floor_mask"] == [True, False, True, True, False]


def test_top_view_fits_the_whole_plan_in_frame(js):
    """До аспекта около 1 кадр ограничивает вертикаль экрана (длинная ось плана), у узкого окна - ширина; полвысоты кадра покрывает длинную ось."""
    wide, square, tall = js["top_view"]
    assert wide == square < tall
    assert wide * math.tan(math.radians(FOV_DEG / 2)) >= PLAN_W / 2
