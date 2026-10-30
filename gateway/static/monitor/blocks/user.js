/* Реплика человека: событие user_message и своё эхо поля ввода (картинки, ответ inject). */
import {esc} from '../format.js';
import {line} from './row.js';

const SPEAKER = 'человек';   // кто сказал, когда голос не опознан

// Почему запись скрыта от модели: решение адресата («фон - обрывок вне беседы; голос не узнан», SAM-98) или поправка
export function ignoreMark(ev) {
  return ev.ignore ? ` <span class="t-dim">[${esc(ev.ignore)}]</span>` : '';
}

export function userLine(ev) {
  return line(ev.command ? '/' : '👤', 'dot-amber',
    `<span class="t-amber" style="font-weight:600">${esc(ev.who || SPEAKER)}${ev.command ? ' · команда' : ''}:</span> ` +
    `<span class="t-amber">${esc(ev.content)}</span>` + ignoreMark(ev));
}

// SAM-241: реплика ушла, ход ещё не принял; текста нет (в нём может быть секрет, SAM-88), только пометка
export function pendingLine(imgPreview) {
  return line('👤', 'dot-amber',
    `<span class="t-amber" style="font-weight:600">${SPEAKER}:</span> <span class="t-dim">сообщение отправлено · в очереди</span>` + imgPreview);
}

// SAM-241: подтверждение не пришло (таймаут) или отправка упала; метка «в очереди» не висит вечно
export function pendingFailedLine(imgPreview) {
  return line('⚠', 'dot-red',
    `<span class="t-amber" style="font-weight:600">${SPEAKER}:</span> <span class="t-red">не подтверждено — ход не принял сообщение, проверь и отправь снова</span>` + imgPreview);
}

// превью картинок, ушедших с репликой; dataUrl собран страницей из файла (FileReader), не из сети
export function imagesPreview(images) {
  return images.length
    ? ' ' + images.map(i =>
        `<img src="${i.dataUrl}" style="height:40px;border-radius:3px;border:1px solid var(--border2);vertical-align:middle;margin:2px;"/>`
      ).join('')
    : '';
}

// Эхо своей реплики: текст приходит от шлюза уже с маской секрета (SAM-88); inject - сообщение агенту
export function echoLine(text, isTaskInject, imgPreview) {
  return line(isTaskInject ? '↓' : '👤', isTaskInject ? 'dot-cyan' : 'dot-amber',
    `<span class="${isTaskInject ? 't-cyan' : 't-amber'}" style="font-weight:600">${isTaskInject ? 'inject' : SPEAKER}:</span> ` +
    `<span class="t-amber">${esc(text)}</span>` + imgPreview);
}
