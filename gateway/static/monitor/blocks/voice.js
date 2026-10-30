/* Состояние голоса и хода: «услышала, записываю», «распознаю», «SAM думает» с кнопкой прервать. Обе строки живут
   по одной на сессию и меняются на месте (live-rows.js). */
import {esc, jsArg} from '../format.js';
import {line} from './row.js';

const VOICE_STATES = {
  wake_detected: () => ['🎤', 'Сэм услышала, записываю...', 'dot-lilac'],
  rec_end: ev => ['🎙', `Запись завершена (rms ${Number(ev.rms) || 0}), распознаю...`, 'dot-lilac'],
};
// состояния, при которых в полосе каркаса горит «слушаю»
const LISTENING = new Set(['wake_detected', 'rec_start']);
// распознанный текст придёт следом событием user_message - своя строка ему не нужна
export const STT_DONE = 'stt_done';
export const isListening = state => LISTENING.has(state);

export function voiceStateLine(ev) {
  const known = VOICE_STATES[ev.state];
  const [mark, msg, dot] = known ? known(ev) : ['🎤', ev.state, 'dot-muted'];
  return line(mark, dot, `<span class="${dot.replace('dot-', 't-')}">${esc(msg)}</span>`);
}

// SAM начала думать: кнопка прерывает ход этой сессии (cancelTurn)
export function busyLine(ev) {
  return line('💭', 'dot-blue',
    `<span class="t-blue">SAM думает…</span>` +
    ` <button onclick="cancelTurn(${jsArg(ev.session || '')})" style="margin-left:8px;background:none;border:1px solid var(--red);color:var(--red);cursor:pointer;padding:2px 8px;border-radius:3px;font-size:11px">✕ прервать</button>`);
}
