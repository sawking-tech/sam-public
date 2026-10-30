"""Силуэты людей точками для призраков радара (SAM-105): рост, плечи, грудь у женской фигуры, позы. Все сценарии figure.js одним запуском node."""
import pytest

from tests.fakes_presence import run_node_scenarios

LIMIT = 250
STAND_VARIANTS = ["cool", "down", "crossed", "hips", "behind", "phone"]
SLOTS = [(g, "stand", v) for g in ("male", "female") for v in STAND_VARIANTS] + \
        [(g, "sit", "side") for g in ("male", "female")] + \
        [(g, "lie", v) for g in ("male", "female") for v in ("side", "back", "hug", "hugR")]
PRELUDE = """
const f = m;
const ext = (p, i) => [Math.min(...p.map(q => q[i])), Math.max(...p.map(q => q[i]))];
const box = (g, pose, n, v = 'side', o = {}) => { const p = f.figurePoints(g, pose, n, v, o); return {n: p.length, fwd: ext(p, 0), up: ext(p, 1), side: ext(p, 2)}; };
const span = (p) => { const e = ext(p, 2); return e[1] - e[0]; };
const slot = (g, pose, v) => { const p = f.figurePoints(g, pose, %d, v); const top = Math.max(...p.map(q => q[1])); const head = p.filter(q => q[3] === 0);
  return {n: p.length, tagged: p.every(q => q.length === 4 && q[3] >= 0), parts: f.BODY_GROUPS.map((_, k) => p.filter(q => q[3] === k).length),
          headTop: Math.max(...head.map(q => q[1])) - top}; };
""" % LIMIT
SCENARIOS = {
    "variants": "f.STAND_VARIANTS",
    **{f"slot:{g}-{p}-{v}": f"slot('{g}', '{p}', '{v}')" for g, p, v in SLOTS},
    "man_stand": "box('male', 'stand', 250)",
    "woman_stand": "box('female', 'stand', 250)",
    "bust": "['male', 'female'].map(g => Math.max(...f.figurePoints(g, 'stand', 250).filter(q => q[1] > 1.15 && q[1] < 1.35).map(q => q[0])))",
    # полоса плеч от роста: рост и ширина плеч опорного, лёгкого и невысокого и человека без размеров
    "scaled": ("{ const s = o => { const p = f.figurePoints('male', 'stand', 250, 'side', o), top = Math.max(...p.map(q => q[1])); "
               "return [top, span(p.filter(q => q[1] > 0.7 * top && q[1] < 0.76 * top))]; }; return [s({height: 182, weight: 90}), s({height: 170, weight: 70}), s({})]; }"),
    "seat_low": "[{}, {height: 170, weight: 70}].map(o => Math.min(...f.figurePoints('male', 'sit', 250, 'side', o).filter(q => q[1] > 0.3).map(q => q[1])))",
    "knees": "['male', 'female'].map(g => span(f.figurePoints(g, 'sit', 4000).filter(q => q[0] > 0.3 && q[1] >= 0.42 && q[1] < 0.56)))",
    "feet": "['male', 'female'].map(g => span(f.figurePoints(g, 'sit', 4000).filter(q => q[0] > 0.3 && q[1] >= 0.0 && q[1] < 0.12)))",
    "arms": ("{ const out = {}; for (const v of f.STAND_VARIANTS) { const p = f.figurePoints('female', 'stand', 250, v); const at = (lo, hi) => p.filter(q => q[1] > lo && q[1] < hi); "
             "out[v] = {width: Math.max(0, ...at(1.0, 1.3).map(q => Math.abs(q[2]))), front: Math.max(0, ...at(1.1, 1.3).map(q => q[0]))}; } return out; }"),
    "sit": "box('male', 'sit', 250)",
    "lie_side": "box('female', 'lie', 500, 'side')",
    "lie_back": "box('female', 'lie', 500, 'back')",
    "lie_head": "f.figurePoints('female', 'lie', 250).reduce((a, q) => q[0] > a[0] ? q : a)",
    "lie_centres": "['side', 'back'].map(v => { const p = f.figurePoints('female', 'lie', 500, v); return [0, 2].map(i => (ext(p, i)[0] + ext(p, i)[1]) / 2); })",
    "hug": "['side', 'hug', 'hugR'].map(v => ext(f.figurePoints('male', 'lie', 4000, v), 2))",
    # фаллос: окно перед бёдрами по центру стоя и сидя, головка относительно корня, лёжа - число точек и край по оси «вбок»
    "phallus_windows": ("{ const win = (g, pose, o) => f.figurePoints(g, pose, 900, 'side', o).filter(q => Math.abs(q[2]) < 0.04 && q[0] > 0.1 && q[0] < 0.22 && q[1] > 0.5 && q[1] < 0.8).length; "
                        "const sit = o => f.figurePoints('male', 'sit', 900, 'side', o).filter(q => Math.abs(q[2]) < 0.04 && q[0] > 0.12 && q[0] < 0.25 && q[1] > 0.3 && q[1] < 0.56).length; "
                        "return {stand: win('male', 'stand', {}), stand_with: win('male', 'stand', {phallus: true}), woman: win('female', 'stand', {phallus: true}), sit: sit({}), sit_with: sit({phallus: true})}; }"),
    "phallus_tips": ("{ const tip = (pose, st) => { const p = f.phallusParts(pose, st); return [p[0].a[1], p.filter(q => q.c)[0].c]; }; "
                     "return {hang: tip('stand', 'hang'), erect: tip('stand', 'erect'), sit: tip('sit', 'hang')}; }"),
    "phallus_lie": ("{ const s = (g, v, st) => { const p = f.figurePoints(g, 'lie', 4000, v, {phallus: st}); return [p.length, Math.max(...p.map(q => q[2]))]; }; "
                    "return {plain: s('male', 'side', false), hang: s('male', 'side', 'hang'), erect: s('male', 'side', 'erect'), hug: s('male', 'hug', 'erect'), "
                    "hug_plain: s('male', 'hug', false), hug_hang: s('male', 'hug', 'hang'), hugR: s('male', 'hugR', 'erect'), "
                    "woman: s('female', 'side', 'erect'), woman_plain: s('female', 'side', false)}; }"),
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("figure.js", SCENARIOS, prelude=PRELUDE)


@pytest.mark.parametrize("gender, pose, variant", SLOTS, ids=[f"{g}-{p}-{v}" for g, p, v in SLOTS])
def test_figure_fits_the_slot_and_tags_every_point_with_a_body_part(js, gender, pose, variant):
    got = js[f"slot:{gender}-{pose}-{variant}"]
    assert 80 < got["n"] <= LIMIT and got["tagged"] and all(n > 0 for n in got["parts"])     # голова, туловище, руки и ноги в каждой позе
    if pose == "stand":
        assert got["headTop"] == pytest.approx(0.0, abs=1e-9)                                # стоя самая верхняя точка - голова


def _man_taller_and_broader(js):
    man, woman = js["man_stand"], js["woman_stand"]
    assert 1.7 < man["up"][1] < 1.85 and 1.55 < woman["up"][1] < 1.72
    assert man["side"][1] - man["side"][0] > woman["side"][1] - woman["side"][0]


def _bust_only_for_a_woman(js):
    man, woman = js["bust"]
    assert woman > man + 0.03


def _scaled_by_height_and_weight(js):
    reference, smaller, plain = js["scaled"]
    assert abs(smaller[0] / reference[0] - 170 / 182) < 0.01                  # рост по отношению к опорному
    assert smaller[1] < reference[1] * 0.95                                    # легче и ниже: уже в плечах
    assert reference[0] == pytest.approx(plain[0])                             # опорный человек не меняется


def _sitter_stays_on_the_seat_when_scaled(js):
    plain, scaled = js["seat_low"]
    assert abs(plain - scaled) < 0.05


def _knees_and_feet_when_sitting(js):
    (man_knees, woman_knees), (man_feet, woman_feet) = js["knees"], js["feet"]
    assert woman_knees < 0.32 and man_knees > 0.5 and man_knees > woman_knees + 0.2       # колени мужчины в стороны, у женщины вместе
    assert woman_feet > woman_knees + 0.15 and man_feet < man_knees + 0.1                 # у женщины стопы шире колен, у мужчины голени вниз


def _stand_arm_variants_differ(js):
    arms = js["arms"]
    assert js["variants"] == STAND_VARIANTS
    assert arms["down"]["width"] < arms["hips"]["width"]                        # руки опущены уже, чем на поясе
    assert arms["crossed"]["front"] > arms["down"]["front"]                     # скрещены на груди выступают вперёд
    assert len({round(a["width"], 3) + round(a["front"], 3) for a in arms.values()}) >= 4


@pytest.mark.parametrize("check", [_man_taller_and_broader, _bust_only_for_a_woman, _scaled_by_height_and_weight, _sitter_stays_on_the_seat_when_scaled,
                                   _knees_and_feet_when_sitting, _stand_arm_variants_differ],
                         ids=["height_shoulders", "bust", "scale", "seat", "knees_feet", "arm_variants"])
def test_figure_proportions_follow_gender_height_and_weight(js, check):
    check(js)


def _sit(js):
    s = js["sit"]
    assert s["fwd"][1] > 0.4 and s["up"][1] < 1.45                               # бёдра вперёд, ниже стоящего


def _lie_side(js):
    s = js["lie_side"]
    assert s["up"][1] < 0.5 and 1.2 < s["fwd"][1] - s["fwd"][0] < 1.8            # на боку низко, вытянут почти на рост
    assert js["lie_head"][0] > 0.4                                                # голова у +вперёд
    for centre in js["lie_centres"]:                                              # по центру опоры: ноги не свисают
        assert centre == pytest.approx([0.0, 0.0], abs=1e-6)


def _lie_back(js):
    side, back = js["lie_side"], js["lie_back"]
    assert back["up"][1] < 0.36 and back["fwd"][1] - back["fwd"][0] > 1.3         # плашмя и вытянут
    assert back["side"][1] - back["side"][0] > side["side"][1] - side["side"][0] and back["up"][1] < side["up"][1]


def _hug(js):
    plain, hug, mirrored = js["hug"]
    assert 0.41 <= hug[1] <= 0.5 and abs(hug[0] - plain[0]) < 0.05                # рука тянется к партнёру, торс на месте
    assert abs(mirrored[0] + hug[1]) < 0.05 and abs(mirrored[1] + hug[0]) < 0.05   # зеркало по оси «вбок»


@pytest.mark.parametrize("check", [_sit, _lie_side, _lie_back, _hug], ids=["sit", "lie_side", "lie_back", "hug"])
def test_pose_shapes_sit_lie_side_back_and_hug(js, check):
    check(js)


def _standing(js):
    w = js["phallus_windows"]
    assert w["stand"] == 0 and w["stand_with"] > 5 and w["woman"] == 0            # висит между ног, только у мужской фигуры
    hang, erect = js["phallus_tips"]["hang"], js["phallus_tips"]["erect"]
    assert hang[1][1] < hang[0] - 0.9 * 0.20                                       # висит: головка почти на длину ниже корня
    assert erect[1][1] - erect[0] > 0.10 and 0.12 < erect[1][2] - 0.095 < 0.2      # поднятый: выше корня и вперёд


def _sitting(js):
    w = js["phallus_windows"]
    assert w["sit"] == 0 and w["sit_with"] > 5
    tip = js["phallus_tips"]["sit"][1]
    assert tip[1] < 0.42 and tip[2] > 0.15                                         # свисает через край сиденья


def _lying(js):
    lie = js["phallus_lie"]
    assert lie["hang"] == lie["plain"] and lie["erect"][0] > lie["plain"][0] and lie["erect"][1] > lie["plain"][1]   # лёжа только поднятый
    assert lie["hug"][0] > lie["hug_plain"][0] and lie["hug_hang"] == lie["hug_plain"] and lie["hugR"][0] == lie["hug"][0]
    assert lie["woman"] == lie["woman_plain"]


@pytest.mark.parametrize("check", [_standing, _sitting, _lying], ids=["stand", "sit", "lie"])
def test_phallus_option_by_pose_and_state(js, check):
    check(js)
