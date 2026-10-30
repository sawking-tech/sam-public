// Пометка guard ждёт свой вызов по сессии: вызов из текста ответа (text_*) - пометка в ожидании, блокировка её
// снимает; вызов из ответа модели не ждёт - его строка уже в ленте
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {markGuard} from '../../../gateway/static/monitor/calls.js';
import {guardMarks, callRows, clearAll} from '../../../gateway/static/monitor/state.js';
import {GUARD_OK, GUARD_WAIT, GUARD_BLOCK} from '../../../gateway/static/monitor/blocks/guard.js';

const run = evs => {
  clearAll(guardMarks, callRows);
  for (const [cid, st] of evs) markGuard(cid, st, {session: 's1', level: 'llm', ts_iso: '2026-10-15T00:00:00Z'});
  return JSON.parse(JSON.stringify(guardMarks));
};

test('a block drops the waiting mark', () => {
  assert.deepEqual(run([['text_1_0', GUARD_WAIT], ['text_1_0', GUARD_BLOCK]]), {});
});

test('a passed check waits for its call keyed by session', () => {
  const passed = run([['text_2_0', GUARD_WAIT], ['text_2_0', GUARD_OK]]);
  assert.deepEqual(Object.keys(passed), ['s1|text_2_0']);
  assert.equal(passed['s1|text_2_0'].state, 'ok');
});

test('a native call does not wait', () => {
  assert.deepEqual(run([['call_abc', GUARD_OK]]), {});
});
