// Запрос к модели: действие, модель, свёрнутые сообщения и функции
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {llmRequestLine, toggleMark} from '../../../gateway/static/monitor/blocks/llm.js';

test('llm request: action label, model, folded messages and tools', () => {
  const row = llmRequestLine({role: 'summarize', model: 'glm', session: 'voice',
    messages: [{role: 'system', content: '<p>'}, {role: 'assistant', tool_calls: ['a', 'b'], content: ''}], tools: ['x']});
  assert.ok(row.html.includes('→ сжатие истории') && row.html.includes('<span class="llm-req-model t-dim">glm</span>'));
  assert.ok(row.html.includes(': 2 сообщений в контексте, инструментов 1') && row.html.includes('[voice]'));
  assert.ok(row.html.includes('<pre>&lt;p&gt;</pre>') && row.html.includes('→ a, b') && row.html.includes('tools: x'));
  const fast = llmRequestLine({fast: true, model: 'q', messages: []});
  assert.equal(fast.dot, 'dot-cyan');
  assert.ok(fast.html.includes('<span class="t-cyan" style="font-weight:600">→ быстрый ответ</span>'));
  assert.equal(toggleMark(true), '▼');
});

test('llm request: memory, archive and a loaded skill are cards, the system prompt is folded', async () => {
  const {RAG_HEADER, ARCHIVE_HEADER, splitCards} = await import('../../../gateway/static/monitor/blocks/cards.js');
  const system = `ROOT\n\n${RAG_HEADER}\n- факт\n\n${ARCHIVE_HEADER}\n- давнее`;
  const {rest, cards} = splitCards(system);
  assert.equal(rest, 'ROOT');
  assert.deepEqual(cards.map(c => [c.title, c.body]), [['память по фразе', '- факт'], ['давнее из архива', '- давнее']]);
  assert.deepEqual(splitCards('[итог load_skill]\nшаг 1').cards, [{title: 'скилл (load_skill)', body: 'шаг 1'}]);
  const row = llmRequestLine({role: 'main', model: 'm', messages: [{role: 'system', content: system}]});
  assert.ok(row.html.includes('<div class="llm-card"><span class="t-tool">память по фразе</span><pre>- факт</pre></div>'));
  assert.ok(row.html.includes('<details><summary class="t-dim">системный промпт, 4 знаков</summary><pre>ROOT</pre></details>'));
});
