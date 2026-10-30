/* Кнопки хода: STOP в полосе (все идущие ходы во всех сессиях) и «прервать» на строке «SAM думает». */
import {JSON_POST} from './api.js';
import {append, blockRow} from './feed.js';
import {clockNow} from './format.js';
import {panicLine, turnCancelledLine} from './blocks/action.js';
import {flash} from './composer.js';

// Прервать ВСЕ активные ходы - независимо от сессии: модель залипла в стриме (например, в петле рассуждения)
export function panicCancel() {
  fetch('/sam/cancel-all', JSON_POST)
    .then(r => r.json())
    .then(d => append(blockRow(clockNow(), panicLine(d))))
    .catch(() => flash());
}

export function cancelTurn(sess) {
  if (!sess) return;
  fetch(`/sam/session/${encodeURIComponent(sess)}/cancel-turn`, JSON_POST)
    .then(r => r.json())
    .then(() => append(blockRow(clockNow(), turnCancelledLine(sess), sess)))
    .catch(() => flash());
}
