// Служебные строки: петля, critic, рассуждение, вставка, консолидация, workflow, дыра после обрыва
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {loopGuardLine, criticLine, thinkingLine, injectedLine, dreamLine, workflowLine, gapLine}
  from '../../../gateway/static/monitor/blocks/notice.js';

test('notice lines', () => {
  assert.ok(loopGuardLine({tool: 't', streak: 3}).html.includes('t × 3'));
  assert.equal(criticLine({type: 'critic_pass'}).dot, 'dot-green');
  assert.ok(criticLine({type: 'critic_block', reason: '<x>'}).html.includes('&lt;x&gt;</pre>'));
  assert.ok(criticLine({type: 'critic_check', tools_count: 2}).html.includes('2 вызов(ов)'));
  assert.ok(thinkingLine({content: 'хм'}).html.includes('рассуждение:'));
  assert.ok(injectedLine({content: 'x'}).html.includes('[?]'));
  assert.ok(dreamLine({steps: {dedup: 2}}).html.includes('dedup: 2'));
  assert.equal(workflowLine({error: 'нет', workflow: 'w'}).dot, 'dot-red');
  assert.ok(workflowLine({source: 'workflow:w/s', target: 'main', reason: 'r'}).html.includes('повод: r'));
  assert.ok(gapLine().html.includes('обнови страницу'));
});
