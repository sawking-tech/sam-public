// Где рисовать устройство (SAM-118): в точке, которую дал поток положения (итог ста бросков последней области, at); нет её - в лучшем месте оценки по уровню (where),
// иначе у хозяина. Координата записывается как есть, на рендере страница прижимает её к ближайшей подходящей поверхности. Чистый модуль без THREE и DOM: проверяется в node.
export const CARRIED_HEIGHT_M = 0.9;          // высота точки до прижатия к поверхности под ней
export const CARRIER_MIN_CONF = 0.05;         // человек с меньшей уверенностью на карте не нарисован: телефон к нему не привязывается
export const OWN_ESTIMATE_MIN_P = 0.4;        // оценка места устройства слабее этой в метку не идёт

// d - строка устройства {at: {x, z}, where: [{place, p, x, z}]}; carrier - призрак хозяина {cx, cz, conf} или undefined. Результат {x, y, z} или null.
export function deviceTarget(d, carrier) {
  if (d.at && d.at.x != null && d.at.z != null) return { x: d.at.x, y: CARRIED_HEIGHT_M, z: d.at.z };
  const own = (d.where || []).find(w => w.x != null && w.z != null && w.p >= OWN_ESTIMATE_MIN_P);
  if (own) return { x: own.x, y: CARRIED_HEIGHT_M, z: own.z };
  if (carrier && carrier.conf > CARRIER_MIN_CONF) return { x: carrier.cx, y: CARRIED_HEIGHT_M, z: carrier.cz };
  return null;
}

export const PUSH_OUT_M = 0.6;                // телефон в фигуре человека не рисуется: он рядом, у тела, а не внутри точек силуэта

// Точка устройства, вытолкнутая из фигур людей: ближе radius к центру человека - сдвигается на radius по линии от центра (в одну точку - в сторону +x).
export function pushOut(point, people, radius = PUSH_OUT_M) {
  let { x, z } = point;
  for (const p of people) {
    if (!(p.conf > CARRIER_MIN_CONF)) continue;
    const dx = x - p.cx, dz = z - p.cz, d = Math.hypot(dx, dz);
    if (d >= radius) continue;
    const [ux, uz] = d > 1e-6 ? [dx / d, dz / d] : [1, 0];
    x = p.cx + ux * radius; z = p.cz + uz * radius;
  }
  return { x, z };
}

// Подпись устройства: чьё оно, имя, уровень; вышедшее из сети - пометка.
export function deviceLabel(name, signalDb, ownerName, present, leftRecently) {
  const who = ownerName ? `${ownerName}: ` : '';
  const short = (name || '').split(/[-_ ]/)[0] || name;
  if (present) return `${who}${short} · ${signalDb} дБ`;
  return leftRecently ? `${who}${short} · вышел из сети` : '';
}
