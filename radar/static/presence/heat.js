// Карта посещений на странице радара (SAM-105): красный - где чаще всего ходили, зелёный - середина, синий - редко и остыло.
// Клетки по 0,25 м рисовались квадратами, шум отсекался порогом и срезал дорожки ходьбы (клетка прохода копит 0,3-0,5 с); теперь шум отличается
// от дорожки связностью, а картинка - непрерывное поле с размытием, градиентные зоны вместо квадратов.
// Чистый модуль без THREE и DOM: проверяется в node.
export const HEAT_NOISE_S = 0.2;           // клетка с меньшим накопленным временем - шум и случайный проход, не рисуется
export const HEAT_SOLID_S = 1.0;           // клетка с таким временем рисуется и без соседей: там стояли
export const HEAT_TRAIL_NEIGHBOURS = 2;    // слабая клетка остаётся, только если у неё столько соседей не слабее шума: это дорожка, а не мигание
export const HEAT_RANK_MIN = 200;          // меньше стольких нарисованных точек относительной шкале верить нельзя: запасная шкала по секундам
export const HEAT_REF_MIN_S = 20;          // запасная шкала: на малых данных оранжевого не выдумываем, оранжевая клетка - 20 секунд и больше
export const HEAT_UP = 5;                  // во сколько раз картинка мельче клетки
export const HEAT_BLUR_CELLS = 0.65;       // размытие (сигма) в клетках, около 0,16 м: соседние клетки сливаются в непрерывную полосу, граница клеток не видна, зоны небольшие, тропинка шириной в клетку читается
export const HEAT_FIELD_FLOOR_S = 0.02;    // в размытом поле порог шума ниже: дорожка в одну клетку после размытия слабая, а шум уже отсечён по клеткам (trailCells)
export const HEAT_FADE_T = 0.15;           // точки с долей посещаемости ниже этой плавно уходят в прозрачность: у зоны нет резкого края

// cells: [[i, k, секунды]] -> те же клетки без шума: жирные остаются всегда, слабые - если у них есть соседи-дорожка.
export function trailCells(cells) {
  const at = new Map(cells.map(([i, k, v]) => [i + ',' + k, v]));
  const near = (i, k) => { let n = 0; for (let di = -1; di <= 1; di++) for (let dk = -1; dk <= 1; dk++) if ((di || dk) && (at.get((i + di) + ',' + (k + dk)) ?? 0) >= HEAT_NOISE_S) n++; return n; };
  return cells.filter(([i, k, v]) => v >= HEAT_SOLID_S || (v >= HEAT_NOISE_S && near(i, k) >= HEAT_TRAIL_NEIGHBOURS));
}

// Рекордные клетки (телефон на кровати копит часы) режутся по этому процентилю до размытия: иначе одна клетка заливает пятном метр вокруг себя.
export const HEAT_CAP_PERCENTILE = 0.97;
export function capCells(cells) {
  const vals = cells.map(c => c[2]).sort((a, b) => a - b);
  if (vals.length < 20) return cells;                                           // мало клеток: процентиль ничего не значит
  const cap = vals[Math.min(vals.length - 1, Math.floor(vals.length * HEAT_CAP_PERCENTILE))];
  return cells.map(([i, k, v]) => [i, k, Math.min(v, cap)]);
}

// values - секунды по точкам карты -> функция v => t в 0..1, null для шума. Цвет относительный: t - доля нарисованных точек, где времени не больше,
// чем здесь: верхние 10% самых посещаемых мест оранжевые, середина зелёная, остальное синее, и рекордная клетка шкалу не съедает. На малых данных - по секундам.
export function heatScale(values, noise = HEAT_NOISE_S) {
  const kept = Float64Array.from(values.filter(v => v >= noise)).sort();
  if (kept.length < HEAT_RANK_MIN) { const top = Math.log1p(HEAT_REF_MIN_S); return v => (v < noise ? null : Math.min(1, Math.log1p(v) / top)); }
  return v => {
    if (v < noise) return null;
    let lo = 0, hi = kept.length;                                                  // сколько точек не больше v
    while (lo < hi) { const mid = (lo + hi) >> 1; if (kept[mid] <= v) lo = mid + 1; else hi = mid; }
    return lo / kept.length;
  };
}

// Непрерывное поле без сот: каждая клетка кладёт в мелкую сетку (up на клетку) гауссово пятно с центром в своей середине; сумма пятен делится на сумму пятен
// сетки пустых клеток, так что единица измерения остаётся секундами. Заливка блоками с размытием оставляла видимую сетку клеток.
// Возвращает {w, h, values}: values[y * w + x] в секундах, строка y растёт с z; клетка (i, k) - от (i0, k0), размер сетки W x H клеток.
export function heatField(cells, i0, k0, W, H, up = HEAT_UP, sigmaCells = HEAT_BLUR_CELLS) {
  const w = W * up, h = H * up, out = new Float32Array(w * h), sigma = sigmaCells * up, radius = Math.max(1, Math.ceil(sigma * 3));
  let norm = 0;
  const reach = Math.ceil(3 * sigmaCells) + 1;                                                                      // решётка клеток вокруг: достаточно далеко, чтобы сумма была плато
  for (let a = -reach; a <= reach; a++) for (let b = -reach; b <= reach; b++) norm += Math.exp(-((a * up) ** 2 + (b * up) ** 2) / (2 * sigma * sigma));     // сумма пятен решётки клеток: плато поля
  for (const [i, k, v] of cells) {
    const cx = (i - i0 + 0.5) * up, cy = (k - k0 + 0.5) * up;
    for (let y = Math.max(0, Math.floor(cy - radius)); y <= Math.min(h - 1, Math.ceil(cy + radius)); y++) {
      for (let x = Math.max(0, Math.floor(cx - radius)); x <= Math.min(w - 1, Math.ceil(cx + radius)); x++) {
        out[y * w + x] += v * Math.exp(-((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) / (2 * sigma * sigma)) / norm;
      }
    }
  }
  return { w, h, values: out };
}
