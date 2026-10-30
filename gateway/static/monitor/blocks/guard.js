/* Проверка guard: пометка на строке вызова (прошёл, идёт, заблокирован) и строка блокировки. */
import {esc} from '../format.js';
import {line} from './row.js';
import {TASK_PREFIX} from '../ids.js';

export const GUARD_OK = 'ok', GUARD_WAIT = 'wait', GUARD_BLOCK = 'block';
const GUARD_LABELS = {ok: 'guard ✓', wait: 'guard: проверка…', block: 'guard ✗'};

export function guardMarkHtml(m) {
  const why = m.state === GUARD_BLOCK ? `заблокирован: ${m.reason || ''}`
    : m.state === GUARD_OK ? 'проверку прошёл' : 'идёт проверка';
  return `<span class="guard-mark ${m.state}" title="guard (${esc(m.level || '')}): ${esc(why)}">${GUARD_LABELS[m.state]}</span>`;
}

export function guardBlockLine(ev) {
  return line('🛡', 'dot-red',
    `<span class="t-red">guard/block</span> <span class="t-muted">${esc(ev.tool || '')} (${esc(ev.level || '')}) — ${esc(ev.reason || '')}</span>` +
    (ev.session && !ev.session.startsWith(TASK_PREFIX) ? ` <span class="t-dim">[${esc(ev.session)}]</span>` : ''));
}
