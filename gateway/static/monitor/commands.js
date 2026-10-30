/* Команды по «/» (SAM-99): подбор подсказок к набранному и разбор строки-команды. Список даёт шлюз
   (GET /sam/commands): скиллы стенда и команды текущей основной модели; монитор ничего не зашивает. */
import {esc} from './format.js';

export const CMD_PREFIX = '/';

// Подсказки к набранному: строка «/» без пробела; сначала имена с этого начала, потом содержащие
export function matchCommands(list, value) {
  if (!value.startsWith(CMD_PREFIX) || /\s/.test(value)) return [];
  const q = value.slice(CMD_PREFIX.length).toLowerCase();
  const starts = list.filter(c => c.name.toLowerCase().startsWith(q));
  return starts.concat(list.filter(c => !starts.includes(c) && c.name.toLowerCase().includes(q)));
}

// Строка - команда, если первое слово после «/» - имя из списка; иначе это обычная фраза
export function parseCommand(value, list) {
  const m = /^\/(\S+)\s*([\s\S]*)$/.exec(value.trim());
  if (!m || !list.some(c => c.name === m[1])) return null;
  return {name: m[1], args: m[2]};
}

// пункты подсказки над строкой ввода; sel - выбранный
export function commandsHtml(matches, sel) {
  return matches.map((c, i) =>
    `<div class="cmd-item${i === sel ? ' sel' : ''}" data-i="${i}">` +
    `<span class="cmd-name">${esc(CMD_PREFIX + c.name)}</span><span class="cmd-src">${esc(c.source)}</span>` +
    `<span class="cmd-desc">${esc(c.description || '')}</span></div>`).join('');
}
