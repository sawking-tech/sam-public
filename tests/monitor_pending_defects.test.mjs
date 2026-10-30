// SAM-241, шаг 3: дефекты — потеря подтверждения, ошибка отправки, рост модели, быстрая серия.
// node --test tests/monitor_pending_defects.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {createPending} from '../gateway/static/monitor/pending.js';
import {createPendingFeed} from '../gateway/static/monitor/pending-feed.js';

const el = name => ({name, isConnected: true, replaceWith(n) { this.replacedBy = n; }});
// управляемые таймеры
function fakeTimers() {
  const t = new Map(); let n = 0;
  return {
    setTimer: (fn, ms) => { t.set(++n, {fn, ms}); return n; },
    clearTimer: id => t.delete(id),
    pending: () => t.size,
    fire: () => { for (const [id, x] of [...t]) { t.delete(id); x.fn(); } },
    lastMs: () => [...t.values()].pop()?.ms,
  };
}

test('потеря подтверждения: по таймауту метка «в очереди» заменяется на ошибку', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => true, ...tm});
  const failed = el('failed');
  const id = f.begin('s', () => failed);
  const p = el('p'); f.attach(id, p);
  assert.equal(tm.pending(), 1);
  tm.fire();
  assert.equal(p.replacedBy, failed);
  assert.equal(f.queuedCount(), 0);
});

test('подтверждение снимает таймер', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => true, ...tm});
  const id = f.begin('s', () => el('failed'));
  f.attach(id, el('p'));
  assert.equal(f.settle({session: 's'}, el('real')), true);
  assert.equal(tm.pending(), 0);
});

test('ошибка отправки (fetch упал): fail() заменяет метку сразу и снимает таймер', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => true, ...tm});
  const failed = el('failed');
  const id = f.begin('s', () => failed);
  const p = el('p'); f.attach(id, p);
  assert.equal(f.fail(id), true);
  assert.equal(p.replacedBy, failed);
  assert.equal(tm.pending(), 0);
  assert.equal(f.fail(id), false);   // второй раз - ничего
});

test('позднее подтверждение после таймаута: строка рисуется обычным путём, метка не висит', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => true, ...tm});
  const id = f.begin('s', () => el('failed'));
  f.attach(id, el('p'));
  tm.fire();
  assert.equal(f.settle({session: 's'}, el('late')), false);
  assert.equal(f.queuedCount(), 0);
});

test('флаг выкл: таймеры не создаются, fail() — false', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => false, ...tm});
  assert.equal(f.begin('s', () => el('x')), null);
  assert.equal(tm.pending(), 0);
  assert.equal(f.fail('c-1'), false);
});

test('таймаут по умолчанию — 5 минут (ход может думать долго)', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => true, ...tm});
  f.begin('s', () => el('x'));
  assert.equal(tm.lastMs(), 300000);
});

test('быстрая серия в одну миллисекунду: id уникальны, порядок подтверждений — по отправке', () => {
  const tm = fakeTimers();
  const f = createPendingFeed({enabled: () => true, now: () => 5, ...tm});
  const ids = Array.from({length: 50}, () => f.begin('s', () => el('x')));
  assert.equal(new Set(ids).size, 50);
  assert.ok(ids.every(Boolean));
  const els = ids.map(i => { const e = el(i); f.attach(i, e); return e; });
  for (let k = 0; k < 50; k++) f.settle({session: 's'}, el('r' + k));
  // каждый подтверждён ровно один раз и в порядке отправки
  els.forEach((e, k) => assert.equal(e.replacedBy.name, 'r' + k));
  assert.equal(f.queuedCount(), 0);
});

test('модель не растёт: подтверждённые записи забываются', () => {
  const p = createPending();
  p.add('a', 's', 1);
  p.settle('a');
  assert.equal(p.forget('a'), true);
  assert.equal(p.list().length, 0);
  assert.equal(p.forget('a'), false);
  // после forget тот же msg_id снова добавляется (не дубль: старый уже закрыт)
  assert.equal(p.add('a', 's', 2), true);
});
