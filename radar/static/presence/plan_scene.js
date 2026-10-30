// План квартиры как источник, всё на сцене - его представление (план исходный, остальное -
// выдавливание; свойства рисовки: блоки, прозрачность; точки). Модуль чистый: без THREE, без страницы, проверяется в node.
//
// План (layout от шлюза) -> элементы {id, kind, style, rect, h} -> выдавливание по таблице стилей -> точки.
// Как рисовать элемент, решает только таблица STYLES: режим (points, blocks, none), прозрачность, шаг, затухание вверх,
// крышка сверху, роль «прорезь» для дверей, occlude - гаснуть на луче от камеры к центру сцены. Менять вид - менять таблицу, не код.

export const LOAD_BEARING_T_M = 0.4;       // стена этой толщины и толще - несущая
export const BLOCK_MIN_H_M = 0.4;          // ниже - предмет на столе или лампа: реквизит, не мебель
export const BLOCK_MIN_AREA_M2 = 0.25;     // мельче - стул и тумба не загораживают проход; те же пороги на сервере (presence_nav)
const DOOR_WORD = /двер/i;                 // проём-дверь по названию из проекта; окно стену не прорезает
const ROUTER_BOX_M = 0.22, ROUTER_DEPTH_M = 0.12, ROUTER_WALL_RADIUS_M = 1.2;   // роутер ближе этого к стене считается висящим на ней   // габариты метки роутера на стене: в плане у него только точка и высота
const SEAT_WORD = /стул|кресл/i;           // сиденье: на нём сидят, стоять и ходить можно рядом и над ним; кубом не рисуется, для пути не преграда
const WORLD_UP_ROUND = 1e3;                // ключ дедупликации: координата в миллиметрах

