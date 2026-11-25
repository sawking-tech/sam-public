// Пол плана по комнатам (SAM-118): паркет, линолеум, плитка. Тип пола лежит в комнате плана (`floor_kind`), рисунок строится от мировых координат,
// поэтому швы паркета и плитки не ломаются на границе кусков пола.
// Чистый модуль без THREE и DOM: контекст рисования приходит параметром, проверяется в node с записывающим контекстом.
export const DEFAULT_FLOOR_KIND = 'linoleum';     // пол вне прямоугольников комнат - проходы между ними: у них этот тип
export const FLOOR_KINDS = {        // пастельные тона: пол светлее, чтобы дом не выглядел мрачным, фигуры на нём остаются заметными
  parquet: { base: '#b89c7b', joint: 'rgba(90,64,38,0.35)', shade: 0.07, plank: 0.14, len: 0.7 },    // паркет: короткие доски рядами со сдвигом швов на половину
  planks: { base: '#c4ab8c', joint: 'rgba(90,64,38,0.30)', shade: 0.06, plank: 0.2, len: 2.2 },      // длинные широкие доски, шов через два с лишним метра
  linoleum: { base: '#cdc3aa', cubeA: '#c8bda4', cubeB: '#d3c9b1', speckle: 0.06 },                   // кубики и мелкая крапинка
  checker: { base: '#cfcfc4', checker: ['#b9cde0', '#e8dec6'], tile: 0.3 },                           // шахматка плитки двух цветов
  tile_small: { base: '#d9b8ae', joint: 'rgba(120,70,60,0.5)', shade: 0.05, tile: 0.1 },              // мелкая плитка с тёплым тоном
};
export const DOT_STEP_M = 0.045;          // шаг точек пола: стены плана из точек, пол тоже из мелких точек, между ними пусто
export const GLOW_ALPHA = 0.22;           // слабая подсветка пола между точками цветом типа: тьма сцены не просвечивает чёрным
export const DOT_RADIUS_M = 0.007;       // радиус круглой точки пола, метры: 7 мм, на рисунке 160 пикселей на метр это радиус около одного пикселя

export const CUBE_M = 0.12;               // кубик линолеума
export const SPECKLE_PER_PX2 = 1 / 90;    // плотность крапинки линолеума: точек на пиксель рисунка
export const SPECKLE_CELL_PX = 30;        // мировая клетка крапинки, пиксели рисунка: 10 точек на клетку при этой плотности
const SEED = 20261010;

// Тип пола в точке: самая маленькая из комнат, чей прямоугольник её содержит; вне комнат - тип по умолчанию.
export function floorKindAt(rooms, x, z) {
  const inside = rooms.filter(r => x >= r.x && x <= r.x + r.w && z >= r.z && z <= r.z + r.d);
  if (!inside.length) return DEFAULT_FLOOR_KIND;
  const room = inside.reduce((a, b) => (a.w * a.d <= b.w * b.d ? a : b));
  return FLOOR_KINDS[room.floor_kind] ? room.floor_kind : DEFAULT_FLOOR_KIND;
}

// Клетки пола по типам: проходимые клетки плана плюс все клетки внутри комнат (под мебелью пол тоже есть, но карта проходимости его не держит);
// строки режутся там, где тип меняется; результат {тип: [[строка, от, до), ...]}.
export function floorCellsByKind(floor, rooms) {
  const rows = new Map();
  const mark = (r, a, b) => { const row = rows.get(r) || new Uint8Array(floor.w + 1); row.fill(1, a, Math.min(b, floor.w)); rows.set(r, row); };
  for (const [r, a, b] of floor.runs) mark(r, a, b);
  for (const room of rooms) {
    const r0 = Math.max(0, Math.ceil((room.z - floor.z0) / floor.cell - 0.5)), r1 = Math.min(floor.h - 1, Math.floor((room.z + room.d - floor.z0) / floor.cell - 0.5));
    const a0 = Math.max(0, Math.ceil((room.x - floor.x0) / floor.cell - 0.5)), a1 = Math.min(floor.w - 1, Math.floor((room.x + room.w - floor.x0) / floor.cell - 0.5));
    for (let r = r0; r <= r1; r++) if (a1 >= a0) mark(r, a0, a1 + 1);
  }
  const out = {};
  for (const [r, row] of [...rows].sort((p, q) => p[0] - q[0])) {
    const z = floor.z0 + (r + 0.5) * floor.cell;
    let start = -1, kind = null;
    for (let i = 0; i <= floor.w; i++) {
      const next = i < floor.w && row[i] ? floorKindAt(rooms, floor.x0 + (i + 0.5) * floor.cell, z) : null;
      if (next !== kind) { if (kind !== null) (out[kind] ||= []).push([r, start, i]); start = i; kind = next; }
    }
  }
  return out;
}

