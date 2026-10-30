// Перелёт облака точек человека на новое место (
// «рой резче», «точки начинали с центра быстрее, чем с краёв», «не должны выходить за объём тела»). Чистый модуль без THREE и DOM: проверяется в node.
// Каждая точка совершает свой пролёт: своя задержка (центр тела срывается раньше края), своя длительность, своя дуга в сторону. Связи между точками нет,
// поэтому тело не тянется резинкой и не раздувается парусом: точки разлетаются и собираются в новой позе.
// Параметры рывка меняются ползунками на странице, поэтому это объект, а не константы.
export const FLIGHT = { delayEdge: 0.1, delayJitter: 0.19, durMin: 0.14, durMax: 0.30, arc: 0.07 };      // подобрано на странице глазами
// Ползунки: ключ FLIGHT, подпись, границы, шаг, подсказка на странице. Дуга не больше радиуса тела (0,15 м), иначе фигура раздувается в пузырь.
export const FLIGHT_KNOBS = [
  ['delayEdge', 'Задержка края, с', 0, 0.5, 0.01, 'центр тела срывается сразу, точки дальше от центра стартуют позже; 0 - все стартуют разом'],
  ['delayJitter', 'Разброс вылета, с', 0, 0.3, 0.01, 'каждой точке к старту добавляется случайная задержка до этого значения; больше - рваннее старт'],
  ['durMin', 'Пролёт короткий, с', 0.05, 0.8, 0.01, 'сколько летит самая быстрая точка; время каждой точки случайно между коротким и долгим'],
  ['durMax', 'Пролёт долгий, с', 0.05, 1.2, 0.01, 'сколько летит самая медленная точка; оба по 0,05 - почти телепорт, для заметного роя 0,15-0,3'],
  ['arc', 'Дуга пролёта, м', 0, 0.3, 0.01, 'на сколько точка уходит в сторону от прямой посреди полёта; 0 - летят по прямой'],
];
export const DEFAULTS = { ...FLIGHT };
export const burstSeconds = (f = FLIGHT) => f.delayEdge + f.delayJitter + Math.max(f.durMin, f.durMax);       // весь рывок от первой до последней точки

const ease = u => u * u * (3 - 2 * u);                                  // плавный старт и посадка пролёта

// План рывка: для каждой из n точек откуда она стартует (cur) и её случайные доли - без самих времён и дуги: те считаются из текущих FLIGHT
// на каждом кадре, поэтому ползунок меняет и перелёт, который уже идёт. edge01 - удаление от центра тела (0 - центр, 1 - край).
export function planFlight(n, edge01, nowMs, cur, random = Math.random) {
  const p0 = Float32Array.from(cur.subarray(0, n * 3)), dir = new Float32Array(n * 3), jitter = new Float32Array(n), span = new Float32Array(n), amp = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    jitter[i] = random(); span[i] = random(); amp[i] = 0.3 + 0.7 * random();
    const a = random() * Math.PI * 2;                                                  // дуга в случайную сторону, по высоте вполовину меньше
    dir[i * 3] = Math.cos(a); dir[i * 3 + 1] = Math.sin(a) * 0.5; dir[i * 3 + 2] = Math.sin(a * 1.7);
  }
  return { t0: nowMs, p0, dir, edge: Float32Array.from(edge01.subarray ? edge01.subarray(0, n) : edge01), jitter, span, amp, get endMs() { return nowMs + burstSeconds() * 1000; } };
}

// Положение точки i в момент nowMs: старт -> цель (gx, gy, gz) по плавной дуге; запись в cur[k..k+2]. Возвращает true, когда точка уже на цели.
export function evalFlight(plan, i, cur, k, gx, gy, gz, nowMs) {
  const delay = plan.edge[i] * FLIGHT.delayEdge + plan.jitter[i] * FLIGHT.delayJitter;
  const dur = Math.max(1e-3, Math.min(FLIGHT.durMin, FLIGHT.durMax) + plan.span[i] * Math.abs(FLIGHT.durMax - FLIGHT.durMin));
  const u = ((nowMs - plan.t0) / 1000 - delay) / dur;
  if (u >= 1) { cur[k] = gx; cur[k + 1] = gy; cur[k + 2] = gz; return true; }
  if (u <= 0) { cur[k] = plan.p0[i * 3]; cur[k + 1] = plan.p0[i * 3 + 1]; cur[k + 2] = plan.p0[i * 3 + 2]; return false; }
  const e = ease(u), arc = Math.sin(Math.PI * u), m = FLIGHT.arc * plan.amp[i];
  cur[k] = plan.p0[i * 3] + (gx - plan.p0[i * 3]) * e + plan.dir[i * 3] * m * arc;
  cur[k + 1] = plan.p0[i * 3 + 1] + (gy - plan.p0[i * 3 + 1]) * e + plan.dir[i * 3 + 1] * m * arc;
  cur[k + 2] = plan.p0[i * 3 + 2] + (gz - plan.p0[i * 3 + 2]) * e + plan.dir[i * 3 + 2] * m * arc;
  return false;
}

// Случайная перестановка целей внутри части тела.
// oldGroups[i] - часть тела, где точка i стояла в прежней позе, newGroups[j] - часть тела места j в новой. Результат p: точка i летит в место p[i]; это
// перестановка (каждое место занято ровно одной точкой), а точки и места, которым не хватило пары в своей части, пара находится случайно между собой.
export function assignTargets(oldGroups, newGroups, random = Math.random) {
  const n = newGroups.length, out = new Int32Array(n).fill(-1), shuffle = a => { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
  const places = new Map(), spare = [];
  newGroups.forEach((g, j) => { if (!places.has(g)) places.set(g, []); places.get(g).push(j); });
  places.forEach(list => shuffle(list));
  for (let i = 0; i < n; i++) {
    const list = places.get(oldGroups[i] ?? newGroups[i]);
    if (list && list.length) out[i] = list.pop(); else spare.push(i);
  }
  const free = shuffle([...places.values()].flat());
  spare.forEach(i => { out[i] = free.pop(); });
  return out;
}

// Случайная перестановка целей: точка номер i летит не в место номер i, а в случайное место новой позы; набор мест тот же, форма после посадки прежняя.
export function shuffled(n, random = Math.random) {
  const p = Uint16Array.from({ length: n }, (_, i) => i);
  for (let i = n - 1; i > 0; i--) { const j = Math.floor(random() * (i + 1)); [p[i], p[j]] = [p[j], p[i]]; }
  return p;
}