// Таблица свойств рисовки. plan - как элемент выглядит на самом плане на полу; mode - как выдавливается вверх.
export const STYLES = {
  outer:     { mode: 'points', step: 0.12, cap: false, size: 0.6, px: [2, 14], opacity: 0.26, fade: 0.6, color: [0.5, 0.6, 0.8], plan: { fill: '#27427c' },
             occlude: { f0: 0.5, f1: 0.9, floor: 0.12 } },                                      // наружные стены: прозрачные и, как в Genshin, гаснут, когда встают между камерой и центром сцены; в r0 метров от луча их нет, к r1 возвращаются
  wall:      { mode: 'points', step: 0.12, cap: false, size: 0.6, px: [1.5, 16], opacity: 0.3, fade: 0.55, color: [0.5, 0.6, 0.8], plan: { fill: '#35599f' } },
  bearing:   { mode: 'points', step: 0.12, cap: false, size: 0.85, px: [2, 20], opacity: 0.42, fade: 0.55, color: [0.7, 0.78, 0.92], plan: { fill: '#4a70bd' } },
  furniture: { mode: 'points', step: 0.1, cap: true, size: 0.7, px: [2, 20], opacity: 0.3, fade: 0.0, color: [0.44, 0.56, 0.84], plan: { fill: 'rgba(19,36,75,0.26)', edge: '#3c5ea8' } },
  router:    { mode: 'points', step: 0.04, cap: true, size: 0.8, px: [2, 7], opacity: 0.45, fade: 0.0, color: [0.5, 0.8, 0.95], plan: {} },   // роутер висит на стене на высоте из плана, не стоит на полу
  prop:      { mode: 'none', plan: {} },                                        // ноутбук, лампа: не рисуются
  seat:      { mode: 'none', plan: { fill: 'rgba(19,36,75,0.26)', edge: '#3c5ea8' } },        // стул, кресло: на плане кружок, вверх не растёт
  door:      { mode: 'none', role: 'cut', reach: 0.25, plan: { jamb: '#9db8ee', cut: 0.5 } },   // reach: прорезь шире модели с обеих сторон, у открытой двери модель смещена от стены на створку
  window:    { mode: 'none', plan: { edge: '#6f8fd6', t: 0.1 } },
};
// Формы мебели: части в долях габарита (x и z от -0.5 до 0.5 вокруг центра, y от 0 до 1 высоты). Спереди у предмета +z (так модели из
// Sweet Home 3D стоят при угле 0), поэтому спинка и изголовье на -z. Нет формы - цельный ящик.
const LEG = 0.07;                          // сечение ножки стола в долях габарита
export const SHAPES = {
  sofa:  [{ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [0, 0.5] },                   // сиденье
          { x: [-0.5, 0.5], z: [-0.5, -0.25], y: [0, 1] },                   // спинка
          { x: [-0.5, -0.4], z: [-0.5, 0.5], y: [0, 0.75] }, { x: [0.4, 0.5], z: [-0.5, 0.5], y: [0, 0.75] }],   // подлокотники
  sofabook: [{ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [0, 0.5] },               // диван-книжка: сиденье и спинка, без подлокотников
             { x: [-0.5, 0.5], z: [-0.5, -0.25], y: [0, 1] }],
  bed:   [{ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [0, 0.6] },                   // матрас на раме
          { x: [-0.5, 0.5], z: [-0.5, -0.45], y: [0, 1] }],                  // изголовье
  table: [{ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [0.9, 1] },                   // столешница
          ...[[-0.5, -0.5 + LEG], [0.5 - LEG, 0.5]].flatMap(x => [[-0.5, -0.5 + LEG], [0.5 - LEG, 0.5]].map(z => ({ x, z, y: [0, 0.9] })))],   // четыре ножки
};
// Шкаф-стенка: задняя стенка, стойки между секциями, полки по высоте; в секции справа (+x, у стола) на полках книги плотными рядами.
const SECTIONS = 3, SHELVES = 4, PANEL = 0.02, SHELF_T = 0.015;     // секций, полок на секцию, толщина стойки и полки в долях габарита
const unit = [{ x: [-0.5, 0.5], z: [-0.5, -0.46], y: [0, 1] },
  ...Array.from({ length: SECTIONS + 1 }, (_, k) => { const x = -0.5 + k / SECTIONS; return { x: [Math.max(-0.5, x - PANEL / 2), Math.min(0.5, x + PANEL / 2)], z: [-0.5, 0.5], y: [0, 1] }; }),
  ...Array.from({ length: SHELVES + 1 }, (_, j) => ({ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [j / SHELVES, Math.min(1, j / SHELVES + SHELF_T)] })),
  ...Array.from({ length: SHELVES }, (_, j) => ({ x: [0.5 - 1 / SECTIONS + PANEL, 0.5 - PANEL], z: [-0.4, 0.3], y: [(j + 0.08) / SHELVES, (j + 0.8) / SHELVES] }))];   // книги
SHAPES.wallunit = unit;
const SHELF_BOARDS = 5, BOARD_T = 0.02;       // этажерка: пять досок до потолка, без стенок; число совпадает с полем shelves предмета в плане
SHAPES.shelves = Array.from({ length: SHELF_BOARDS }, (_, k) => ({ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [(k + 1) / SHELF_BOARDS - BOARD_T, (k + 1) / SHELF_BOARDS] }))
  .concat([{ x: [-0.03, 0.03], z: [-0.5, -0.46], y: [0, 1] }]);      // брус в середине дальнего (-z) торца от пола до потолка (столб, обмотанный верёвкой): тоньше шага точек, рисуется одной колонкой
SHAPES.tub = [{ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [0, 0.2] },                    // ванна: дно и четыре борта, внутри пусто
  { x: [-0.5, -0.43], z: [-0.5, 0.5], y: [0, 1] }, { x: [0.43, 0.5], z: [-0.5, 0.5], y: [0, 1] },
  { x: [-0.5, 0.5], z: [-0.5, -0.43], y: [0, 1] }, { x: [-0.5, 0.5], z: [0.43, 0.5], y: [0, 1] }];
