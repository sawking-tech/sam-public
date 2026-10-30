// Преобразования текста монитора: экранирование, аргумент onclick, обрезка, аргументы вызова, падеж, время
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {esc, jsArg, trim, clip, fmtArgs, ruPlural, fmtTs} from '../../../gateway/static/monitor/format.js';

const HOSTILE_IDS = ["x');alert(1);('", 'a"b', 'back\\slash', '<img src=x>', 'обычная'];
// браузер сначала декодирует HTML-сущности атрибута, потом разбирает JS
const decode = s => s.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

test('onclick argument round-trips any session id', () => {
  for (const id of HOSTILE_IDS) {
    let got = null;
    new Function('cancelTurn', decode(`cancelTurn(${jsArg(id)})`))(v => { got = v; });
    assert.equal(got, id);
  }
});

test('esc escapes markup and quotes, null is empty', () => {
  assert.equal(esc('<a href="x">&</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;');
  assert.equal(esc(null), '');
});

test('clip keeps short text and folds the rest of long text into the row', () => {
  assert.equal(clip('a<b', 5), 'a&lt;b');
  const long = clip('xxxxx<yyyyyyyyy', 5);
  assert.ok(long.startsWith('<span class="clip"'));
  assert.ok(long.includes('xxxxx<span class="clip-more">…</span>'));
  assert.ok(long.includes('<span class="clip-rest">&lt;yyyyyyyyy</span>'));
});

test('trim cuts with an ellipsis', () => {
  assert.equal(trim('abcdef', 3), 'abc…');
  assert.equal(trim(null, 3), '');
});

test('call arguments: strings quoted, other values as JSON', () => {
  const html = fmtArgs({q: 'свет', n: 2});
  assert.ok(html.includes('<span class="t-dim">q:</span>\u00a0<span class="t-arg">"свет"</span>'));
  assert.ok(html.includes('<span class="t-arg">2</span>'));
  assert.equal(fmtArgs(null), '');
});

test('russian plural', () => {
  const p = n => ruPlural(n, 'сообщение', 'сообщения', 'сообщений');
  assert.deepEqual([1, 2, 5, 11, 12, 14, 21, 22, 25].map(p),
    ['сообщение', 'сообщения', 'сообщений', 'сообщений', 'сообщений', 'сообщений', 'сообщение', 'сообщения', 'сообщений']);
});

test('row time: ts_iso to local time, else the string or ts', () => {
  assert.match(fmtTs({ts_iso: '2026-10-15T00:00:00Z'}), /^\d\d:\d\d:\d\d$/);
  assert.equal(fmtTs('12:00:01'), '12:00:01');
  assert.equal(fmtTs({ts: '09:00:00'}), '09:00:00');
  assert.equal(fmtTs(undefined), '');
});
