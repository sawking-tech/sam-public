/* Общие преобразования текста для блоков ленты: экранирование, обрезка, аргументы вызова, время. Чистые функции:
   ни DOM, ни сети - их гоняют тесты в node (tests/js/monitor). */

export function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Аргумент для onclick="f(...)": браузер сначала декодирует HTML-сущности, потом
// разбирает JS, поэтому строка - JS-литерал через JSON.stringify, затем esc.
// esc без JSON ломался на кавычке в session id из X-Session-ID (SAM-72).
export function jsArg(s) {
  return esc(JSON.stringify(String(s == null ? '' : s)));
}

export function trim(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }

// Длинный текст - начало и «…», остаток уже в строке и разворачивается щелчком (обработчик - на ленте, main.js)
export function clip(s, n) {
  s = String(s ?? '');
  if (s.length <= n) return esc(s);
  return `<span class="clip" title="развернуть или свернуть">${esc(s.slice(0, n))}<span class="clip-more">…</span>` +
    `<span class="clip-rest">${esc(s.slice(n))}</span></span>`;
}

// аргумент вызова в строке ленты - начало строкового значения и JSON остального; целиком - разворотом
export const ARG_CLIP_CHARS = 120, ARG_JSON_CLIP_CHARS = 160;
export function fmtArgs(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args).map(([k, v]) => {
    const vs = typeof v === 'string'
      ? `<span class="t-arg">"${clip(v, ARG_CLIP_CHARS)}"</span>`
      : `<span class="t-arg">${clip(JSON.stringify(v), ARG_JSON_CLIP_CHARS)}</span>`;
    return `<span class="t-dim">${esc(k)}:</span>\u00a0${vs}`;
  }).join('<span class="t-dim">, </span>');
}

// «1 сообщение, 2 сообщения, 5 сообщений»
export function ruPlural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  return m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

// Время строки: событие с ts_iso (UTC) - местное время, иначе готовая строка HH:MM:SS или поле ts
export function fmtTs(tsOrEv) {
  if (tsOrEv && typeof tsOrEv === 'object' && tsOrEv.ts_iso) {
    const d = new Date(tsOrEv.ts_iso);
    if (!isNaN(d)) {
      return d.toLocaleTimeString('ru-RU', {hour12: false});
    }
  }
  return (typeof tsOrEv === 'string') ? tsOrEv : (tsOrEv && tsOrEv.ts) || '';
}

// время строки, которую кладёт сама страница (итог кнопки, отказ шлюза): HH:MM:SS местного
const CLOCK_CHARS = 8;
export function clockNow() { return new Date().toTimeString().slice(0, CLOCK_CHARS); }
