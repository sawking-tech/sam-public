// Карта покрытия сети на странице радара (SAM-118): уровень роутера по клеткам плана, окрашенный полосами качества.
// Чистый модуль без THREE и DOM: данные приходят из /sam/presence/coverage, проверяется в node.
export const COVERAGE_ALPHA = 150;                  // непрозрачность слоя 0..255: план под ним остаётся читаемым
export const BAND_RGB = {                           // цвет полосы по её имени из ответа сервера: опорные цвета на серединах полос
  'отлично': [31, 157, 85],
  'хорошо': [139, 195, 74],
  'средне': [242, 201, 76],
  'слабо': [242, 153, 74],
  'почти нет': [214, 69, 69],
};
export const BAND_FALLBACK_RGB = [128, 128, 128];

// Имя полосы для уровня: bands идут от сильной к слабой, min_db null - нижняя граница без предела.
export function bandName(dbm, bands) {
  for (const b of bands) if (b.min_db === null || dbm >= b.min_db) return b.name;
  return bands[bands.length - 1].name;
}

// Опорные точки цвета: цвет полосы стоит на её середине, крайние полосы - на EDGE_PAD_DB от своей границы; между опорными цвет плавно перетекает
// (зелёный - жёлтый - красный без резкой границы), за крайними остаётся цвет крайней полосы.
export const EDGE_PAD_DB = 6.0;
export function colourStops(bands) {
  return bands.map((b, i) => {
    const upper = i > 0 ? bands[i - 1].min_db : null;
    const level = b.min_db === null ? bands[i - 1].min_db - 2 * EDGE_PAD_DB : upper === null ? b.min_db + EDGE_PAD_DB : (b.min_db + upper) / 2;
    return { level, rgb: BAND_RGB[b.name] || BAND_FALLBACK_RGB };
  });
}

export function bandColour(dbm, bands) {
  const stops = colourStops(bands);                                                         // по убыванию уровня
  if (dbm >= stops[0].level) return stops[0].rgb;
  for (let i = 1; i < stops.length; i++) {
    if (dbm >= stops[i].level) {
      const share = (stops[i - 1].level - dbm) / (stops[i - 1].level - stops[i].level);
      return stops[i - 1].rgb.map((v, k) => Math.round(v + (stops[i].rgb[k] - v) * share));
    }
  }
  return stops[stops.length - 1].rgb;
}

// RGBA-текстура сетки: строка 0 - дальний край по z, как у карты посещений; клетки вне пола (onFloor ложь) прозрачные.
export function coverageTexture(payload, onFloor) {
  const { w, h, i0, k0, cell_m: cell, levels, bands } = payload, data = new Uint8Array(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (!onFloor((i0 + x + 0.5) * cell, (k0 + y + 0.5) * cell)) continue;
    const rgb = bandColour(levels[y * w + x], bands), o = ((h - 1 - y) * w + x) * 4;
    data[o] = rgb[0]; data[o + 1] = rgb[1]; data[o + 2] = rgb[2]; data[o + 3] = COVERAGE_ALPHA;
  }
  return { w, h, data };
}

// Поле стрелок градиента: куда и как круто растёт ожидаемый уровень. По нему видно, где узор уровня различает места (стрелки длинные), а где поле плоское.
export const ARROW_STRIDE = 3;                      // стрелка в каждой третьей клетке сетки 0,25 м: раз в 0,75 м, иначе поле сливается в штриховку
export const ARROW_MIN_SLOPE_DB_PER_M = 0.5;        // ниже этой крутизны стрелка не рисуется: поле плоское, направление случайное
export const ARROW_FULL_SLOPE_DB_PER_M = 6.0;       // при этой крутизне и выше стрелка во всю длину шага; у роутера крутизна 3-6 дБ/м, в дальних комнатах 0,5-1,5
export const WALL_STEP_DB = 5.0;                    // разность соседних клеток больше этой - между ними стена (модель даёт 8 и больше дБ на стену): такая пара в градиент не идёт
export const ARROW_STYLE = { rgb: [20, 24, 40], alpha: 0.85, width: 1.5, headShare: 0.3 };

