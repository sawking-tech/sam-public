// Дорожка следа на странице радара (SAM-105): маршрут идёт по натяжке между видимыми точками, углы острые.
// Углы скругляются дугой не больше TRAIL_CORNER_M: длинные отрезки остаются прямыми и не уходят в стены и мебель, как уходили при срезании по Чайкину.
// Чистый модуль без THREE и DOM: проверяется в node.
export const TRAIL_CORNER_M = 0.35;        // радиус скругления угла: человек не режет углы на метры, а огибает их
export const TRAIL_ARC_STEPS = 6;          // точек на дугу

// points: [{x, z}] -> [{x, z}] с теми же первой и последней точками; на каждом внутреннем углу квадратичная дуга от точки на отрезке до точки на отрезке.
export function smoothTrail(points, radius = TRAIL_CORNER_M, steps = TRAIL_ARC_STEPS) {
  if (points.length < 3) return points.map(p => ({ x: p.x, z: p.z }));
  const out = [{ x: points[0].x, z: points[0].z }];
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1], b = points[i], c = points[i + 1], la = Math.hypot(a.x - b.x, a.z - b.z), lc = Math.hypot(c.x - b.x, c.z - b.z);
    if (la < 1e-6 || lc < 1e-6) { out.push({ x: b.x, z: b.z }); continue; }
    const d = Math.min(radius, la * 0.45, lc * 0.45);                              // не дальше середины коротких отрезков: дуги соседних углов не наезжают
    const p = { x: b.x + (a.x - b.x) / la * d, z: b.z + (a.z - b.z) / la * d }, q = { x: b.x + (c.x - b.x) / lc * d, z: b.z + (c.z - b.z) / lc * d };
    for (let k = 0; k <= steps; k++) {
      const t = k / steps, u = 1 - t;
      out.push({ x: u * u * p.x + 2 * u * t * b.x + t * t * q.x, z: u * u * p.z + 2 * u * t * b.z + t * t * q.z });
    }
  }
  out.push({ x: points[points.length - 1].x, z: points[points.length - 1].z });
  return out;
}

// Развод проходов: путь туда и обратно по одному коридору - линия поверх линии. Каждое следующее прохождение того же места сдвигается вбок на LANE_M по очереди
// влево и вправо; соседние шаги одного прохода (ближе PASS_GAP шагов друг к другу) считаются одним проходом и не сдвигаются.
export const TRAIL_CELL_M = 0.12;          // клетка, по которой ищутся совпадения проходов
export const TRAIL_LANE_M = 0.07;          // шаг сдвига вбок для каждого нового прохода
export const TRAIL_PASS_GAP = 15;          // шагов подряд - один проход; дальше по порядку - уже другой

// steps: [{x, z, ux, uz}] по порядку пути -> те же шаги, сдвинутые поперёк направления (-uz, ux).
export function spreadPasses(steps, cell = TRAIL_CELL_M, lane = TRAIL_LANE_M, gap = TRAIL_PASS_GAP) {
  const seen = new Map();                                                          // клетка -> номера шагов, которые её уже занимали
  return steps.map((st, i) => {
    const key = Math.round(st.x / cell) + ',' + Math.round(st.z / cell), before = (seen.get(key) ?? []).filter(j => i - j > gap), passes = new Set(before.map(j => Math.floor(j / gap)));
    (seen.get(key) ?? seen.set(key, []).get(key)).push(i);
    const n = passes.size, shift = n === 0 ? 0 : (n % 2 ? 1 : -1) * Math.ceil(n / 2) * lane;
    return { ...st, x: st.x - st.uz * shift, z: st.z + st.ux * shift };
  });
}