SHAPES.toilet = [{ x: [-0.28, 0.28], z: [-0.18, 0.4], y: [0, 0.45] },                // основание под чашей
  { x: [-0.4, 0.4], z: [-0.2, 0.5], y: [0.45, 0.58] },                            // чаша с сиденьем, спереди (+z) выступает
  { x: [-0.42, 0.42], z: [-0.5, -0.2], y: [0.4, 1] },                             // бачок у стены (-z)
  { x: [-0.3, 0.3], z: [-0.2, 0.3], y: [0.58, 0.66] }];                           // крышка над чашей
const SHAPE_WORDS = [[/этажерк|полочк/i, 'shelves'], [/ванн/i, 'tub'], [/унитаз/i, 'toilet'], [/стенк/i, 'wallunit'], [/книжк/i, 'sofabook'], [/диван/i, 'sofa'], [/кроват/i, 'bed'], [/стол/i, 'table']];   // по названию из проекта
export const shapeOf = title => SHAPE_WORDS.find(([re]) => re.test(title || ''))?.[1];

// Верх опоры для сидящего и лежащего, в долях высоты предмета: сиденье дивана и стула, матрас кровати.
export const SURFACE = { sofa: 0.5, sofabook: 0.5, bed: 0.6, seat: 0.5, toilet: 0.55, tub: 0.3 };
// Верх поверхности для устройства (телефон, ПК): матрас и сиденье дивана по SURFACE, у стола и тумбы - их полная высота. Сиденье стула не поверхность (на нём не лежат
// телефоны), высокие шкафы и холодильник тоже: выше DEVICE_MAX_SURFACE_M ничего не лежит.
export const DEVICE_MAX_SURFACE_M = 1.1;      // выше ничего не лежит: кошачья этажерка 1,5 м - место лёжки кошек (SAM-111), не устройств
const SOFT_SHAPES = ['sofa', 'sofabook', 'bed'];
export const DEVICE_SLACK_M = 0.15;         // полка не выше текущей высоты устройства плюс этот запас: ложится вниз, а не прыгает вверх
export function deviceSurfaceAt(elements, x, z, y = Infinity) {
  let best = 0;
  for (const e of elements) {
    if (e.kind === 'seat' || !inRect(x, z, e.rect)) continue;
    const levels = SOFT_SHAPES.includes(e.shape) ? [SURFACE[e.shape] * e.h]                                  // матрас и сиденье дивана: верх - не спинка
      : e.shelves > 0 ? Array.from({ length: e.shelves }, (_, k) => e.h * (k + 1) / e.shelves)               // каждая полка - поверхность, равными шагами до верха
      : e.kind === 'furniture' && e.h <= DEVICE_MAX_SURFACE_M ? [e.h] : [];                                   // стол, тумба, плита, стиралка, бачок и край ванны - полная высота
    for (const top of levels) if (top <= y + DEVICE_SLACK_M) best = Math.max(best, top);
  }
  return best;
}
export const SURFACE_SEARCH_STEP_M = 0.02;       // шаг поиска ближайшей опоры по расстоянию: на радиусе 0,3 м это 15 колец по 16 направлений; поверхности, различающиеся меньше шага, считаются равно близкими
export const SURFACE_SEARCH_ANGLES = 16;          // направлений на каждом кольце поиска
export const SURFACE_STICK_M = 0.08;              // прежняя поверхность остаётся, пока новая не ближе неё больше чем на столько: круг не мигает между двумя близкими
// Ближайшая к (x, z) опора для устройства не дальше maxM: кольца по возрастанию расстояния, на первом кольце, где есть опора, берётся верхняя (стол выше дивана). Опоры нет - null.
export function nearestSurface(elements, x, z, y, maxM) {
  for (let r = 0; r <= maxM + 1e-9; r += SURFACE_SEARCH_STEP_M) {
    const angles = r === 0 ? 1 : SURFACE_SEARCH_ANGLES;
    let best = null;
    for (let k = 0; k < angles; k++) {
      const px = x + Math.cos(2 * Math.PI * k / angles) * r, pz = z + Math.sin(2 * Math.PI * k / angles) * r, top = deviceSurfaceAt(elements, px, pz, y);
      if (top > 0 && (!best || top > best.top)) best = { x: px, z: pz, top, dist: r };
    }
    if (best) return best;
  }
  return null;
}
// Как nearestSurface, но липнет к прежней точке опоры previous {x, z}: она остаётся, пока всё ещё лежит на опоре и не дальше новой больше чем на SURFACE_STICK_M.
export function stickySurface(elements, x, z, y, maxM, previous) {
  const found = nearestSurface(elements, x, z, y, maxM);
  if (!found || !previous) return found;
  const top = deviceSurfaceAt(elements, previous.x, previous.z, y), dist = Math.hypot(previous.x - x, previous.z - z);
  return top > 0 && dist <= maxM && dist <= found.dist + SURFACE_STICK_M ? { x: previous.x, z: previous.z, top, dist } : found;
}
// Поза человека над опорой
export const POSE_OF = { sofa: 'sit', sofabook: 'sit', seat: 'sit', toilet: 'sit', bed: 'lie', tub: 'lie' };
export function supportPose(elements, x, z) {          // поза по опоре под точкой; опоры нет - null, значит стоит
  for (const e of elements) { const pose = POSE_OF[e.shape] ?? POSE_OF[e.kind]; if (pose && inRect(x, z, e.rect)) return pose; }
  return null;
}
export function surfaceAt(elements, x, z) {          // высота верха опоры под точкой, 0 - пол
  let top = 0;
  for (const e of elements) {
    const f = SURFACE[e.shape] ?? SURFACE[e.kind];
    if (f && inRect(x, z, e.rect)) top = Math.max(top, f * e.h);
  }
  return top;
}