// Градиент в клетке (y, x) сетки payload по соседям на полу без стены между ними; нет пары хотя бы по одной оси - null.
function gradientAt(payload, onFloor, x, y) {
  const { w, h, i0, k0, cell_m: cell, levels } = payload;
  const floor = (xx, yy) => xx >= 0 && yy >= 0 && xx < w && yy < h && onFloor((i0 + xx + 0.5) * cell, (k0 + yy + 0.5) * cell);
  const level = (xx, yy) => levels[yy * w + xx];
  const axis = (dx, dy) => {                                                                   // производная по оси: центральная, если оба соседа годятся, иначе односторонняя
    const here = level(x, y), pairs = [];
    for (const s of [1, -1]) {
      const xx = x + s * dx, yy = y + s * dy;
      if (floor(xx, yy) && Math.abs(level(xx, yy) - here) <= WALL_STEP_DB) pairs.push(s * (level(xx, yy) - here) / cell);
    }
    return pairs.length ? pairs.reduce((a, b) => a + b, 0) / pairs.length : null;
  };
  const gx = axis(1, 0), gz = axis(0, 1);
  return gx === null || gz === null ? null : [gx, gz];
}

// Стрелки по сетке: {x, z} центр клетки в метрах плана, {dx, dz} единичное направление роста уровня, slope в дБ/м; клетки вне пола и плоские пропущены.
export function gradientArrows(payload, onFloor, stride = ARROW_STRIDE, minSlope = ARROW_MIN_SLOPE_DB_PER_M) {
  const { w, h, i0, k0, cell_m: cell } = payload, out = [];
  for (let y = stride >> 1; y < h; y += stride) for (let x = stride >> 1; x < w; x += stride) {
    if (!onFloor((i0 + x + 0.5) * cell, (k0 + y + 0.5) * cell)) continue;
    const g = gradientAt(payload, onFloor, x, y);
    if (!g) continue;
    const slope = Math.hypot(g[0], g[1]);
    if (slope < minSlope) continue;
    out.push({ x: (i0 + x + 0.5) * cell, z: (k0 + y + 0.5) * cell, dx: g[0] / slope, dz: g[1] / slope, slope });
  }
  return out;
}

// Рисует стрелки на холсте 2d: холст покрывает сетку payload, scale пикселей на метр; длина стрелки от нуля до шага сетки по крутизне.
export function paintArrows(g, arrows, payload, scale, fullSlope = ARROW_FULL_SLOPE_DB_PER_M, stride = ARROW_STRIDE) {
  const { i0, k0, cell_m: cell } = payload, maxLen = stride * cell * 0.8;
  g.strokeStyle = `rgba(${ARROW_STYLE.rgb.join(',')},${ARROW_STYLE.alpha})`; g.lineWidth = ARROW_STYLE.width;
  for (const a of arrows) {
    const len = maxLen * Math.min(1, a.slope / fullSlope), head = len * ARROW_STYLE.headShare;
    const x0 = (a.x - i0 * cell - a.dx * len / 2) * scale, y0 = (a.z - k0 * cell - a.dz * len / 2) * scale;
    const x1 = x0 + a.dx * len * scale, y1 = y0 + a.dz * len * scale, ang = Math.atan2(y1 - y0, x1 - x0);
    g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1);
    g.moveTo(x1, y1); g.lineTo(x1 - head * scale * Math.cos(ang - Math.PI / 6), y1 - head * scale * Math.sin(ang - Math.PI / 6));
    g.moveTo(x1, y1); g.lineTo(x1 - head * scale * Math.cos(ang + Math.PI / 6), y1 - head * scale * Math.sin(ang + Math.PI / 6));
    g.stroke();
  }
}

