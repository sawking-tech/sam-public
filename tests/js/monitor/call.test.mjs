// Вызов инструмента: строка вызова, итог с обрезкой, итог без вызова, план воркера, пометка и блокировка guard
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {callLine, resultHtml, resultRowHtml, orphanResultLine, workerPlanLine, isErrorResult}
  from '../../../gateway/static/monitor/blocks/call.js';
import {guardMarkHtml, guardBlockLine} from '../../../gateway/static/monitor/blocks/guard.js';

test('call line: saving tool, worker tool, plain tool, debug tool', () => {
  const persist = callLine({name: 'remember', args: {fact: 'x'}, session: 'voice'});
  assert.equal(persist.mark, '💾');
  assert.ok(persist.html.includes('сохраняет') && persist.html.includes('[voice]'));
  const worker = callLine({name: 'run_command', session: 'task:abcd1234'});
  assert.equal(worker.mark, '🛠');
  assert.equal(worker.dot, 'dot-purple');
  assert.ok(!worker.html.includes('[task:'));
  assert.equal(callLine({name: 'get_devices'}).mark, '●');
  assert.equal(callLine({name: 'noop'}).debug, true);
  assert.equal(callLine({name: 'get_devices'}).debug, false);
});

test('result: short as is, cut result says how much came', () => {
  assert.equal(resultHtml({result: 'итог'}, 400), 'итог');
  assert.ok(resultHtml({result: 'начало', result_len: 9000}, 400).includes('[пришло 6 из 9000 знаков]'));
});

test('result row and orphan result: error in red', () => {
  assert.ok(isErrorResult({result: '[GUARD] нельзя'}) && isErrorResult({result: 'Ошибка: x'}));
  assert.ok(resultRowHtml({result: 'Ошибка: x'}).includes('<span class="ev-res-body t-red">'));
  assert.ok(resultRowHtml({result: 'ok'}).includes('<span class="ev-hook">⎿</span>'));
  const orphan = orphanResultLine({name: 'remember', result: 'ok'});
  assert.equal(orphan.dot, 'dot-green');
  assert.ok(orphan.html.startsWith('<span class="t-tool">remember</span>'));
});

test('worker plan lists the calls', () => {
  const plan = workerPlanLine({turn: 2, calls: [{name: 'a', args: {}}, {name: 'b', args: {x: 1}}]});
  assert.ok(plan.html.includes('turn 2, 2 вызов(ов):') && plan.html.includes('<span class="t-dim">2.</span>'));
});

test('guard mark: passed, waiting, blocked with the reason', () => {
  const ok = guardMarkHtml({state: 'ok', level: 'llm'});
  assert.ok(ok.includes('class="guard-mark ok"') && ok.includes('проверку прошёл') && ok.includes('guard ✓'));
  const wait = guardMarkHtml({state: 'wait', level: 'llm'});
  assert.ok(wait.includes('class="guard-mark wait"') && wait.includes('идёт проверка'));
  const block = guardMarkHtml({state: 'block', level: 'llm', reason: 'LLM judge: UNSAFE'});
  assert.ok(block.includes('class="guard-mark block"') && block.includes('заблокирован: LLM judge: UNSAFE'));
});

test('guard block line tags only a non-agent session', () => {
  assert.ok(guardBlockLine({tool: 't', session: 'voice'}).html.includes('[voice]'));
  assert.ok(!guardBlockLine({tool: 't', session: 'task:1'}).html.includes('[task:1]'));
});
