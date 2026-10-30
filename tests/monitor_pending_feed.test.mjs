// SAM-241: node --test tests/monitor_pending_feed.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {createPendingFeed} from '../gateway/static/monitor/pending-feed.js';

const el = name => ({name, isConnected: true, replaceWith(n) { this.replacedBy = n; }});

test('флаг выкл: begin даёт null, settle даёт false, старое поведение', () => {
  const f = createPendingFeed({enabled: () => false});
  assert.equal(f.begin('s'), null);
  assert.equal(f.settle({session: 's'}, el('real')), false);
  assert.equal(f.queuedCount(), 0);
});

test('флаг вкл: реплика в очереди, подтверждение заменяет строку на месте и снимает метку', () => {
  let t = 100;
  const f = createPendingFeed({enabled: () => true, now: () => t++});
  const id = f.begin('s');
  assert.ok(id);
  const pend = el('pending');
  f.attach(id, pend);
  assert.equal(f.queuedCount(), 1);
  const real = el('real');
  assert.equal(f.settle({session: 's'}, real), true);
  assert.equal(pend.replacedBy, real);
  assert.equal(f.queuedCount(), 0);
  // повторное событие без ожидающей строки - обычный путь (вызывающий рисует сам)
  assert.equal(f.settle({session: 's'}, el('again')), false);
});

test('две реплики подряд: старая снимается первой, порядок по времени отправки', () => {
  let t = 1;
  const f = createPendingFeed({enabled: () => true, now: () => t++});
  const a = f.begin('s'), b = f.begin('s');
  const ea = el('a'), eb = el('b');
  f.attach(a, ea); f.attach(b, eb);
  f.settle({session: 's'}, el('ra'));
  assert.ok(ea.replacedBy && !eb.replacedBy);
  assert.equal(f.queuedCount(), 1);
});

test('чужая сессия и replay не трогают очередь', () => {
  const f = createPendingFeed({enabled: () => true});
  const id = f.begin('s');
  f.attach(id, el('p'));
  assert.equal(f.settle({session: 'other'}, el('r')), false);
  assert.equal(f.settle({session: 's', replay: true}, el('r')), false);
  assert.equal(f.queuedCount(), 1);
});

test('строка удалена из ленты (очистка): замены нет, вызывающий рисует сам', () => {
  const f = createPendingFeed({enabled: () => true});
  const id = f.begin('s');
  const p = el('p'); p.isConnected = false;
  f.attach(id, p);
  assert.equal(f.settle({session: 's'}, el('r')), false);
  assert.equal(p.replacedBy, undefined);
});
