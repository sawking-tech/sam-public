// Реплика SAM: значок судьбы озвучки, мета-блоки ответа (ADR-003), текст без озвучки, подписи и приглушение
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fateIcon, fateHead, renderRaw, renderNote, replyLine, spokenLine, streamLine}
  from '../../../gateway/static/monitor/blocks/reply.js';

const SCHEME = '┌──┐\n│ГW│──> монитор\n└──┘  <tag>';
// речь вокруг, заметка монитору, молчание, оборванное рассуждение
const META_ANSWER = 'Свет включила. <note kind="зал">лампа 2</note>List<String> тут.<silent></silent> <think>оборвано';

test('fate icon: played, unknown with a question mark, none without', () => {
  const played = renderRaw('Раз.', 'played'), unknown = renderRaw('<voice>Раз.</voice>', 'unknown'), none = renderRaw('Раз.');
  assert.ok(played.includes('<svg') && !played.includes('<sup>?</sup>'));
  assert.ok(unknown.includes('</svg><sup>?</sup>') && unknown.includes('fate-unknown'));
  assert.ok(!none.includes('<sup>?</sup>'));
  for (const v of [played, unknown, none]) assert.ok(!v.includes('судьба неизвестна]'));
  assert.ok(fateIcon('played').includes('title="прозвучало"'));
});

test('meta blocks do not sound and the fate icon stays with the first speech', () => {
  const got = renderRaw(META_ANSWER, 'played');
  assert.equal(got.split('fate-icon').length - 1, 1);
  assert.ok(got.indexOf('fate-icon') < got.indexOf('Свет включила.'));
  assert.ok(got.includes('<span class="t-dim">[note] лампа 2</span>') && got.includes('[не звучит] молчит'));
  assert.ok(got.includes('[think] оборвано') && got.includes('List&lt;String&gt; тут.'));
  assert.equal(renderRaw(''), '');
});

test('text note keeps its layout as is', () => {
  const got = renderNote(SCHEME);
  assert.ok(got.startsWith('<pre class="text-note">'));
  assert.ok(got.includes('│ГW│──&gt; монитор\n└──┘  &lt;tag&gt;'));
});

test('reply line: speaker label, model, text channel, muted system reply', () => {
  const fast = replyLine({content: 'Да.', source: 'fast', model: 'qwen'});
  assert.equal(fast.mark, '🤖');
  assert.equal(fast.dot, 'dot-green');
  assert.ok(fast.html.startsWith('<span class="t-green" style="font-weight:600">SAM · быстрая · qwen:</span> '));
  assert.equal(fast.muted, false);
  const note = replyLine({content: SCHEME, channel: 'text'});
  assert.equal(note.mark, '▤');
  assert.ok(note.html.includes('SAM · текст:') && note.html.includes('<pre class="text-note">'));
  const muted = replyLine({content: 'внутреннее', system: true, voice: null});
  assert.equal(muted.muted, true);
  assert.equal(muted.dot, 'dot-muted');
});

test('spoken line and stream placeholder', () => {
  assert.ok(spokenLine({content: 'Раз.', fate: 'played'}).html.startsWith('<span class="t-green" style="font-weight:600">SAM:</span> '));
  const stream = streamLine();
  assert.ok(stream.html.includes('<span class="stream-buf"></span>') && stream.html.includes('stream-cursor'));
});

test('fate head drops service marks and keeps the first 20 chars', () => {
  assert.equal(fateHead('[бодро] Привет, как дела у тебя сегодня?'), 'Привет, как дела у т');
});
