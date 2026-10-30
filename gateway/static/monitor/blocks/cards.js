/* Подмешанное в запрос к модели (SAM-230): память по фразе, давнее из архива и тело загруженного скилла - карточками,
   чтобы в раскрытом запросе было видно, что модель получила сверх диалога. Заголовки - те же строки, что пишет шлюз:
   gateway/runner.py RAG_HEADER, gateway/recall_history.py ARCHIVE_HEADER, gateway/runner.py LINGER_HEADER (сверка -
   tests/test_monitor_modules.py). Чистые функции на строках. */
import {esc} from '../format.js';

export const RAG_HEADER = 'Из памяти (используй активно):';
export const ARCHIVE_HEADER = 'Из прошлого разговора (архив, дословно):';
export const SKILL_HEADER_RE = /^\[итог (load_skill)\]\n/;
const CARD_TITLES = {[RAG_HEADER]: 'память по фразе', [ARCHIVE_HEADER]: 'давнее из архива'};
const BLOCK_SEP = '\n\n';   // блоки промпта шлюз склеивает пустой строкой (runner.py)

// {rest, cards}: блоки с известным заголовком - карточки, остальной текст - как был
export function splitCards(content) {
  const text = String(content || '');
  const skill = text.match(SKILL_HEADER_RE);
  if (skill) return {rest: '', cards: [{title: `скилл (${skill[1]})`, body: text.slice(skill[0].length)}]};
  const cards = [], rest = [];
  for (const block of text.split(BLOCK_SEP)) {
    const header = Object.keys(CARD_TITLES).find(h => block.startsWith(h + '\n'));
    if (header) cards.push({title: CARD_TITLES[header], body: block.slice(header.length + 1)});
    else rest.push(block);
  }
  return {rest: rest.join(BLOCK_SEP), cards};
}

export const cardHtml = c =>
  `<div class="llm-card"><span class="t-tool">${esc(c.title)}</span><pre>${esc(c.body)}</pre></div>`;
