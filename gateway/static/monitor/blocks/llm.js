/* Запрос к LLM (gateway/llm_monitor.py): свёрнуто «→ действие модель: N сообщений», по клику - сообщения и функции.
   Строка называет действие, модель - приглушённо после («glm-5.3-flash (summarize)» читалось как «включилась GLM»,
   а это сжатие истории). Роли - gateway/token_usage.py ROLE_*. */
import {esc} from '../format.js';
import {line} from './row.js';
import {TASK_PREFIX} from '../ids.js';
import {splitCards, cardHtml} from './cards.js';

const LLM_ROLE_LABEL = {
  main: 'ход модели', worker: 'агент', guard: 'проверка guard', summarize: 'сжатие истории',
  review: 'проверка быстрого ответа', fast: 'быстрый ответ', unknown: 'вызов LLM',
};
export const LLM_ROW_TITLE = 'Запрос к модели: раскрыть - что именно ушло модели (промпт и история)';
// раскрыт или свёрнут - значок шапки
export const toggleMark = open => open ? '▼' : '▶';

const SYSTEM_ROLE = 'system';
// системный промпт длинный и одинаковый ход за ходом - свёрнут, подмешанное в него видно карточками
const textHtml = (role, text) => !text ? '' : role === SYSTEM_ROLE
  ? `<details><summary class="t-dim">системный промпт, ${text.length} знаков</summary><pre>${esc(text)}</pre></details>`
  : `<pre>${esc(text)}</pre>`;

const msgHtml = m => {
  const {rest, cards} = splitCards(m.content);
  return `<div class="llm-req-msg"><span class="t-blue">${esc(m.role)}</span>` +
    (m.name ? ` <span class="t-dim">${esc(m.name)}</span>` : '') +
    (m.tool_calls ? ` <span class="t-tool">→ ${esc(m.tool_calls.join(', '))}</span>` : '') +
    cards.map(cardHtml).join('') + textHtml(m.role, rest) + `</div>`;
};

// Быстрая модель голоса (SAM-56) - циановым, остальные роли - приглушённо
export function llmRequestLine(ev) {
  const msgs = ev.messages || [];
  const tools = ev.tools || [];
  const role = ev.fast ? 'fast' : (ev.role || '?');
  const tone = ev.fast ? 't-cyan' : 't-muted';
  const sessTag = (ev.session && !ev.session.startsWith(TASK_PREFIX))
    ? ` <span class="t-dim">[${esc(ev.session)}]</span>` : '';
  const head =
    `<span class="llm-req-head"><span class="toggle t-dim">${toggleMark(false)}</span> ` +
    `<span class="${tone}"${ev.fast ? ' style="font-weight:600"' : ''}>→ ${esc(LLM_ROLE_LABEL[role] || role)}</span> ` +
    `<span class="llm-req-model t-dim">${esc(ev.model || '')}</span>` +
    `<span class="t-dim">: ${msgs.length} сообщений в контексте` + (tools.length ? `, инструментов ${tools.length}` : '') +
    `</span>${sessTag}</span>`;
  const body =
    `<div class="llm-req-body">${msgs.map(msgHtml).join('')}` +
    (tools.length ? `<div class="t-dim" style="font-size:12px">tools: ${esc(tools.join(', '))}</div>` : '') +
    `</div>`;
  return line('→', ev.fast ? 'dot-cyan' : 'dot-gray', head + body);
}
