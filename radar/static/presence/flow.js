// Слой потока положения (SAM-118): как работает алгоритм на полу. Кольца среза, подходящие пути окна, итоговая зона по клеткам и точка.
// Чистый модуль без THREE и DOM: рисует в контекст 2d по ответу /sam/presence/flow?detail=1, проверяется в node.
import { FIELD_PX_PER_M, PROBABILITY_RAMP, probabilityRgb } from './fields.js';

export const RING_MIN_SHARE = 0.4;               // клетка кольца: вероятность не ниже этой доли максимума среза
export const EXCLUDED_STYLE = { rgb: [14, 20, 44], alpha: 0.42 };    // зоны колец, которые не лежат ни на одном законном пути: тёмная полупрозрачная дымка
export const CONTOUR_SHARE = 0.8;                // контур уровня секунды: клетки кольца не ниже этой доли его максимума; кольцо шириной около 9 дБ толстое, 0,8 оставляет тонкую полосу
export const LEGAL_MIN_SHARE = 0.05;             // клетка на пути: вероятность не ниже этой доли наибольшей в срезе
export const ZONE_MIN_SHARE = 0.06;              // клетка зоны: вероятность не ниже этой доли максимума распределения
export const ZONE_ALPHA = [0.3, 0.85];           // прозрачность зоны в виде алгоритма от малой вероятности к наибольшей
export const FIELD_ALPHA = [0.55, 0.95];         // прозрачность клетки в виде «поле вероятности»: плотный цвет, ноль виден чёрным
export const PATHS_SHOWN = 40;                   // путей рисуется из ста: линии тонкие, пучок читается
export const PATH_STYLE = { rgb: [255, 255, 255], alpha: 0.16, width: 1.2 };
export const END_RADIUS_M = 0.04;                // конец пути, 8 см в поперечнике
export const POINT_RADIUS_M = 0.07;              // итоговая точка, 14 см в поперечнике: с телефон
export const PREVIOUS_RADIUS_M = 0.05;           // прошлая точка, полое кольцо, 10 см
export const TRAIL_RGB = [255, 255, 255];        // цепочка точек секунд окна
export const TRAIL_LINE_ALPHA = 0.35;
export const TRAIL_FONT_PX = 11;                 // номер секунды у точки
export const OUTLINE_PX = 1.5;                   // кайма точки: тонкая, чтобы не раздувать размер
export const ASCENT_LENGTH_M = 0.6;              // стрелка роста уровня (к роутеру) от точки: длина
export const ASCENT_HEAD_M = 0.12;               // наконечник стрелки
export const ASCENT_STYLE = { rgb: [255, 214, 90], alpha: 0.95, width: 2 };

