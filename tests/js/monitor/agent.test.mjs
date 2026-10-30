// Агент в ленте: шапка секции, подсказка, запуск, шаг плана, сообщение и его итог; карточка задачи в панели
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {taskTitle, taskHeaderHtml, taskHeaderTitle, taskCreatedLine, stageLine, taskMessageLine, taskMessageStatus}
  from '../../../gateway/static/monitor/blocks/agent.js';
import {taskCardHtml} from '../../../gateway/static/monitor/blocks/task-card.js';

const entry = over => ({name: '', descr: '', status: 'running', stage: '', last_msg: '', final: '', count: 0, ...over});

test('agent title: name, else the start of the description, else the id', () => {
  assert.equal(taskTitle('abcd1234', entry({name: 'Свет'})), '«Свет»');
  assert.equal(taskTitle('abcd1234', entry({descr: 'x'.repeat(70)})), `«${'x'.repeat(60)}…»`);
  assert.equal(taskTitle('abcd1234', entry()), 'abcd1234');
});

test('header: toggle, status, stage, final over the last message, step count', () => {
  const html = taskHeaderHtml('k', entry({status: 'done', stage: 'шаг 1', final: 'готово', last_msg: 'идёт', count: 3}), false);
  assert.ok(html.startsWith('<span class="toggle">▶</span><span class="tsh-status t-green">✓</span>'));
  assert.ok(html.includes('[шаг 1]') && html.includes('→ готово') && !html.includes('… идёт'));
  assert.ok(html.endsWith('<span class="count" title="шагов агента - раскрыть">шагов: 3</span>'));
  assert.ok(taskHeaderHtml('k', entry({last_msg: 'идёт'}), true).includes('<span class="toggle">▼</span>'));
  assert.ok(taskHeaderHtml('k', entry({status: 'past'}), true).includes('<span class="tsh-status t-muted">●</span>'));
  assert.equal(taskHeaderTitle('k', entry({descr: 'd', final: 'f'})),
    'Агент k, которого запустила SAM: раскрыть - его шаги\n\nd\n\nf');
});

test('created, stage and message lines', () => {
  const created = taskCreatedLine({task_id: 't1', device: 'pc', stage_count: 2, description: 'd',
    stages: [{name: 'a'}, {name: 'b'}]});
  assert.ok(created.html.includes('├─ a') && created.html.includes('└─ b') && created.html.includes('2 stages'));
  const stage = stageLine({task_id: 't1', status: 'failed', idx: 1, total: 3, name: 'b', attempt: 2});
  assert.equal(stage.mark, '✗');
  assert.equal(stage.dot, 'dot-red');
  assert.ok(stage.html.includes('[2/3]') && stage.html.includes('retry 2'));
  assert.equal(stageLine({status: 'weird'}).mark, '?');
  assert.ok(taskMessageLine({task_id: 't1', content: 'x'}).html.startsWith('<span class="sess">[task:t1]</span>'));
});

test('agent message with the answer mark is the final, otherwise the last message', () => {
  assert.deepEqual(taskMessageStatus('ОТВЕТ: всё'), {final: 'всё'});
  assert.deepEqual(taskMessageStatus('думаю'), {last_msg: 'думаю'});
  assert.deepEqual(taskMessageStatus(undefined), {last_msg: ''});
});

test('task card: cancel only while running, answer mark dropped from the result', () => {
  const t = {id: 'abc', name: '', device: 'pc', status: 'running', desc: 'd', result: 'ОТВЕТ: итог',
    stages: [{name: 's', status: 'done'}]};
  const html = taskCardHtml(t);
  assert.ok(html.includes('onclick="cancelTask(&quot;abc&quot;)"') && html.includes('viewTaskSession(&quot;abc&quot;)'));
  assert.ok(html.includes('<div class="tcard-res">итог</div>') && html.includes('<span class="tcard-dev">pc</span>'));
  assert.ok(html.includes('<div class="ts-row done"><span class="ts-dot">✓</span>'));
  assert.ok(!taskCardHtml({...t, status: 'done', name: 'Имя'}).includes('cancel-btn'));
});
