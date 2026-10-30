"""Перелёт облака точек (SAM-105): независимые пролёты точек, центр срывается раньше края, дуги, посадка точно на цель.
Случайность flight.js передаётся сидированным генератором, все сценарии - один запуск node."""
import pytest

from tests.fakes_presence import run_node_scenarios

PRELUDE = r"""
const seeded = seed => () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
  t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };       // mulberry32: повторяемый ряд по сиду
const simulate = seed => {
  const rnd = seeded(seed), N = 300, STEP = 1.5, DT = 1000 / 60;
  const body = Array.from({length: N}, (_, i) => [((i * 37) % 11 - 5) * 0.03, 0.2 + (i % 100) * 0.016, ((i * 29) % 7 - 3) * 0.03]);
  const mid = [0, 1, 2].map(a => body.reduce((t, q) => t + q[a], 0) / N);
  const rad = Math.max(...body.map(q => Math.hypot(q[0] - mid[0], q[1] - mid[1], q[2] - mid[2])));
  const edge = Float32Array.from(body, q => Math.min(1, Math.hypot(q[0] - mid[0], q[1] - mid[1], q[2] - mid[2]) / rad));
  const cur = new Float32Array(N * 3); body.forEach((q, i) => cur.set(q, i * 3));
  const perm = m.shuffled(N, rnd), plan = m.planFlight(N, edge, 0, cur, rnd);
  const goal = i => { const q = body[perm[i]]; return [q[0] + STEP, q[1], q[2]]; };
  const arrive = new Array(N).fill(null), leave = new Array(N).fill(null);
  const frames = Math.ceil((m.burstSeconds() * 1000 + 100) / DT);
  let maxArc = 0, notStraight = 0, last = [];
  for (let k = 0; k <= frames; k++) {                                     // время по номеру кадра, не по накопленной сумме шагов
    const t = k * DT, work = Float32Array.from(cur);
    for (let i = 0; i < N; i++) {
      const g = goal(i), s = body[i], done = m.evalFlight(plan, i, work, i * 3, g[0], g[1], g[2], t);
      if (leave[i] === null && Math.hypot(work[i * 3] - s[0], work[i * 3 + 1] - s[1], work[i * 3 + 2] - s[2]) > 1e-4) leave[i] = t / 1000;
      if (arrive[i] === null && done) arrive[i] = t / 1000;
      const ax = g[0] - s[0], ay = g[1] - s[1], az = g[2] - s[2], al = Math.hypot(ax, ay, az) || 1;     // отклонение от прямой старт-цель
      const px = work[i * 3] - s[0], py = work[i * 3 + 1] - s[1], pz = work[i * 3 + 2] - s[2], along = (px * ax + py * ay + pz * az) / al;
      const dev = Math.hypot(px - ax / al * along, py - ay / al * along, pz - az / al * along);
      maxArc = Math.max(maxArc, dev); if (dev > 0.05) notStraight += 1;
    }
    last = Array.from(work);
  }
  const avg = a => a.reduce((x, y) => x + y, 0) / a.length, std = a => { const m0 = avg(a); return Math.sqrt(avg(a.map(v => (v - m0) ** 2))); };
  const idx = f => Array.from({length: N}, (_, i) => i).filter(f);
  let worst = 0; for (let i = 0; i < N; i++) { const g = goal(i); worst = Math.max(worst, ...[0, 1, 2].map(a => Math.abs(last[i * 3 + a] - g[a]))); }
  return { burst: m.burstSeconds(), frame: DT / 1000, done: arrive.every(v => v !== null), worst, maxArc, notStraight,
    leaveCore: avg(idx(i => edge[i] < 0.3).map(i => leave[i] ?? 9)), leaveRim: avg(idx(i => edge[i] > 0.7).map(i => leave[i] ?? 9)),
    arriveStd: std(arrive.map(v => v ?? 9)), leaveStd: std(leave.map(v => v ?? 9)), lastArrive: Math.max(...arrive.map(v => v ?? 9)) };
};
"""
OLD_PARTS = [0, 0, 0, 1, 1, 1, 1, 2, 2, 3, 3, 3]          # головы 3 -> 4, туловища 4 -> 3, руки 2 -> 3, ноги 3 -> 2: число мест в частях разное
NEW_PARTS = [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3]
SCENARIOS = {
    "sim": "simulate(7)",
    "shuffled": "[1, 2].map(s => Array.from(m.shuffled(200, seeded(s))))",
    "parts": f"{{ const old = {OLD_PARTS}, now = {NEW_PARTS}; const p = Array.from(m.assignTargets(old, now, seeded(3))); return {{p, same: p.filter((j, i) => now[j] === old[i]).length}}; }}",
    "same_pose": "{ const g = Array.from({length: 120}, (_, i) => i % 4); const p = Array.from(m.assignTargets(g, g, seeded(5))); return {ok: p.every((j, i) => g[j] === g[i]), moved: p.filter((j, i) => j !== i).length}; }",
    # последним: сценарий меняет общие ползунки FLIGHT, сценарии выполняются по порядку
    "slider": ("{ const N = 50, cur = new Float32Array(N * 3), edge = new Float32Array(N).fill(0.5); "
               "const plan = m.planFlight(N, edge, 0, cur, () => 0.5), pos = t => { const w = Float32Array.from(cur); m.evalFlight(plan, 0, w, 0, 1, 0, 0, t); return [w[0], plan.endMs]; }; "
               "const before = pos(200); m.FLIGHT.durMin = 1.0; m.FLIGHT.durMax = 1.0; return {before, after: pos(200), burst: m.burstSeconds()}; }"),
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("flight.js", SCENARIOS, prelude=PRELUDE)


def test_flight_points_leave_core_first_fly_on_arcs_and_land_on_goal(js):
    got = js["sim"]
    assert got["done"] and got["worst"] < 1e-5                                  # все сели точно, допуск на шум float32
    assert got["lastArrive"] <= got["burst"] + got["frame"]                     # последняя посадка в пределах рывка с точностью до кадра
    assert got["leaveStd"] > 0.02 and got["arriveStd"] > 0.03                   # времена вылета и посадки разбросаны: не единым телом
    assert got["leaveCore"] + 0.02 < got["leaveRim"]                            # центр срывается раньше края
    assert got["notStraight"] > 200 and 0.03 < got["maxArc"] < 0.15             # дуги, но не шире тела


@pytest.mark.parametrize("case", ["shuffled", "parts", "same_pose"])
def test_targets_are_a_permutation_that_keeps_body_parts(js, case):
    if case == "shuffled":
        a, b = js["shuffled"]
        assert sorted(a) == list(range(200)) and a != list(range(200)) and a != b      # каждое место занято одной точкой, разные сиды - разный выбор
        assert sum(1 for i, v in enumerate(a) if v == i) < 20                          # почти никогда не «в своё» место
    elif case == "parts":
        got = js["parts"]
        assert sorted(got["p"]) == list(range(12)) and got["same"] >= 9                 # почти все в свою часть тела, остальным пары не хватило
    else:
        got = js["same_pose"]
        assert got["ok"] and got["moved"] > 60                                          # голова остаётся головой, выбор при этом случайный


def test_slider_change_mid_flight_applies_on_the_next_frame(js):
    got = js["slider"]
    assert got["after"][0] < got["before"][0]                                   # пролёт стал долгим: точка ушла от старта меньше
    assert got["after"][1] == pytest.approx(got["burst"] * 1000) and got["after"][1] > got["before"][1]     # конец рывка по текущим ползункам
