// Песочница и живое приложение: метка сети, фрейм только со своих адресов
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {sandboxNetMark, sandboxLine, appLine} from '../../../gateway/static/monitor/blocks/sandbox.js';

test('sandbox network mark: internet granted or refused, nothing without network', () => {
  assert.ok(sandboxNetMark({mode: 'internet', granted: true}).includes('сеть: интернет'));
  assert.ok(sandboxNetMark({mode: 'internet', granted: false}).includes('сеть: отказ'));
  assert.equal(sandboxNetMark({mode: 'none', granted: false}), '');
  assert.equal(sandboxNetMark(undefined), '');
});

test('sandbox and app frames only for own preview addresses', () => {
  const ok = sandboxLine({code: 0, job: 'j', files: [1], preview: '/sandbox/j/index.html', text: 'out'});
  assert.equal(ok.dot, 'dot-green');
  assert.ok(ok.html.includes('<iframe sandbox="allow-scripts" src="/sandbox/j/index.html"') && ok.html.includes('out</pre>'));
  assert.ok(!sandboxLine({code: 1, preview: 'https://evil/x'}).html.includes('<iframe'));
  assert.ok(appLine({ok: true, preview: '/apps/a/'}).html.includes('<iframe'));
  assert.ok(appLine({ok: false, preview: '/sandbox/x', text: 'нет'}).html.includes('нет</pre>'));
});
