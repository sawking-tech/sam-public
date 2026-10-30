// Сжатие истории: строка шапки, ход и конец сжатия, сводка без тегов модели
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {compressionText, compressionHeader, compressionProgressHtml, compressionDone, cleanSummary, summaryLine,
  compressionErrorLine, compressionWaitingLine} from '../../../gateway/static/monitor/blocks/compression.js';

test('compression line per session', () => {
  assert.deepEqual([
    compressionText('homepc', {pending_msgs: 24, last_ok_ago_s: 60}),
    compressionText('voice', {pending_msgs: 1, last_ok_ago_s: 20}),
    compressionText('voice', {pending_msgs: 22, last_ok_ago_s: 300}),
    compressionText('voice', {pending_msgs: 5, last_ok_ago_s: null}),
  ], ['homepc: 24 сообщения без сжатия, последнее сжатие 1 мин назад',
      'voice: 1 сообщение без сжатия, сжато только что',
      'voice: 22 сообщения без сжатия, последнее сжатие 5 мин назад',
      'voice: 5 сообщений без сжатия, сжатий ещё не было']);
  // 11-14 - родительный падеж
  assert.deepEqual([11, 12, 14, 21, 25].map(n => compressionText('s', {pending_msgs: n, last_ok_ago_s: 600})
    .split(': ')[1].split(' без')[0]), ['11 сообщений', '12 сообщений', '14 сообщений', '21 сообщение', '25 сообщений']);
  assert.ok(compressionText('s', {pending_msgs: 1, waiting_s: 3, running_s: 4, last_error: 'x'})
    .endsWith('ждёт паузы 3 с, идёт 4 с, ошибка'));
});

test('header: red on an error or a ceiling without running compression, error in the tooltip', () => {
  const ok = compressionHeader({sessions: {s: {pending_msgs: 2, last_ok_ago_s: 0}}, ceiling_msgs: 40, ceiling_tokens: 9000});
  assert.equal(ok.cls, 't-dim');
  assert.ok(ok.text.startsWith('сжатие s: 2 сообщения'));
  assert.ok(ok.title.includes('потолок 40 сообщений или 9000 токенов'));
  const bad = compressionHeader({sessions: {s: {pending_msgs: 2, last_error: 'boom', last_error_at: 't'}}});
  assert.equal(bad.cls, 't-red');
  assert.equal(bad.title, 's: boom (t)');
  assert.equal(compressionHeader({sessions: {s: {above_ceiling: true, running_s: 3}}}).cls, 't-dim');
  assert.equal(compressionHeader({}).text, '');
});

test('progress and done', () => {
  assert.ok(compressionProgressHtml({session: 'v', done: 3, total: 9, call: 1, calls: 2})
    .includes('3 из 9 сообщ., вызов 1 из 2'));
  assert.ok(compressionProgressHtml({session: 'v', stage: 'merge', call: 4}).includes('слияние сводок, вызов 4'));
  const done = compressionDone('v', null, 3);
  assert.equal(done.dot, 'dot-muted');
  assert.ok(done.html.includes('готово, вызовов 3'));
  const failed = compressionDone('v', 'boom', 1);
  assert.equal(failed.dot, 'dot-red');
  assert.ok(failed.html.includes('<span class="t-red">не удалось, вызовов 1</span>'));
});

test('summary: model tags dropped, roles named for people', () => {
  assert.equal(cleanSummary('user: привет <think>x</think> assistant: да'), 'Человек: привет x SAM: да');
  const s = summaryLine({time_range: '10:00-11:00', content: 'user: <b>'});
  assert.equal(s.mark, '⊡');
  assert.ok(s.html.includes('[сжато 10:00-11:00]') && s.html.includes('Человек: '));
});

test('error and waiting lines', () => {
  assert.equal(compressionErrorLine({error: 'boom'}).dot, 'dot-red');
  assert.ok(compressionWaitingLine({max_wait_s: 30}).html.includes('не дольше 30 с'));
  assert.ok(compressionWaitingLine({}).html.includes('не дольше - с'));
});
