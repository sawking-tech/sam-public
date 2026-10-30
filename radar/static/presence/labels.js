// Дубли подписей на странице радара (SAM-105): одно название на одном месте показывается один раз.
// Комната «кухня» и место плана «кухня», предмет «Комод» и зона «комод» - одна и та же вещь с двумя подписями.
// Чистый модуль без THREE и DOM: проверяется в node.
export const DUP_RADIUS_M = 3.0;                    // одинаковые названия ближе этого - дубль; три разных стола в разных комнатах остаются
export const PRIORITY = { place: 0, object: 1, room: 2, zone: 3 };     // какую из дублей оставить: зона (её правит человек), потом комната, предмет, место плана

export const labelKey = text => String(text || '').replace(/\s*\(примерно\)\s*$/i, '').trim().toLowerCase().replace(/\s+/g, ' ');

// items: [{key, prio, x, z}] -> Set индексов, которые прячутся (есть такая же подпись важнее или раньше, не дальше DUP_RADIUS_M).
export function hiddenLabels(items, radius = DUP_RADIUS_M) {
  const order = items.map((_, i) => i).sort((a, b) => items[b].prio - items[a].prio || a - b), kept = [], hidden = new Set();
  for (const i of order) {
    const it = items[i];
    if (it.key && kept.some(j => items[j].key === it.key && Math.hypot(items[j].x - it.x, items[j].z - it.z) <= radius)) hidden.add(i);
    else kept.push(i);
  }
  return hidden;
}
