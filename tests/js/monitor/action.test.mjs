// Итоги кнопок страницы: STOP, прервать, снять последнее, откат истории, команда, вставка в агента
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {panicLine, turnCancelledLine, cancelTurnLine, popLastLine, undoLine, commandFailedLine, reanimatedLine,
  injectFailedLine} from '../../../gateway/static/monitor/blocks/action.js';

test('page action results', () => {
  assert.ok(panicLine({cancelled: 2}).html.includes("прервано 2 активных turn'ов"));
  assert.ok(turnCancelledLine('s').html.includes('[s]'));
  assert.ok(cancelTurnLine({turn_cancelled: true}, 'd').html.includes('текущий turn прерван'));
  assert.ok(cancelTurnLine({}, 'd').html.includes('нечего прерывать'));
  assert.ok(popLastLine({removed: 'x'.repeat(100), queue_size: 1}, 'd').html.includes(`${'x'.repeat(80)} (осталось 1)`));
  assert.equal(popLastLine({}, 'd').dot, 'dot-muted');
  assert.ok(undoLine({deleted: 2}, 'd').html.includes('удалено сообщений: 2'));
  assert.ok(commandFailedLine(404).html.includes('команда не ушла:</span> 404'));
  assert.ok(reanimatedLine('task:1').html.includes('воркер реанимирован'));
  assert.ok(injectFailedLine({error: 'e'}).html.includes('inject failed:</span> e'));
});
