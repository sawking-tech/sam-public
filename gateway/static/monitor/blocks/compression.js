/* Сжатие истории (SAM-36, SAM-97): строка в шапке из /health, ход сжатия одной строкой на сессию, сводка на месте
   сжатых сообщений, ожидание паузы и ошибка. */
import {esc, ruPlural} from '../format.js';
import {line} from './row.js';

const SECONDS_PER_MIN = 60;

export function compressionText(sid, s) {
  const parts = [`${sid}: ${s.pending_msgs} ${ruPlural(s.pending_msgs, 'сообщение', 'сообщения', 'сообщений')} без сжатия`];
  if (s.last_ok_ago_s != null) {
    const min = Math.round(s.last_ok_ago_s / SECONDS_PER_MIN);
    parts.push(min < 1 ? 'сжато только что' : `последнее сжатие ${min} мин назад`);
  } else parts.push('сжатий ещё не было');
  if (s.waiting_s != null) parts.push(`ждёт паузы ${s.waiting_s} с`);
  if (s.running_s != null) parts.push(`идёт ${s.running_s} с`);
  if (s.last_error) parts.push('ошибка');
  return parts.join(', ');
}

// Шапка: текст по сессиям, красный - ошибка или потолок без идущего сжатия, подсказка - ошибки или потолки
export function compressionHeader(c) {
  const sessions = Object.entries(c.sessions || {});
  const bad = sessions.some(([, s]) => s.last_error || (s.above_ceiling && s.running_s == null));
  return {
    text: sessions.length ? 'сжатие ' + sessions.map(([sid, s]) => compressionText(sid, s)).join(' · ') : '',
    cls: bad ? 't-red' : 't-dim',
    title: sessions.filter(([, s]) => s.last_error)
      .map(([sid, s]) => `${sid}: ${s.last_error} (${s.last_error_at})`).join('\n') ||
      `Сжатие истории (SAM-97): потолок ${c.ceiling_msgs} сообщений или ${c.ceiling_tokens} токенов несжатого`,
  };
}

// ход сжатия: перед каждым вызовом сводки (gateway/compression.py)
export function compressionProgressHtml(ev) {
  const step = ev.stage === 'merge'
    ? `слияние сводок, вызов ${ev.call}`
    : `${ev.done} из ${ev.total} сообщ., вызов ${ev.call} из ${ev.calls}`;
  return `<span class="t-dim">сжатие ${esc(ev.session || '')}</span> <span class="t-muted">${esc(step)}</span>`;
}
export const progressLine = () => line('⊡', 'dot-amber', '');

// конец хода сжатия: класс метки и тело
export function compressionDone(sid, error, calls) {
  return {
    dot: error ? 'dot-red' : 'dot-muted',
    html: `<span class="t-dim">сжатие ${esc(sid || '')}</span> ` +
      `<span class="${error ? 't-red' : 't-muted'}">${error ? 'не удалось' : 'готово'}, вызовов ${calls}</span>`,
  };
}

// Фоновое сжатие упало (SAM-62): несжатое осталось в истории, следующая реплика поставит снова
export function compressionErrorLine(ev) {
  return line('⊡', 'dot-red',
    `<span class="t-red">сжатие не удалось</span> <span class="t-muted">${esc(ev.error || '')}</span>`);
}

// SAM-97: сжатие поставлено и ждёт паузы в разговоре, не дольше max_wait_s
export function compressionWaitingLine(ev) {
  return line('⊡', 'dot-muted',
    `<span class="t-dim">сжатие ждёт паузы</span> <span class="t-muted">не дольше ${esc(String(ev.max_wait_s ?? '-'))} с</span>`);
}

// сжатая история хранит реплики с ролями и мета-тегами модели: человеку - текст с подписью, без тегов
export function cleanSummary(text) {
  return String(text || '').replace(/<\/?[a-z_]+>/gi, '')
    .replace(/\bassistant:\s*/g, 'SAM: ').replace(/\buser:\s*/g, 'Человек: ');
}

export const SUMMARY_MARK = '⊡', SUMMARY_DOT = 'dot-muted';
export function summaryHtml(s) {
  return `<span class="t-dim">[сжато ${s.time_range || ''}]</span>` +
    `<br><span class="ev-res-body t-muted" style="padding-left:2ch">${esc(cleanSummary(s.content))}</span>`;
}
export const summaryLine = s => line(SUMMARY_MARK, SUMMARY_DOT, summaryHtml(s));