// Крутящийся стул: сидящий на нём иногда разворачивается к ближайшей двери. Угол плана к центру двери; не на стуле или дверей нет - null.
export function swivelFace(elements, x, z) {
  const seat = elements.find(e => e.kind === 'seat' && inRect(x, z, e.rect));
  const doors = elements.filter(e => e.kind === 'door');
  if (!seat || !doors.length) return null;
  const d = doors.reduce((best, e) => Math.hypot(e.rect.cx - seat.rect.cx, e.rect.cz - seat.rect.cz) < Math.hypot(best.rect.cx - seat.rect.cx, best.rect.cz - seat.rect.cz) ? e : best);
  return Math.atan2(d.rect.cz - seat.rect.cz, d.rect.cx - seat.rect.cx);
}

export const PRIORITY = ['bearing', 'wall', 'outer', 'furniture', 'prop'];            // при совпадении точки выигрывает стиль левее

export function inRect(x, z, r) {          // поворот предмета вокруг центра, как в плане (rotation.y = -angle)
  const dx = x - r.cx, dz = z - r.cz, c = Math.cos(r.a), s = Math.sin(r.a);
  return Math.abs(dx * c + dz * s) <= r.w / 2 && Math.abs(-dx * s + dz * c) <= r.d / 2;
}

// Роутер висит на стене плашмя: тонкой стороной к ближайшей стене, вплотную к её грани; стены рядом нет - как был, по точке плана.
export function routerRect(layout) {
  const r = layout.router, plain = { cx: r.x, cz: r.z, w: ROUTER_BOX_M, d: ROUTER_DEPTH_M, a: 0 };
  let best = null;
  const outer = new Set(layout.outer_walls || []);                          // роутер висит на внутренней стене, наружная годится, только если внутренней рядом нет
  const walls = (layout.walls || []).map((w, i) => ({ w, inner: !outer.has(i) }));
  for (const { w, inner } of walls) {
    const vx = w.x2 - w.x1, vz = w.z2 - w.z1, l2 = vx * vx + vz * vz;
    const t = l2 ? Math.min(1, Math.max(0, ((r.x - w.x1) * vx + (r.z - w.z1) * vz) / l2)) : 0, px = w.x1 + t * vx, pz = w.z1 + t * vz, dist = Math.hypot(r.x - px, r.z - pz);
    const near = dist <= ROUTER_WALL_RADIUS_M;
    if (!best || (inner && near && !best.inner) || (inner === best.inner && dist < best.dist)) best = { dist, px, pz, w, a: Math.atan2(vz, vx), inner };
  }
  if (!best || best.dist > ROUTER_WALL_RADIUS_M || best.dist < 1e-6) return plain;
  const nx = (r.x - best.px) / best.dist, nz = (r.z - best.pz) / best.dist, off = best.w.t / 2 + ROUTER_DEPTH_M / 2;   // от оси стены к центру коробки: половина толщины и половина глубины
  return { cx: best.px + nx * off, cz: best.pz + nz * off, w: ROUTER_BOX_M, d: ROUTER_DEPTH_M, a: best.a };
}

