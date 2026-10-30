// Слои вероятности на карте радара (SAM-118): поле телефона (оценка мест устройства по уровню сигнала) и поле человека (свидетельства о его месте).
// Чистый модуль без THREE и DOM: строит пятна из строк кадра и рисует их в контекст 2d, проверяется в node.
export const FIELD_PX_PER_M = 40;                 // разрешение холста слоя
export const BLOB_RADIUS_M = 1.3;                 // радиус пятна одного места: оценка места это не точка
export const BLOB_MAX_ALPHA = 0.85;               // непрозрачность в центре пятна при вероятности единица
export const PHONE_RGB = [127, 227, 255];         // голубой: устройство
export const PERSON_RGB = [255, 150, 235];        // розовый: человек
export const PROBABILITY_RAMP = [[0, [0, 0, 0]], [0.12, [40, 90, 255]], [0.4, [40, 220, 130]], [0.7, [255, 220, 60]], [1, [255, 60, 40]]];   // вероятность от нуля к наибольшей: чёрный (ноль), синий, зелёный, жёлтый, красный
export const FLOOR_MASK_PX_PER_M = 20;            // разрешение маски пола: край стены с точностью 5 см, этого хватает пятну радиусом больше метра
export const IFACE_BAND = { 'phy0-ap0': '2,4 ГГц', 'phy1-ap0': '5 ГГц' };

export function phoneBlobs(row) {                 // место устройства с вероятностью: field приходит с сервера целиком
  return (row?.field || []).filter(f => f.x != null && f.z != null).map(f => ({ x: f.x, z: f.z, p: f.p, label: f.place }));
}

export function personBlobs(identity) {           // свидетельство с местом: вес на сейчас; слабее порога не рисуется
  return (identity?.evidence || []).filter(e => e.x != null && e.z != null && e.w > 0.02).map(e => ({ x: e.x, z: e.z, p: e.w, label: e.source }));
}

export function probabilityRgb(p) {              // цвет вероятности 0..1 по шкале PROBABILITY_RAMP: чем вероятнее, тем краснее
  const t = Math.min(1, Math.max(0, p)), a = PROBABILITY_RAMP.findLast(([stop]) => stop <= t) ?? PROBABILITY_RAMP[0], b = PROBABILITY_RAMP.find(([stop]) => stop > t) ?? a, k = b === a ? 0 : (t - a[0]) / (b[0] - a[0]);
  return a[1].map((v, i) => Math.round(v + (b[1][i] - v) * k));
}

export function floorMask(onFloor, bbox, scale = FLOOR_MASK_PX_PER_M) {   // маска пола для обрезки слоя: непрозрачный пиксель там, где пол плана; без неё пятно у стены выходит за стену
  const w = Math.ceil(bbox.w * scale), h = Math.ceil(bbox.d * scale), data = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (onFloor(bbox.minX + (x + 0.5) / scale, bbox.minZ + (y + 0.5) / scale)) data[(y * w + x) * 4 + 3] = 255;
  return { w, h, data };
}

// bbox {minX, minZ}; пятно - радиальный градиент от rgba(центр) до нуля, прозрачность центра пропорциональна вероятности.
export function paintBlobs(g, blobs, rgb, bbox, scale = FIELD_PX_PER_M, radiusM = BLOB_RADIUS_M) {    // rgb - цвет или функция вероятности в цвет
  for (const b of blobs) {
    const color = typeof rgb === 'function' ? rgb(b.p) : rgb;
    const cx = (b.x - bbox.minX) * scale, cy = (b.z - bbox.minZ) * scale, r = radiusM * scale;
    const gradient = g.createRadialGradient(cx, cy, 0, cx, cy, r);
    gradient.addColorStop(0, `rgba(${color[0]},${color[1]},${color[2]},${(BLOB_MAX_ALPHA * Math.min(1, b.p)).toFixed(3)})`);
    gradient.addColorStop(1, `rgba(${color[0]},${color[1]},${color[2]},0)`);
    g.fillStyle = gradient;
    g.fillRect(cx - r, cy - r, 2 * r, 2 * r);
  }
}

const percent = p => `${Math.round(p * 100)}%`;

export function describePhone(row) {
  if (!row) return '';
  const places = phoneBlobs(row).slice(0, 4).map(b => `${b.label} ${percent(b.p)}`).join(', ');
  return `${(row.name || '').split(/[-_ ]/)[0]}, ${IFACE_BAND[row.iface] || row.iface}, ${row.signal_db} дБ${row.at ? `, выбрано: ${row.at.place}` : ''}${places ? `: ${places}` : ''}`;
}

export function describePerson(identity) {
  if (!identity) return '';
  const parts = personBlobs(identity).slice(0, 4).map(b => `${b.label} ${percent(b.p)}`).join(', ');
  return `уверенность ${percent(identity.confidence)}${parts ? `; свидетельства: ${parts}` : ''}`;
}
