/* Служебные строки хода и фона: петля вызовов, проверка плана critic, рассуждение, вставка в агента, консолидация
   памяти, шаг workflow стенда и дыра в ленте после обрыва связи. */
import {esc} from '../format.js';
import {line} from './row.js';

export function loopGuardLine(ev) {
  return line('⚠', 'dot-red',
    `<span class="t-red">loop:</span> <span class="t-muted">${esc(ev.tool)} × ${ev.streak} — injecting hint</span>`);
}

const CRITIC = {
  critic_check: ev => line('🧐', 'dot-amber',
    `<span class="t-amber">critic/check</span> <span class="t-muted">${ev.tools_count || 0} вызов(ов)</span>`),
  critic_pass: () => line('🧐', 'dot-green',
    `<span class="t-green">critic/pass</span> <span class="t-muted">план одобрен</span>`),
  critic_block: ev => line('🧐', 'dot-red',
    `<span class="t-red">critic/block + FIX:</span><br>` +
    `<pre style="margin:4px 0 0 8px;white-space:pre-wrap;font-size:12px;color:var(--m-text)">${esc(ev.reason || '')}</pre>`),
};
export const criticLine = ev => CRITIC[ev.type](ev);

export function thinkingLine(ev) {
  return line('…', 'dot-muted',
    `<span class="t-dim">рассуждение:</span> <span class="t-muted">${esc(ev.content)}</span>`);
}

export function injectedLine(ev) {
  return line('↓', 'dot-cyan',
    `<span class="t-cyan">${esc(ev.content)}</span> <span class="t-dim">[${esc(ev.session || '?')}]</span>`);
}

// Ночная консолидация памяти (SAM-22): отчёт по шагам
export function dreamLine(ev) {
  const steps = Object.entries(ev.steps || {}).map(([k, v]) => `${esc(k)}: ${esc(JSON.stringify(v))}`).join('<br>');
  return line('☾', 'dot-blue', `<span class="t-blue">консолидация памяти</span><br>` +
    `<span class="t-muted" style="font-size:12px">${steps}</span>`);
}

// Лёгкий workflow стенда (SAM-96): шаг ушёл основной модели или инструменту; источник - workflow:<имя>/<шаг>
export function workflowLine(ev) {
  if (ev.error) {
    return line('⏱', 'dot-red',
      `<span class="t-red">workflow:${esc(ev.workflow || '')} не выполнен</span> <span class="t-muted">${esc(ev.error)}</span>`);
  }
  const reason = ev.reason ? `<br><span class="t-muted" style="font-size:12px">повод: ${esc(ev.reason)}</span>` : '';
  const reply = ev.reply ? `<br><span class="t-dim" style="font-size:12px">${esc(ev.reply)}</span>` : '';
  return line('⏱', 'dot-blue',
    `<span class="t-blue">${esc(ev.source || '')}</span> <span class="t-muted">→ ${esc(ev.target || '')}</span>` +
    reason + reply);
}

// обрыв дольше порции снимка: между показанным и снимком могла остаться дыра
export function gapLine() {
  return line('…', 'dot-muted',
    '<span class="t-dim">часть ленты за время обрыва связи не показана - обнови страницу</span>');
}