export function elementsFromLayout(layout) {
  const outer = new Set(layout.outer_walls || []), els = [];
  (layout.walls || []).forEach((w, i) => {
    const dx = w.x2 - w.x1, dz = w.z2 - w.z1, len = Math.hypot(dx, dz);
    els.push({ id: `wall-${i}`, kind: 'wall', style: outer.has(i) ? 'outer' : (w.t >= LOAD_BEARING_T_M ? 'bearing' : 'wall'), h: w.h,
      rect: { cx: (w.x1 + w.x2) / 2, cz: (w.z1 + w.z2) / 2, w: len + w.t, d: w.t, a: Math.atan2(dz, dx) } });    // + толщина: углы стен смыкаются
  });
  if (layout.router) els.push({ id: 'router', kind: 'router', style: 'router', title: 'роутер', base: layout.router.h - ROUTER_BOX_M / 2, h: ROUTER_BOX_M,
    rect: routerRect(layout) });
  for (const o of layout.objects || []) {
    const kind = o.kind === 'opening' ? (DOOR_WORD.test(o.title || '') ? 'door' : 'window')
      : SEAT_WORD.test(o.title || '') ? 'seat'
      : (o.h >= BLOCK_MIN_H_M && (o.solid === true || o.w * o.d >= BLOCK_MIN_AREA_M2) ? 'furniture' : 'prop');      // предмет с явной меткой solid рисуется всегда: плита и стиралка мельче 0,25 м2, но стоят
    els.push({ id: o.id, kind, style: kind, h: o.h, title: o.title, shelves: o.shelves, shape: kind === 'furniture' ? shapeOf(o.title) : undefined, approx: !!o.approx, rect: { cx: o.x + o.w / 2, cz: o.z + o.d / 2, w: o.w, d: o.d, a: o.angle || 0 } });
  }
  return els;
}

export function cutsOf(elements, styles = STYLES) {
  return elements.filter(e => styles[e.style]?.role === 'cut')
    .map(e => ({ rect: { ...e.rect, d: e.rect.d + 2 * (styles[e.style].reach ?? 0) }, h: e.h }));
}

// Точки на поверхности выдавленного ящика: торцы и обе стороны на всю высоту, сверху - если style.cap. Тонкая стена (толщина не
// больше шага) - один лист по оси. Что попало в прорезь ниже её высоты, выкидывается.
function boxPoints(r, y0, y1, style, cuts, out) {
  const step = style.step, nx = Math.max(1, Math.ceil(r.w / step)), nz = Math.max(1, Math.ceil(r.d / step)), ny = Math.max(1, Math.ceil((y1 - y0) / step));
  const xs = Array.from({ length: nx + 1 }, (_, i) => -r.w / 2 + r.w * i / nx);
  const zs = r.d <= step ? [0] : Array.from({ length: nz + 1 }, (_, i) => -r.d / 2 + r.d * i / nz);
  const c = Math.cos(r.a), s = Math.sin(r.a);
  const put = (lx, y, lz) => {
    const x = r.cx + lx * c - lz * s, z = r.cz + lx * s + lz * c;
    if (!cuts.some(k => y < k.h && inRect(x, z, k.rect))) out.push([x, y, z]);
  };
  for (let j = 0; j <= ny; j++) {
    const y = y0 + (y1 - y0) * j / ny;
    for (const lx of xs) for (const lz of zs.length === 1 ? zs : [zs[0], zs[zs.length - 1]]) put(lx, y, lz);      // две стороны
    if (zs.length > 1) for (const lx of [xs[0], xs[nx]]) for (const lz of zs) put(lx, y, lz);                      // торцы
    if (style.cap && j === ny) for (const lx of xs) for (const lz of zs) put(lx, y, lz);                          // крышка
  }
}

