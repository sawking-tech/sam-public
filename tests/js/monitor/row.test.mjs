// Рамка строки ленты: крупные реплики, lilac у голоса, разметка времени, метки и тела
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {rowClass, rowDot, rowHtml} from '../../../gateway/static/monitor/blocks/row.js';

test('row frame: talk rows larger, voice marks lilac', () => {
  assert.equal(rowClass('👤'), 'ev ev-talk');
  assert.equal(rowClass('●'), 'ev');
  assert.equal(rowDot('💭', 'dot-blue'), 'dot-lilac');
  assert.equal(rowDot('●', 'dot-gray'), 'dot-gray');
  assert.equal(rowHtml('10:00:00', '●', 'dot-gray', 'x'),
    '<span class="ev-ts">10:00:00</span><span class="ev-dot dot-gray">●</span><span class="ev-body">x</span>');
});
