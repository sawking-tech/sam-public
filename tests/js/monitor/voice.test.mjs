// Голос и ход: строка состояния голоса, «SAM думает» с кнопкой прервать
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {voiceStateLine, busyLine, isListening} from '../../../gateway/static/monitor/blocks/voice.js';

test('voice state and busy row', () => {
  const wake = voiceStateLine({state: 'wake_detected'});
  assert.equal(wake.mark, '🎤');
  assert.ok(wake.html.includes('<span class="t-lilac">Сэм услышала'));
  assert.ok(voiceStateLine({state: 'rec_end', rms: 'x'}).html.includes('rms 0'));
  assert.deepEqual([voiceStateLine({state: 'other'}).dot, voiceStateLine({state: 'other'}).html],
    ['dot-muted', '<span class="t-muted">other</span>']);
  assert.ok(isListening('rec_start') && !isListening('rec_end'));
  assert.ok(busyLine({session: 'a"b'}).html.includes('onclick="cancelTurn(&quot;a\\&quot;b&quot;)"'));
});