// Выдавливание элемента: по частям его формы (SHAPES) или целым ящиком от основания base на высоту h.
export function extrudePoints(el, style, cuts = []) {
  const { rect: r, h } = el, base = el.base ?? 0, out = [], c = Math.cos(r.a), s = Math.sin(r.a);
  const parts = (el.shape && SHAPES[el.shape]) || [{ x: [-0.5, 0.5], z: [-0.5, 0.5], y: [0, 1] }];
  for (const p of parts) {
    const lx = (p.x[0] + p.x[1]) / 2 * r.w, lz = (p.z[0] + p.z[1]) / 2 * r.d;           // центр части в локальных осях предмета
    const sub = { cx: r.cx + lx * c - lz * s, cz: r.cz + lx * s + lz * c, w: (p.x[1] - p.x[0]) * r.w, d: (p.z[1] - p.z[0]) * r.d, a: r.a };
    boxPoints(sub, base + p.y[0] * h, base + p.y[1] * h, style, cuts, out);
  }
  return out;
}

// Все точки по стилям: {стиль: Float32Array}. Общая сетка снимает дубли там, где стены сходятся, левее в PRIORITY сильнее.
// normalsOut - необязательный объект: туда по стилям кладутся нормали стен (nx, nz на точку), по ним шейдер понимает, насколько прямо камера смотрит на стену.
export function extrudeAll(elements, styles = STYLES, normalsOut = null) {
  const cuts = cutsOf(elements, styles), seen = new Map(), byStyle = {};
  const order = [...elements].sort((a, b) => PRIORITY.indexOf(a.style) - PRIORITY.indexOf(b.style));
  for (const el of order) {
    const st = styles[el.style];
    if (!st || st.mode !== 'points') continue;
    for (const [x, y, z] of extrudePoints(el, st, cuts)) {
      const q = st.step, key = Math.round(x / q) + ',' + Math.round(y / q) + ',' + Math.round(z / q);
      if (seen.has(key)) continue;
      seen.set(key, true);
      (byStyle[el.style] ||= []).push(x, y, z);
      if (normalsOut) (normalsOut[el.style] ||= []).push(-Math.sin(el.rect.a), Math.cos(el.rect.a));
    }
  }
  if (normalsOut) for (const k of Object.keys(normalsOut)) normalsOut[k] = new Float32Array(normalsOut[k]);
  return Object.fromEntries(Object.entries(byStyle).map(([k, v]) => [k, new Float32Array(v)]));
}

// Пол от шлюза: ряды [ряд, от, до) клеток. Возвращает функцию «здесь пол».
export function makeOnFloor(floor, rooms = [], epsM = 0.05) {
  if (!floor) return (x, z) => rooms.some(r => x >= r.x - epsM && x <= r.x + r.w + epsM && z >= r.z - epsM && z <= r.z + r.d + epsM);
  const mask = new Uint8Array(floor.w * floor.h);
  for (const [r, a, b] of floor.runs) mask.fill(1, r * floor.w + a, r * floor.w + b);
  return (x, z) => {
    const c = Math.floor((x - floor.x0) / floor.cell), r = Math.floor((z - floor.z0) / floor.cell);
    return c >= 0 && r >= 0 && c < floor.w && r < floor.h && mask[r * floor.w + c] === 1;
  };
}