const px = (xz, bbox, scale) => [(xz[0] - bbox.minX) * scale, (xz[1] - bbox.minZ) * scale];
const lerp = (range, t) => range[0] + (range[1] - range[0]) * Math.min(1, Math.max(0, t));
const rgba = (rgb, alpha) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha.toFixed(3)})`;

export function cellsAbove(values, share) {      // номера клеток, где значение не ниже доли максимума; пусто - пусто
  const top = Math.max(0, ...values);
  return top > 0 ? values.map((v, i) => [v, i]).filter(([v]) => v >= share * top).map(([, i]) => i) : [];
}

function fillCell(g, cell, bbox, stepM, scale, style) {
  const [x, y] = px(cell, bbox, scale), side = stepM * scale;
  g.fillStyle = style;
  g.fillRect(x - side / 2, y - side / 2, side, side);
}

export function paintZone(g, cells, values, bbox, stepM, scale = FIELD_PX_PER_M, minShare = ZONE_MIN_SHARE, alpha = ZONE_ALPHA) {      // зона: цвет и прозрачность клетки по её вероятности относительно наибольшей; minShare 0 - все доступные клетки, нулевые чёрные
  const top = Math.max(0, ...values);
  for (const i of cellsAbove(values, minShare)) fillCell(g, cells[i], bbox, stepM, scale, rgba(probabilityRgb(values[i] / top), lerp(alpha, values[i] / top)));
}

export function contourCells(slice) {            // контур уровня секунды: клетки кольца не ниже CONTOUR_SHARE его максимума
  return cellsAbove(slice.parts?.rings || [], CONTOUR_SHARE);
}

export function paintContour(g, cells, slice, bbox, stepM, scale = FIELD_PX_PER_M) {      // контур уровня одной секунды: тонкая полоса клеток кольца, цвет по вероятности относительно максимума
  const ring = slice.parts?.rings || [], top = Math.max(0, ...ring);
  for (const i of contourCells(slice)) fillCell(g, cells[i], bbox, stepM, scale, rgba(probabilityRgb(ring[i] / top), lerp(ZONE_ALPHA, ring[i] / top)));
}

export function paintContours(g, cells, slices, bbox, stepM, scale = FIELD_PX_PER_M, upTo = slices.length - 1) {   // контуры секунд 1..upTo: пути проходят через контур каждой из этих секунд
  slices.slice(0, upTo + 1).forEach(slice => paintContour(g, cells, slice, bbox, stepM, scale));
}

export function paintExcluded(g, cells, slices, bbox, stepM, scale = FIELD_PX_PER_M, upTo = slices.length - 1) {   // контуры секунд 1..upTo без законных клеток: затемнены
  slices.slice(0, upTo + 1).forEach(slice => {
    const legal = new Set(cellsAbove(slice.gamma || [], LEGAL_MIN_SHARE));
    const style = rgba(EXCLUDED_STYLE.rgb, EXCLUDED_STYLE.alpha);
    for (const i of contourCells(slice)) if (!legal.has(i)) fillCell(g, cells[i], bbox, stepM, scale, style);
  });
}

export function paintLegal(g, cells, slices, bbox, stepM, scale = FIELD_PX_PER_M, upTo = slices.length - 1) {     // контуры секунд 1..upTo на законных клетках: обычным цветом вероятности кольца
  slices.slice(0, upTo + 1).forEach(slice => {
    const legal = new Set(cellsAbove(slice.gamma || [], LEGAL_MIN_SHARE)), ring = slice.parts?.rings || [], top = Math.max(0, ...ring);
    for (const i of contourCells(slice)) if (legal.has(i)) fillCell(g, cells[i], bbox, stepM, scale, rgba(probabilityRgb(ring[i] / top), lerp(ZONE_ALPHA, ring[i] / top)));
  });
}

export function paintPaths(g, cells, paths, bbox, scale = FIELD_PX_PER_M, shown = PATHS_SHOWN) {   // подходящие пути окна: ломаная по центрам клеток срезов, концы точками
  g.strokeStyle = rgba(PATH_STYLE.rgb, PATH_STYLE.alpha);
  g.lineWidth = PATH_STYLE.width;
  for (const path of paths.slice(0, shown)) {
    g.beginPath();
    path.forEach((cell, k) => { const [x, y] = px(cells[cell], bbox, scale); k ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  }
  g.fillStyle = rgba(PATH_STYLE.rgb, 0.4);
  for (const path of paths) {
    const [x, y] = px(cells[path[path.length - 1]], bbox, scale);
    g.beginPath(); g.arc(x, y, END_RADIUS_M * scale, 0, 2 * Math.PI); g.fill();
  }
}

export function paintPoint(g, point, previous, bbox, scale = FIELD_PX_PER_M) {            // итоговая точка: заливка с тёмной каймой; прошлая точка: полое кольцо
  if (previous) {
    const [x, y] = px([previous.x, previous.z], bbox, scale);
    g.strokeStyle = 'rgba(255,255,255,0.9)'; g.lineWidth = OUTLINE_PX; g.beginPath(); g.arc(x, y, PREVIOUS_RADIUS_M * scale, 0, 2 * Math.PI); g.stroke();
  }
  if (point) {
    const [x, y] = px([point.x, point.z], bbox, scale);
    g.fillStyle = 'rgba(255,255,255,1)'; g.strokeStyle = 'rgba(10,14,24,0.95)'; g.lineWidth = OUTLINE_PX;
    g.beginPath(); g.arc(x, y, POINT_RADIUS_M * scale, 0, 2 * Math.PI); g.fill(); g.stroke();
  }
}

export function paintAscent(g, point, ascent, bbox, scale = FIELD_PX_PER_M) {     // стрелка от точки туда, где ожидаемый уровень растёт: к роутеру; нет точки, стрелки или крутизна нуль - ничего
  if (!point || !ascent || !(ascent.db_per_m > 0)) return;
  const [x0, y0] = px([point.x, point.z], bbox, scale), [x1, y1] = px([point.x + ascent.dx * ASCENT_LENGTH_M, point.z + ascent.dz * ASCENT_LENGTH_M], bbox, scale);
  const angle = Math.atan2(y1 - y0, x1 - x0), head = ASCENT_HEAD_M * scale;
  g.strokeStyle = rgba(ASCENT_STYLE.rgb, ASCENT_STYLE.alpha); g.lineWidth = ASCENT_STYLE.width;
  g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke();
  g.beginPath(); g.moveTo(x1, y1);
  g.lineTo(x1 - head * Math.cos(angle - Math.PI / 6), y1 - head * Math.sin(angle - Math.PI / 6));
  g.moveTo(x1, y1);
  g.lineTo(x1 - head * Math.cos(angle + Math.PI / 6), y1 - head * Math.sin(angle + Math.PI / 6));
  g.stroke();
}

export function describeFlow(device) {          // строка под слоем: устройство, диапазон, лучшие места, точка
  if (!device) return '';
  const places = (device.places || []).slice(0, 4).map(p => `${p.place} ${Math.round(p.p * 100)}%`).join(', ');
  const point = device.point ? `, точка (${device.point.x}; ${device.point.z})` : '';
  const who = device.iface === 'person' ? `человек ${device.name}, по телефону` : `${(device.name || device.id).split(/[-_ ]/)[0]}, ${device.iface === 'phy0-ap0' ? '2,4' : '5'} ГГц`;
  return `${who}${point}${places ? `: ${places}` : ''}`;
}

export function cellAt(cells, x, z, stepM) {    // номер клетки под точкой пола: ближайшая в пределах половины диагонали клетки; нет - -1 (стена, мебель, снаружи)
  let best = -1, bestD = (stepM * Math.SQRT2) / 2;
  cells.forEach((c, i) => { const d = Math.hypot(c[0] - x, c[1] - z); if (d <= bestD) { best = i; bestD = d; } });
  return best;
}

export function legendGradient() {              // полоса легенды: шкала цвета вероятности от малой к большой
  return `linear-gradient(90deg, ${PROBABILITY_RAMP.map(([stop, rgb]) => `rgb(${rgb[0]},${rgb[1]},${rgb[2]}) ${Math.round(stop * 100)}%`).join(', ')})`;
}

export function legendText(values) {            // подпись легенды: абсолютное значение у красного края
  return `0% ... ${(Math.max(0, ...values) * 100).toFixed(1)}%`;
}

const percent = value => `${(value * 100).toFixed(1)}%`;

export function describeGrowth(slices, upTo) {   // строки роста по секундам 1..upTo: пик клетки, доля массы у точки секунды, площадь 90% массы; текущая секунда помечена стрелкой
  return slices.slice(0, upTo).map((slice, k) => {
    const g = slice.growth, mark = k === upTo - 1 ? '>' : ' ';
    return g ? `${mark} ${k + 1} с: пик ${percent(g.peak)}, у точки ${percent(g.mass_near)}, площадь 90% ${g.area_m2.toFixed(1)} м2` : `${mark} ${k + 1} с: нет данных`;
  });
}

export function secondSlices(slices, upTo) {    // срезы за первые upTo секунд для рисования: законные клетки по этим секундам (upto), а не по всему окну
  const at = Math.min(slices.length, Math.max(1, upTo)) - 1;
  return slices.slice(0, at + 1).map((slice, j) => ({ parts: slice.parts, gamma: slices[at].upto?.[j] || [] }));
}

export function paintTrail(g, points, bbox, scale = FIELD_PX_PER_M, upTo = points.length - 1, before = null) {     // точки секунд окна: цепочка с номерами, последняя крупнее и залита; before - позиция до окна, секунда 0
  const shown = points.slice(0, upTo + 1);
  g.strokeStyle = rgba(TRAIL_RGB, TRAIL_LINE_ALPHA); g.lineWidth = 1; g.beginPath();
  let started = false;
  for (const p of before ? [before, ...shown] : shown) {
    if (!p) continue;
    const [x, y] = px([p.x, p.z], bbox, scale);
    started ? g.lineTo(x, y) : g.moveTo(x, y); started = true;
  }
  g.stroke();
  g.font = `${TRAIL_FONT_PX}px sans-serif`;
  if (before) {
    const [x, y] = px([before.x, before.z], bbox, scale);
    g.beginPath(); g.arc(x, y, PREVIOUS_RADIUS_M * scale, 0, 2 * Math.PI);
    g.strokeStyle = 'rgba(255,255,255,0.9)'; g.lineWidth = OUTLINE_PX; g.stroke();
    g.fillStyle = 'rgba(255,255,255,0.95)'; g.fillText('0', x + PREVIOUS_RADIUS_M * scale + 2, y - 3);
  }
  shown.forEach((p, k) => {
    if (!p) return;
    const [x, y] = px([p.x, p.z], bbox, scale), current = k === upTo;
    g.beginPath(); g.arc(x, y, (current ? POINT_RADIUS_M : PREVIOUS_RADIUS_M) * scale, 0, 2 * Math.PI);
    if (current) { g.fillStyle = 'rgba(255,255,255,1)'; g.strokeStyle = 'rgba(10,14,24,0.95)'; g.lineWidth = OUTLINE_PX; g.fill(); g.stroke(); }
    else { g.strokeStyle = 'rgba(255,255,255,0.9)'; g.lineWidth = OUTLINE_PX; g.stroke(); }
    g.fillStyle = 'rgba(255,255,255,0.95)';
    g.fillText(String(k + 1), x + (current ? POINT_RADIUS_M : PREVIOUS_RADIUS_M) * scale + 2, y - 3);
  });
}
