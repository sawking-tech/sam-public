/* Итоги действий самой страницы: STOP, прерывание хода, снятие последней реплики, откат истории, команда и вставка
   в агента. d - ответ шлюза на запрос действия. */
import {esc} from '../format.js';
import {line} from './row.js';

const POPPED_PREVIEW_CHARS = 80;   // начало снятой из очереди реплики
const sessTag = device => `<span class="sess">[${esc(device)}]</span>`;

export function panicLine(d) {
  return line('⛔', 'dot-red',
    `<span class="t-red" style="font-weight:600">PANIC STOP:</span> ` +
    `<span class="t-muted">прервано ${d.cancelled} активных turn'ов</span>`);
}

// кнопка «прервать» на строке «SAM думает»
export function turnCancelledLine(sess) {
  return line('⊘', 'dot-red', `<span class="t-red">прервано:</span> <span class="t-dim">[${esc(sess)}]</span>`);
}

// Ctrl/Shift+Esc в поле ввода: прервать ход адресата
export function cancelTurnLine(d, device) {
  const txt = d.turn_cancelled ? 'текущий turn прерван' : 'нечего прерывать';
  return line('⊘', 'dot-red', sessTag(device) + `<span class="t-red">${esc(txt)}</span>`);
}

// Esc в пустом поле: снять последнюю реплику из очереди
export function popLastLine(d, device) {
  if (d.removed) {
    return line('⊘', 'dot-amber', sessTag(device) +
      `<span class="t-muted">↩ убрано из очереди: ${esc(d.removed.slice(0, POPPED_PREVIEW_CHARS))} (осталось ${d.queue_size})</span>`);
  }
  return line('⊘', 'dot-muted', `<span class="t-muted">очередь пуста — нечего отменять</span>`);
}

// Ctrl+Z: убрать последний обмен из истории
export function undoLine(d, device) {
  return line('⎌', 'dot-amber', sessTag(device) +
    `<span class="t-muted">↩ из истории удалено сообщений: ${d.deleted || 0}</span>`);
}

export function commandFailedLine(detail) {
  return line('✗', 'dot-red', `<span class="t-red">команда не ушла:</span> ${esc(detail)}`);
}

export function reanimatedLine(device) {
  return line('⤴', 'dot-purple',
    `<span class="t-purple">воркер реанимирован</span> <span class="t-dim">[${esc(device)}]</span>`);
}

export function injectFailedLine(d) {
  return line('✗', 'dot-red', `<span class="t-red">inject failed:</span> ${esc(d.error || '')}`);
}