function hash(i, j) {                      // дешёвый детерминированный шум 0..1 по двум целым
  let h = (i * 374761393 + j * 668265263 + SEED) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

// Рисунок типа на всё полотно: w, h - пиксели, s - пикселей на метр, (x0, z0) - мировая точка левого верхнего угла полотна.
export function paintFloor(g, kind, w, h, s, x0, z0) {
  const spec = FLOOR_KINDS[kind] || FLOOR_KINDS[DEFAULT_FLOOR_KIND];
  g.fillStyle = spec.base; g.fillRect(0, 0, w, h);
  if (spec.plank) {                                                                  // доски рядами: ширина и длина доски у типа, швы каждого ряда со сдвигом на половину
    const plank = spec.plank, len = spec.len, rows = Math.ceil(h / (plank * s)) + 1, row0 = Math.floor(z0 / plank);
    for (let k = 0; k < rows; k++) {
      const rowIdx = row0 + k, y = (rowIdx * plank - z0) * s, shift = (rowIdx % 2 ? len / 2 : 0);
      const cols = Math.ceil(w / (len * s)) + 2, col0 = Math.floor((x0 - shift) / len);
      for (let c = 0; c < cols; c++) {
        const colIdx = col0 + c, x = (colIdx * len + shift - x0) * s, tone = (hash(colIdx, rowIdx) - 0.5) * 2 * spec.shade;
        g.fillStyle = tone > 0 ? `rgba(255,240,215,${tone})` : `rgba(60,40,20,${-tone})`; g.fillRect(x, y, len * s, plank * s);
        g.fillStyle = spec.joint; g.fillRect(x, y, 1, plank * s); g.fillRect(x, y, len * s, 1);
      }
    }
  } else if (kind === 'linoleum') {
    const n = Math.ceil(CUBE_M * s), cols = Math.ceil(w / n) + 1, rows = Math.ceil(h / n) + 1;
    const ci = Math.floor(x0 / CUBE_M), cj = Math.floor(z0 / CUBE_M), ox = (ci * CUBE_M - x0) * s, oy = (cj * CUBE_M - z0) * s;
    for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
      g.fillStyle = (i + ci + j + cj) % 2 ? spec.cubeA : spec.cubeB; g.fillRect(ox + i * CUBE_M * s, oy + j * CUBE_M * s, CUBE_M * s + 1, CUBE_M * s + 1);
    }
    // крапинка по мировым клеткам SPECKLE_CELL_PX: у соседних кусков пола и проёмов узор один, а не повтор от угла куска
    const cell = SPECKLE_CELL_PX, per = Math.round(cell * cell * SPECKLE_PER_PX2), wx = x0 * s, wz = z0 * s;
    for (let bj = Math.floor(wz / cell); bj * cell < wz + h; bj++) for (let bi = Math.floor(wx / cell); bi * cell < wx + w; bi++) {
      for (let d = 0; d < per; d++) {
        const k = d * 3, px = bi * cell + hash(bi * 7919 + k, bj) * cell - wx, py = bj * cell + hash(bi * 7919 + k + 1, bj) * cell - wz;
        g.fillStyle = hash(bi * 7919 + k + 2, bj) > 0.5 ? `rgba(0,0,0,${spec.speckle})` : `rgba(255,248,230,${spec.speckle})`; g.fillRect(px, py, 2, 1);
      }
    }
  } else {                                                                          // плитка по размеру типа: шахматка двух цветов или швы с лёгким разбросом тона
    const size = spec.tile, cols = Math.ceil(w / (size * s)) + 1, rows = Math.ceil(h / (size * s)) + 1, ci = Math.floor(x0 / size), cj = Math.floor(z0 / size);
    for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
      const x = ((ci + i) * size - x0) * s, y = ((cj + j) * size - z0) * s;
      if (spec.checker) { g.fillStyle = spec.checker[(ci + i + cj + j) & 1]; g.fillRect(x, y, size * s + 1, size * s + 1); continue; }
      const tone = (hash(ci + i, cj + j) - 0.5) * 2 * spec.shade;
      g.fillStyle = tone > 0 ? `rgba(255,255,245,${tone})` : `rgba(60,50,30,${-tone})`; g.fillRect(x, y, size * s, size * s);
      g.fillStyle = spec.joint; g.fillRect(x, y, size * s, 1); g.fillRect(x, y, 1, size * s);
    }
  }
}

