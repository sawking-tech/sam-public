/* Агент (задача воркера) в ленте: шапка его сворачиваемой секции, строка запуска, шаг плана и сообщение агента. */
import {esc, clip, trim} from '../format.js';
import {line} from './row.js';

const TASK_DESC_CLIP_CHARS = 70, STAGE_MSG_CLIP_CHARS = 60;
// в шапке - начало строки, целиком - в подсказке и в развёрнутых шагах
const TITLE_DESC_CHARS = 60, HEAD_FINAL_CHARS = 80, HEAD_LAST_MSG_CHARS = 60;

const STATUS_ICON = {running: '⟳', done: '✓', failed: '✗', cancelled: '⊘', pending: '…'};
const STATUS_CLS = {running: 't-blue', done: 't-green', failed: 't-red', cancelled: 't-amber', pending: 't-amber'};
const STAGE_ICON = {pending: '●', running: '⟳', done: '✓', failed: '✗', skipped: '→'};
const STAGE_DOT = {pending: 'dot-gray', running: 'dot-blue', done: 'dot-green', failed: 'dot-red', skipped: 'dot-gray'};

// Имя агента для людей: имя задачи, иначе начало описания, иначе id
export function taskTitle(key, e) {
  if (e && e.name) return `«${esc(e.name)}»`;
  if (e && e.descr) return `«${esc(trim(e.descr, TITLE_DESC_CHARS))}»`;
  return esc(key);
}

// Шапка секции агента: e - {name, descr, status, stage, last_msg, final, count}
export function taskHeaderHtml(key, e, expanded) {
  const statusIcon = STATUS_ICON[e.status] || '●';
  const statusCls = STATUS_CLS[e.status] || 't-muted';
  const stagePart = e.stage
    ? ` <span class="t-dim">[${esc(e.stage)}]</span>` : '';
  const finalPart = e.final
    ? ` <span class="tsh-final t-green">→ ${esc(trim(e.final, HEAD_FINAL_CHARS))}</span>`
    : (e.last_msg
        ? ` <span class="tsh-final t-cyan">… ${esc(trim(e.last_msg, HEAD_LAST_MSG_CHARS))}</span>`
        : '');
  return `<span class="toggle">${expanded ? '▼' : '▶'}</span>` +
    `<span class="tsh-status ${statusCls}">${statusIcon}</span>` +
    ` <span class="t-purple">агент ${taskTitle(key, e)}</span>` +
    stagePart +
    finalPart +
    `<span class="count" title="шагов агента - раскрыть">${stepsText(e.count)}</span>`;
}
export const stepsText = n => `шагов: ${n}`;

// полный текст задачи и итога - в подсказке шапки: в самой шапке они началом строки
export function taskHeaderTitle(key, e) {
  return [`Агент ${key}, которого запустила SAM: раскрыть - его шаги`, e.descr, e.final].filter(Boolean).join('\n\n');
}

// Запуск задачи: id, устройство, описание и дерево шагов плана
export function taskCreatedLine(ev) {
  const stages = (ev.stages || []).map((s, i) => {
    const last = i === ev.stages.length - 1;
    return `<span class="t-dim">${last ? '└' : '├'}─ ${esc(s.name)}</span>`;
  }).join('  ');
  return line('◆', 'dot-purple',
    `<span class="t-purple">#${esc(ev.task_id)}</span>` +
    ` <span class="t-dim">@ ${esc(ev.device)} · ${ev.stage_count} stages</span><br>` +
    `<span class="t-muted">"${clip(ev.description, TASK_DESC_CLIP_CHARS)}"</span>` +
    (stages ? `<br><span class="stage-tree">${stages}</span>` : ''));
}

export function stageLine(ev) {
  return line(STAGE_ICON[ev.status] || '?', STAGE_DOT[ev.status] || 'dot-gray',
    `<span class="t-purple">#${esc(ev.task_id)}</span>` +
    ` <span class="t-dim">[${(ev.idx || 0) + 1}/${ev.total || '?'}]</span>` +
    ` ${esc(ev.name)}` +
    (ev.message ? ` <span class="t-dim">— ${clip(ev.message, STAGE_MSG_CLIP_CHARS)}</span>` : '') +
    (ev.attempt ? ` <span class="t-dim">retry ${ev.attempt}</span>` : ''));
}

export function taskMessageLine(ev) {
  return line('↗', 'dot-purple',
    `<span class="sess">[task:${esc(ev.task_id || '?')}]</span> <span class="t-cyan">${esc(ev.content)}</span>`);
}

// Сообщение агента с «ОТВЕТ:» - его итог, иначе последнее сообщение; приставка в шапку не идёт
const ANSWER_MARK = 'ОТВЕТ:';
export const ANSWER_PREFIX_RE = /^ОТВЕТ:\s*/;
export function taskMessageStatus(content) {
  const text = content || '';
  return {[text.includes(ANSWER_MARK) ? 'final' : 'last_msg']: text.replace(ANSWER_PREFIX_RE, '')};
}
