// SAM-241: стык модели pending.js с лентой. Без импортов DOM: узел строки приходит снаружи.
// Флаг выключен - begin() даёт null, settle()/fail() дают false, таймеры не создаются, старое поведение не тронуто.
// Потерянное подтверждение: метка «в очереди» не висит вечно - по таймауту или ошибке отправки
// она заменяется на строку «не подтверждено» (её строит вызывающий, makeFailed).
import {createPending, pendingEnabled} from './pending.js';

export const PENDING_TIMEOUT_MS = 5 * 60 * 1000;   // ход может думать долго, сообщение всё это время в очереди

export function createPendingFeed({enabled = pendingEnabled, now = Date.now, timeoutMs = PENDING_TIMEOUT_MS,
                                   setTimer = (fn, ms) => { const t = setTimeout(fn, ms); t?.unref?.(); return t; },   // unref: таймер не держит процесс (тесты node)
                                    clearTimer = id => clearTimeout(id)} = {}) {
  const model = createPending();
  const slots = new Map();   // msg_id -> {el, timer, makeFailed}
  let seq = 0;

  const drop = id => {
    const s = slots.get(id);
    if (s && s.timer != null) clearTimer(s.timer);
    slots.delete(id);
    model.forget(id);
    return s;
  };

  function fail(id) {
    if (!enabled() || !slots.has(id)) return false;
    const s = drop(id);
    if (!s.el || s.el.isConnected === false || !s.makeFailed) return false;
    s.el.replaceWith(s.makeFailed());
    return true;
  }

  return {
    // отправка: вернёт клиентский msg_id или null (флаг выключен); makeFailed() - строка «не подтверждено»
    begin(session, makeFailed) {
      if (!enabled()) return null;
      const id = `c-${now()}-${++seq}`;
      if (!model.add(id, session, now())) return null;
      const slot = {el: null, timer: null, makeFailed};
      slot.timer = setTimer(() => { slot.timer = null; fail(id); }, timeoutMs);
      slots.set(id, slot);
      return id;
    },
    attach(id, el) { const s = id && slots.get(id); if (s && el) s.el = el; },
    fail,
    // ход принял сообщение (событие user_message): строка «в очереди» заменяется на настоящую
    // (текст с сервера, с маской секрета) на том же месте - порядок по времени отправки.
    // true - замена сделана, вызывающий строку второй раз не рисует.
    settle(ev, realEl) {
      if (!enabled() || !ev || ev.replay) return false;
      const id = (ev.client_id && model.settle(ev.client_id)) ? ev.client_id : model.settleOldest(ev.session);
      if (!id) return false;
      const s = drop(id);
      if (!s || !s.el || s.el.isConnected === false) return false;
      s.el.replaceWith(realEl);
      return true;
    },
    queuedCount: () => model.queuedCount(),
  };
}

export const pendingFeed = createPendingFeed();
