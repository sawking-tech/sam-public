/* Карточка задачи агента во вкладке «Задачи» панели: id, имя или устройство, кнопки, статус, описание, итог, шаги. */
import {esc, jsArg, clip, trim} from '../format.js';
import {ANSWER_PREFIX_RE} from './agent.js';

const TASK_RESULT_CLIP_CHARS = 160;
const CARD_DESC_CHARS = 50;
const STAGE_ICON = {pending: '●', running: '⟳', done: '✓', failed: '✗', skipped: '→', cancelled: '⊘'};

// t - {id, name, device, status, desc, result, stages: [{name, status}]}; кнопки зовут cancelTask и viewTaskSession
export function taskCardHtml(t) {
  const stages = t.stages.map(s =>
    `<div class="ts-row ${s.status}">` +
      `<span class="ts-dot">${STAGE_ICON[s.status] || '?'}</span>` +
      `<span class="ts-name">${esc(s.name)}</span>` +
    `</div>`).join('');
  const cancelBtn = t.status === 'running'
    ? `<button class="cancel-btn" onclick="cancelTask(${jsArg(t.id)})">✕ отмена</button>`
    : '';
  const viewBtn = `<button class="view-sess-btn" onclick="viewTaskSession(${jsArg(t.id)})" title="Лента агента: его шаги; назад - кнопкой над лентой">👁</button>`;
  return `<div class="tcard-head">` +
      `<span class="tcard-id">#${esc(t.id)}</span>` +
      (t.name ? `<span class="tcard-name">${esc(t.name)}</span>` : `<span class="tcard-dev">${esc(t.device)}</span>`) +
      cancelBtn + viewBtn +
      `<span class="tcard-st ${esc(t.status)}">${esc(t.status)}</span>` +
    `</div>` +
    `<div class="tcard-desc" title="${esc(t.desc)}">"${esc(trim(t.desc, CARD_DESC_CHARS))}"</div>` +
    (t.result ? `<div class="tcard-res">${clip(String(t.result).replace(ANSWER_PREFIX_RE, ''), TASK_RESULT_CLIP_CHARS)}</div>` : '') +
    (stages ? `<div class="tcard-stages">${stages}</div>` : '');
}
