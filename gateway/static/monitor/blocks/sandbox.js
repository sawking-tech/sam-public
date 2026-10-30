/* Итог песочницы (SAM-83, ADR-016) и живого приложения (SAM-182, SAM-183): строка итога и страница во фрейме.
   Фрейм без allow-same-origin: страница не достаёт до API и cookie монитора. */
import {esc} from '../format.js';
import {line} from './row.js';

// превью - только со своих адресов шлюза: иначе фрейм открыл бы чужую страницу
const SANDBOX_PREVIEW_PREFIX = '/sandbox/', APP_PREVIEW_PREFIX = '/apps/';

// Песочница (ADR-016): метка сети запуска - интернет выдан или в нём отказано; без сети метки нет
export function sandboxNetMark(net) {
  if (!net || net.mode !== 'internet') return '';
  return net.granted ? ' <span class="t-amber">сеть: интернет</span>'
                     : ' <span class="t-red">сеть: отказ</span>';
}

const ownPreview = (preview, prefix) => (typeof preview === 'string' && preview.startsWith(prefix)) ? preview : '';
const previewHtml = prev =>
  ` <a class="t-blue" href="${esc(prev)}" target="_blank" rel="noopener">открыть</a>` +
  `<br><iframe sandbox="allow-scripts" src="${esc(prev)}" loading="lazy" ` +
  `style="width:100%;height:320px;border:1px solid var(--line);border-radius:var(--radius-sam);background:var(--panel);margin-top:4px"></iframe>`;
const outputHtml = text =>
  `<pre style="margin:4px 0 0 8px;white-space:pre-wrap;font-size:12px;color:var(--m-text)">${esc(text || '')}</pre>`;

export function sandboxLine(ev) {
  const ok = ev.code === 0;
  const prev = ownPreview(ev.preview, SANDBOX_PREVIEW_PREFIX);
  return line('📦', ok ? 'dot-green' : 'dot-amber',
    `<span class="${ok ? 't-green' : 't-amber'}">sandbox</span> <span class="t-muted">${esc(ev.job || '')}` +
    ` код ${esc(String(ev.code ?? '-'))}, файлов ${(ev.files || []).length}</span>` + sandboxNetMark(ev.network) +
    (prev ? previewHtml(prev) : '') +
    outputHtml(ev.text));
}

// создано или поправлено - страница во фрейме, как у песочницы; без страницы - вывод
export function appLine(ev) {
  const prev = ownPreview(ev.preview, APP_PREVIEW_PREFIX);
  return line('🧩', ev.ok ? 'dot-green' : 'dot-amber',
    `<span class="${ev.ok ? 't-green' : 't-amber'}">app</span> <span class="t-muted">${esc(ev.name || '')}` +
    ` ${esc(ev.app || '')} ${esc(ev.action || '')}</span>` +
    (prev ? previewHtml(prev) : outputHtml(ev.text)));
}
