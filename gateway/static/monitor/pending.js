// SAM-241: отправленное сообщение видно в ленте сразу с пометкой «в очереди».
// Чистая модель без DOM: порядок по времени отправки, дубль по msg_id, метка снимается, когда ход принял сообщение.
// Текст не хранится: набранное может содержать секрет (SAM-88), показывается только маска от сервера.
// Флаг выключен по умолчанию: localStorage['sam.monitor.pending'] === '1'.

export function pendingEnabled(storage) {
  try { return (storage || globalThis.localStorage).getItem('sam.monitor.pending') === '1'; }
  catch { return false; }
}

export function createPending() {
  const rows = new Map();   // msg_id -> {msg_id, session, sentAt, queued}
  return {
    // отправка: строка появляется сразу; повторный msg_id - не дубль
    add(msgId, session, sentAt) {
      if (!msgId || rows.has(msgId)) return false;
      rows.set(msgId, {msg_id: msgId, session, sentAt, queued: true});
      return true;
    },
    // ход принял сообщение (событие user_message / queue_flushed с msg_id): метка «в очереди» снимается
    settle(msgId) {
      const r = rows.get(msgId);
      if (!r) return false;
      r.queued = false;
      return true;
    },
    // порядок по времени отправки, не по времени доставки; равные - в порядке добавления
    list() {
      return [...rows.values()].sort((a, b) => a.sentAt - b.sentAt);
    },
    // сервер не отдал msg_id подтверждения: принятым считается самое старое в очереди той же сессии; чужая сессия не трогается
    settleOldest(session) {
      const q = [...rows.values()].filter(r => r.queued).sort((a, b) => a.sentAt - b.sentAt);
      const r = q.find(x => x.session === session);
      if (!r) return null;
      r.queued = false;
      return r.msg_id;
    },
    // запись закрыта (подтверждена или провалена): модель не растёт за весь день работы монитора
    forget(msgId) { return rows.delete(msgId); },
    queuedCount() { return [...rows.values()].filter(r => r.queued).length; },
  };
}
