// SAM-241: node --test tests/monitor_pending.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {createPending, pendingEnabled} from '../gateway/static/monitor/pending.js';

test('флаг выключен по умолчанию, включается значением 1', () => {
  assert.equal(pendingEnabled({getItem: () => null}), false);
  assert.equal(pendingEnabled({getItem: () => '0'}), false);
  assert.equal(pendingEnabled({getItem: () => '1'}), true);
  assert.equal(pendingEnabled({getItem() { throw new Error('x'); }}), false);
});

test('сообщение видно сразу в очереди, метка снимается', () => {
  const p = createPending();
  assert.equal(p.add('m1', 's', 100), true);
  assert.equal(p.queuedCount(), 1);
  assert.equal(p.settle('m1'), true);
  assert.equal(p.queuedCount(), 0);
  assert.equal(p.settle('nope'), false);
});

test('порядок по времени отправки, не доставки', () => {
  const p = createPending();
  p.add('late', 's', 300);
  p.add('early', 's', 100);
  p.add('mid', 's', 200);
  p.settle('late');   // доставлено первым
  assert.deepEqual(p.list().map(r => r.msg_id), ['early', 'mid', 'late']);
});

test('дубль по msg_id не добавляется', () => {
  const p = createPending();
  assert.equal(p.add('m1', 's', 1), true);
  assert.equal(p.add('m1', 's', 2), false);
  assert.equal(p.list().length, 1);
  assert.equal(p.add('', 's', 3), false);
});
