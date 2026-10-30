/* Рамка любого блока ленты: время | полоса-метка | текст. Блок отдаёт описание строки {mark, dot, html} - значок
   события, класс цвета метки и HTML тела; рамку из него собирает эта функция, узел - лента (feed.js). */

// Голос и мысль - lilac, реплики человека и SAM - крупнее (ev-talk); значок события - только полоса-метка цвета
const VOICE_MARKS = new Set(['🎤', '🎙', '🔊', '💭']);
const TALK_MARKS = new Set(['👤', '/', '🤖', '▤']);
const VOICE_DOT = 'dot-lilac';

export const rowClass = mark => TALK_MARKS.has(mark) ? 'ev ev-talk' : 'ev';
export const rowDot = (mark, dot) => VOICE_MARKS.has(mark) ? VOICE_DOT : dot;

export function rowHtml(ts, mark, dot, bodyHtml) {
  return `<span class="ev-ts">${ts}</span>` +
    `<span class="ev-dot ${rowDot(mark, dot)}">${mark}</span>` +
    `<span class="ev-body">${bodyHtml}</span>`;
}

// описание строки блока: значок, класс метки, тело
export const line = (mark, dot, html) => ({mark, dot, html});
