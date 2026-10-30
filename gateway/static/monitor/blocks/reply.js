/* Реплика SAM: ответ хода (assistant_reply), прозвучавшее без ответа в ленте (assistant_spoken), текст без озвучки
   (канал text) и строка живой печати потока. Сырой ответ разбирается как в gateway/speech_meta.py. */
import {esc, clip} from '../format.js';
import {line} from './row.js';

// Подпись ответа по источнику (gateway/voice_turn.py SOURCE_*); говорит всегда SAM, пометка - кто ответил
const SPEAKER_LABELS = {fast: 'SAM · быстрая', main: 'SAM · основная'};
// Канал реплики SAM (gateway/repos/session_repo.py TEXT_CHANNEL): текстом в монитор, без озвучки
export const TEXT_CHANNEL = 'text';

// Судьба реплики (ADR-013): прозвучала - динамик, отчёта моста нет - динамик с вопросиком
export const FATE_UNKNOWN = 'unknown';
const FATE_MATCH_CHARS = 20;   // начало речи, по которому отчёт находит свой ответ в ленте
const SERVICE_MARK_RE = /\s*\[[^\[\]]*\]/g;

// значок «прозвучало» - SVG динамика (эмодзи не значок: шрифт решает, как он выглядит)
export const SPEAKER_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h2.5L9 3v10L5.5 10H3z"/><path d="M11.5 5.5a3.5 3.5 0 0 1 0 5"/></svg>';
export function fateIcon(fate) {
  if (fate === FATE_UNKNOWN) {
    return `<span class="fate-icon fate-unknown" title="судьба неизвестна: мост не отчитался">${SPEAKER_SVG}<sup>?</sup></span>`;
  }
  return `<span class="fate-icon"${fate ? ' title="прозвучало"' : ''}>${SPEAKER_SVG}</span>`;
}

// Начало прозвучавшего без служебных пометок: по нему отчёт моста находит ответ, ждущий судьбы. Мост отчитывается
// по порядку ответов, а оборванный ответ прозвучал только началом
export const fateHead = voice => voice.replace(SERVICE_MARK_RE, '').trim().slice(0, FATE_MATCH_CHARS);

// Текст без озвучки (note основной модели) - как есть: пробелы и переводы строк держат схему
export function renderNote(text) {
  return `<pre class="text-note">${esc(text)}</pre>`;
}

// Что звучит - как gateway/speech_meta.py (ADR-003, пересмотр 14 окт): весь ответ, кроме парных блоков <имя>...</имя>;
// незакрытый не звучит до конца только у рассуждения, вызова и молчания; <voice> старых записей снимается, текст - речь
const LEGACY_VOICE_RE = /<\/?voice\b[^<>]*>/gi;
const META_RE = /<([a-z][\w:.-]*)(?:\s[^<>]*)?(?<!\/)>([\s\S]*?)<\/\1\s*>|<(think|tool_call|silent|quiet)(?:\s[^<>]*)?(?<!\/)>([\s\S]*)$/gi;
// мета-блок в строке ответа - начало, целиком он в событии
const META_PREVIEW_CHARS = 150;

export function renderMeta(name, inner) {
  inner = inner.trim();
  if (name === 'silent' || name === 'quiet') {
    return `<span class="t-dim">[не звучит] ${inner ? esc(inner) : 'молчит'}</span> `;
  }
  if (!inner) return '';
  return `<span class="t-dim">[${esc(name)}] ${clip(inner, META_PREVIEW_CHARS)}</span> `;
}

export function renderRaw(text, fate) {
  if (!text) return '';
  text = text.replace(LEGACY_VOICE_RE, '');
  const pieces = [];
  let last = 0;
  for (const m of text.matchAll(META_RE)) {
    pieces.push({speech: text.slice(last, m.index)});
    pieces.push({meta: renderMeta((m[1] || m[3]).toLowerCase(), m[2] ?? m[4] ?? '')});
    last = m.index + m[0].length;
  }
  pieces.push({speech: text.slice(last)});
  let html = '', icon = fateIcon(fate) + ' ';
  for (const p of pieces) {
    if (p.meta !== undefined) { html += p.meta; continue; }
    if (!p.speech.trim()) continue;
    html += `<span class="t-green">${icon}${esc(p.speech)}</span>`;   // значок судьбы - у первого куска речи
    icon = '';
  }
  return html;
}

const samName = label => `<span class="t-green" style="font-weight:600">${label}:</span> `;

// Ответ хода. Без озвучки и системный (агент поставил задачу, текст не звучит) - приглушён: muted
export function replyLine(ev) {
  const muted = ev.voice == null && ev.system === true;
  const isNote = ev.channel === TEXT_CHANNEL;
  const label = isNote ? 'SAM · текст' : (SPEAKER_LABELS[ev.source] || 'SAM');
  return {
    ...line(isNote ? '▤' : '🤖', muted ? 'dot-muted' : 'dot-green',
      samName(`${label}${ev.model ? ' · ' + esc(ev.model) : ''}`) +
      (isNote ? renderNote(ev.content) : renderRaw(ev.content, ev.fate))),
    muted,
  };
}

// Прозвучавшее, ответа которого в ленте нет (поправка проверки, своя реплика)
export function spokenLine(ev) {
  return line('🤖', 'dot-green', samName('SAM') + renderRaw(ev.content, ev.fate));
}

// Живая печать потока (SAM-09): буфер дописывают дельты, курсор снимает конец потока
export function streamLine() {
  return line('🤖', 'dot-green',
    samName('SAM') + `<span class="stream-buf"></span><span class="stream-cursor" style="opacity:.6">▍</span>`);
}
