/* Полоса монитора: связь, счётчик событий, кто ведёт, сжатие истории, «история не сохраняется». */
import {getJson} from './api.js';
import {compressionHeader} from './blocks/compression.js';

export function setConn(ok) {
  document.getElementById('conn-dot').className = ok ? 'sam-live on' : 'sam-live';
  document.getElementById('conn-lbl').textContent = ok ? 'на связи' : 'нет связи';
}

// счётчик событий; мигает на живом событии, не на снимке
export function countEvent(n, live) {
  const counter = document.getElementById('ev-count');
  counter.textContent = n;
  if (live) {
    counter.classList.remove('sam-blink'); void counter.offsetWidth; counter.classList.add('sam-blink');
  }
}

// SAM-91: ярлык «кто ведёт» - со старта из /health, дальше по событию main_model_switch
export function setMainModel(name) {
  const el = document.getElementById('main-model');
  if (el && name) el.textContent = `ведёт: ${name}`;
}

// SAM-97: сжатие в шапке - из /health, поэтому ожидание и ошибка видны и после F5. Несжатый хвост растёт с каждой
// репликой, а /health опрашивает компоненты: раз в минуту хватает, чтобы увидеть вставшее сжатие
export const COMPRESSION_REFRESH_MS = 60000;
export function setCompression(c) {
  const el = document.getElementById('compression');
  if (!el || !c) return;
  const head = compressionHeader(c);
  el.textContent = head.text;
  el.className = head.cls;
  el.title = head.title;
}
export function loadHealth() {
  getJson('/health').then(h => { setMainModel(h.main_model); setCompression(h.compression); }).catch(() => {});
}
export function refreshCompression() {
  getJson('/health').then(h => setCompression(h.compression)).catch(() => {});
}

// отладка выключает запись истории - это видно всегда, а не только в настройках; включить её может и SAM
// (tune_self), и другая вкладка, поэтому полоса сверяется с шлюзом сама
export const HISTORY_OFF_POLL_MS = 30000;
export function showHistoryOff(on) { document.getElementById('history-off').hidden = !on; }
export function refreshHistoryOff() {
  getJson('/sam/config').then(c => showHistoryOff(c.debug_mode)).catch(() => {});
}
