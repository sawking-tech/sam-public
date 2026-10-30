/* Вызов инструмента: строка вызова, итог под ней (или отдельной строкой, если вызова в ленте нет) и план воркера. */
import {esc, clip, fmtArgs} from '../format.js';
import {line} from './row.js';
import {TASK_PREFIX} from '../ids.js';

// Tools которые изменяют ПЕРСИСТЕНТНОЕ состояние — выделяем явно,
// чтобы видеть когда SAM реально что-то сохраняет/меняет
const PERSIST_TOOLS = new Set([
  'remember', 'update_memory', 'forget',
  'tune_self', 'set_env',
  'create_task',
]);
// вызовы без действия для человека: пустой ход и фоновая сверка скиллов (workflow skills-sync) - в отладку
const DEBUG_TOOLS = new Set(['noop', 'sync_skills']);

// Строка вызова; debug - вызов прячется в отладку
export function callLine(ev) {
  const isWorker = (ev.session || '').startsWith(TASK_PREFIX);
  const isPersist = PERSIST_TOOLS.has(ev.name);
  const sessTag = (ev.session && !isWorker)
    ? ` <span class="t-dim">[${esc(ev.session)}]</span>` : '';
  let mark, dot, persistBadge = '';
  if (isPersist) {
    mark = '💾';
    dot = 'dot-amber';
    persistBadge = ' <span class="t-amber" style="font-size:12px;padding:0 4px;border:1px solid var(--amber);border-radius:var(--radius-sam)">сохраняет</span>';
  } else if (isWorker) {
    mark = '🛠';
    dot = 'dot-purple';
  } else {
    mark = '●';
    dot = 'dot-gray';
  }
  return {
    ...line(mark, dot, `<span class="t-tool">${esc(ev.name)}</span>` +
      `<span class="t-args">(${fmtArgs(ev.args)})</span>` + persistBadge + sessTag),
    debug: DEBUG_TOOLS.has(ev.name),
  };
}

export const isErrorResult = ev => (ev.result || '').startsWith('Ошибка') || (ev.result || '').startsWith('[GUARD]');

// Итог вызова: начало, остальное - разворотом; шлюз шлёт не больше SAM_MONITOR_RESULT_CHARS, остальное не пришло
export const RESULT_CLIP_CHARS = 400, ORPHAN_CLIP_CHARS = 200;
export function resultHtml(ev, n) {
  const text = String(ev.result || '');
  const lost = ev.result_len > text.length
    ? ` <span class="t-dim">[пришло ${text.length} из ${ev.result_len} знаков]</span>` : '';
  return clip(text, n) + lost;
}

// итог под строкой своего вызова: крючок и тело
export function resultRowHtml(ev) {
  const isErr = isErrorResult(ev);
  return `<span></span>` +
    `<span class="ev-hook">⎿</span>` +
    `<span class="ev-res-body ${isErr ? 't-red' : 't-muted'}">${resultHtml(ev, RESULT_CLIP_CHARS)}</span>`;
}

// итог, вызова которого в ленте нет, - своей строкой
export function orphanResultLine(ev) {
  const isErr = isErrorResult(ev);
  return line('⎿', isErr ? 'dot-red' : 'dot-green',
    `<span class="t-tool">${esc(ev.name)}</span> <span class="${isErr ? 't-red' : 't-muted'}">${resultHtml(ev, ORPHAN_CLIP_CHARS)}</span>`);
}

// План воркера: список tool-вызовов, которые он собирается выполнить
export function workerPlanLine(ev) {
  const calls = ev.calls || [];
  const html = calls.map((c, i) =>
    `<div style="margin-left:8px;font-size:12px">` +
    `<span class="t-dim">${i + 1}.</span> ` +
    `<span class="t-tool">${esc(c.name)}</span>` +
    `<span class="t-args">(${fmtArgs(c.args)})</span>` +
    `</div>`
  ).join('');
  return line('📋', 'dot-blue',
    `<span class="t-blue">план</span> <span class="t-dim">turn ${ev.turn}, ${calls.length} вызов(ов):</span><br>${html}`);
}