// Кусок пола целиком (рисунок, точки, свечение) только в окне box {x0, y0, x1, y1} пикселей полотна: начало переносится в угол окна, мировая точка
// сдвигается на столько же, поэтому швы и точки ложатся там же, где при рисовании всего полотна. Рисовать всё полотно ради куска под обрезкой -
// 6,7 с на большом плане (замер в Chrome: каждый тип пола и каждый проём проходили полотно целиком).
export function paintPatch(g, kind, box, s, x0, z0) {
  const bx = Math.floor(box.x0), by = Math.floor(box.y0), w = Math.ceil(box.x1) - bx, h = Math.ceil(box.y1) - by;
  if (w <= 0 || h <= 0) return;
  const px = x0 + bx / s, pz = z0 + by / s;
  if (typeof OffscreenCanvas === 'function' && typeof g.drawImage === 'function') {
    // браузер: кусок рисуется без обрезки на своём холсте и кладётся одним drawImage. Тысячи прямоугольников рисунка под
    // обрезкой из клеток Chrome растеризовал 3-4,6 с (замер)
    const off = new OffscreenCanvas(w, h), o = off.getContext('2d');
    paintFloor(o, kind, w, h, s, px, pz); stipple(o, w, h, s, px, pz); glow(o, kind, w, h);
    g.clearRect(bx, by, w, h); g.drawImage(off, bx, by);     // под обрезкой: проём стирает стену до пола, как и прежде
    return;
  }
  g.save(); g.translate(bx, by);
  paintFloor(g, kind, w, h, s, px, pz); stipple(g, w, h, s, px, pz); glow(g, kind, w, h);
  g.restore();
}

// Сетка точек паттерном: плитка с одной точкой, растянутая на шаг сетки, начало - узел сетки в мире. Контур из дуг Chrome растеризует
// на GPU отложенно, при загрузке текстуры плана: 5,3 с главного потока в первом кадре (замер: getProgramInfoLog ждал холст).
// null - контекст без паттернов (проверка в node): точки рисуются контуром.
export const DOT_TILE_PX = 64;            // плитка паттерна: точка в ней крупная и круглая, на полотне сжимается до шага сетки
function dotPattern(g, step, r, ox, oy) {
  if (typeof g.createPattern !== 'function' || typeof OffscreenCanvas !== 'function' || typeof DOMMatrix !== 'function') return null;
  const tile = new OffscreenCanvas(DOT_TILE_PX, DOT_TILE_PX), t = tile.getContext('2d'), k = step / DOT_TILE_PX;
  t.fillStyle = '#000'; t.beginPath(); t.arc(DOT_TILE_PX / 2, DOT_TILE_PX / 2, r / k, 0, 2 * Math.PI); t.fill();
  const pattern = g.createPattern(tile, 'repeat');
  pattern.setTransform(new DOMMatrix([k, 0, 0, k, ox - step / 2, oy - step / 2]));
  return pattern;
}

// Пол точками: от уже нарисованного рисунка остаются только мелкие круглые точки сетки, между ними пол прозрачный; цвет точки - цвет рисунка под ней.
// Режим destination-in оставляет рисунок только под одной фигурой, поэтому все точки собираются в один контур и заливаются один раз.
// g - контекст 2d; действует в пределах текущей обрезки, соседние куски пола не затрагиваются. bounds {x0, y0, x1, y1} в пикселях - точки только в этом окне
// (проём двери: нет смысла считать сетку всего плана).
export function stipple(g, w, h, s, x0, z0, bounds = null) {
  const step = DOT_STEP_M * s, r = DOT_RADIUS_M * s, ox = (Math.ceil(x0 / DOT_STEP_M) * DOT_STEP_M - x0) * s, oy = (Math.ceil(z0 / DOT_STEP_M) * DOT_STEP_M - z0) * s;
  const xa = bounds ? Math.max(0, bounds.x0) : 0, xb = bounds ? Math.min(w, bounds.x1) : w, ya = bounds ? Math.max(0, bounds.y0) : 0, yb = bounds ? Math.min(h, bounds.y1) : h;
  const pattern = dotPattern(g, step, r, ox, oy);
  if (pattern) {             // браузер: одна заливка паттерном вместо контура из десятков тысяч дуг
    g.globalCompositeOperation = 'destination-in'; g.fillStyle = pattern; g.fillRect(xa, ya, xb - xa, yb - ya);
    g.globalCompositeOperation = 'source-over';
    return;
  }
  g.globalCompositeOperation = 'destination-in'; g.fillStyle = '#000'; g.beginPath();
  for (let y = oy + Math.ceil((ya - oy) / step) * step; y < yb; y += step) {
    for (let x = ox + Math.ceil((xa - ox) / step) * step; x < xb; x += step) { g.moveTo(x + r, y); g.arc(x, y, r, 0, 2 * Math.PI); }
  }
  g.fill(); g.globalCompositeOperation = 'source-over';
}

// Свечение поля: под точками слабая заливка цветом типа пола; destination-over кладёт её под уже нарисованное, в пределах обрезки.
export function glow(g, kind, w, h) {
  const base = (FLOOR_KINDS[kind] || FLOOR_KINDS[DEFAULT_FLOOR_KIND]).base, v = parseInt(base.slice(1), 16);
  g.globalCompositeOperation = 'destination-over'; g.fillStyle = `rgba(${v >> 16},${(v >> 8) & 255},${v & 255},${GLOW_ALPHA})`; g.fillRect(0, 0, w, h);
  g.globalCompositeOperation = 'source-over';
}
