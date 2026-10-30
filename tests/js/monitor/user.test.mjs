// Реплика человека: имя, команда, пометка скрытой записи, эхо своей реплики и вставки в агента
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {ignoreMark, userLine, echoLine, imagesPreview} from '../../../gateway/static/monitor/blocks/user.js';

test('hidden phrase is marked with the reason, a plain one is not', () => {
  assert.ok(ignoreMark({ignore: 'фон - обрывок вне беседы; голос не узнан'})
    .includes('[фон - обрывок вне беседы; голос не узнан]'));
  assert.equal(ignoreMark({}), '');
});

test('user line: owner by default, command with a slash mark', () => {
  const plain = userLine({content: '<b>свет</b>'});
  assert.equal(plain.mark, '👤');
  assert.equal(plain.dot, 'dot-amber');
  assert.ok(plain.html.includes('человек:') && plain.html.includes('&lt;b&gt;свет&lt;/b&gt;'));
  const cmd = userLine({content: '/drift-guard', who: 'человек', command: true});
  assert.equal(cmd.mark, '/');
  assert.ok(cmd.html.includes('человек · команда:'));
});

test('echo: chat line or inject to an agent, with image previews', () => {
  const images = [{dataUrl: 'data:image/png;base64,AA'}];
  const chat = echoLine('', false, imagesPreview(images));
  assert.equal(chat.mark, '👤');
  assert.ok(chat.html.includes('<img src="data:image/png;base64,AA"'));
  const inject = echoLine('сделай', true, '');
  assert.equal(inject.mark, '↓');
  assert.equal(inject.dot, 'dot-cyan');
  assert.ok(inject.html.includes('<span class="t-cyan" style="font-weight:600">inject:</span>'));
  assert.equal(imagesPreview([]), '');
});