// Строка для панели: лучшие места по уровню и худшее.
export function coverageSummary(places, best = 3) {
  if (!places.length) return '';
  const fmt = p => `${p.label} ${Math.round(p.dbm)} дБм (${p.band})`;
  return `лучше всего: ${places.slice(0, best).map(fmt).join('; ')}. Слабее всего: ${fmt(places[places.length - 1])}`;
}

// Кто на какой сети: «устройство - диапазон» по интерфейсу роутера; диапазон это подпись модели с тем же интерфейсом (модель одна на диапазон).
export function connectedText(connected, models) {
  const band = Object.fromEntries(models.map(m => [m.iface, m.label]));
  const short = n => n.split(/[-_ ]/)[0];
  return connected.map(c => `${short(c.name)} - ${band[c.iface] || c.iface}`).join('; ');
}

// Поле шага к роутеру за k секунд (/sam/presence/walk_fields): из каждой клетки стрелка туда, куда человек дошёл по полу к роутеру, длина - разница ожидаемого уровня.
// Цвет: выше шума носимого телефона - тёмная (движение различимо по уровню), ниже - серая, падение уровня по пути к роутеру - красная (тень стены).
export const WALK_M_PER_DB = 0.02;                  // 10 дБ разницы - стрелка 0,2 м: у клеток шаг 0,3 м, соседние стрелки не сливаются до 15 дБ
export const WALK_MAX_M = 0.4;                      // длиннее не рисуется: за 4 секунды и больше медиана 15-18 дБ, без потолка поле превращается в штриховку
export const WALK_STYLE = { above: [20, 24, 40], below: [120, 120, 120], falling: [214, 69, 69], alpha: 0.9, width: 1.5, headShare: 0.3 };

export function walkArrows(walk, seconds) {
  const field = walk.fields.find(f => f.seconds === seconds);
  if (!field) return [];
  const out = [];
  field.to.forEach((end, i) => {
    if (end === i) return;
    const [x, z] = walk.cells[i], [ex, ez] = walk.cells[end], d = Math.hypot(ex - x, ez - z), delta = field.delta_db[i];
    if (d === 0) return;
    out.push({ x, z, dx: (ex - x) / d, dz: (ez - z) / d, delta, kind: delta < 0 ? 'falling' : delta > walk.noise_db ? 'above' : 'below' });
  });
  return out;
}

export function paintWalkArrows(g, arrows, payload, scale) {
  const { i0, k0, cell_m: cell } = payload;
  g.lineWidth = WALK_STYLE.width;
  for (const a of arrows) {
    const len = Math.min(WALK_MAX_M, Math.abs(a.delta) * WALK_M_PER_DB), head = len * WALK_STYLE.headShare;
    if (len <= 0) continue;
    const x0 = (a.x - i0 * cell) * scale, y0 = (a.z - k0 * cell) * scale, x1 = x0 + a.dx * len * scale, y1 = y0 + a.dz * len * scale, ang = Math.atan2(y1 - y0, x1 - x0);
    g.strokeStyle = `rgba(${WALK_STYLE[a.kind].join(',')},${WALK_STYLE.alpha})`;
    g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1);
    g.moveTo(x1, y1); g.lineTo(x1 - head * scale * Math.cos(ang - Math.PI / 6), y1 - head * scale * Math.sin(ang - Math.PI / 6));
    g.moveTo(x1, y1); g.lineTo(x1 - head * scale * Math.cos(ang + Math.PI / 6), y1 - head * scale * Math.sin(ang + Math.PI / 6));
    g.stroke();
  }
}

// Строка сводки поля: секунды, медиана разницы, доля клеток выше шума и с падением уровня.
export function walkSummary(walk, seconds) {
  const f = walk.fields.find(x => x.seconds === seconds);
  if (!f) return '';
  return `шаг к роутеру ${seconds} с (${(walk.speed_m_s * seconds).toFixed(1)} м): медиана разницы ${f.median_db} дБ, выше шума ${walk.noise_db} дБ в ${Math.round(f.above_noise * 100)}% клеток, падение в ${Math.round(f.falling * 100)}%`;
}
