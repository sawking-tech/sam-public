/* Узел ленты: прокрутка вниз, удержание низа, появление новой строки, заглушка «жду событий». */
import {state} from './state.js';

export const logEl = () => document.getElementById('log');
export const olderMark = () => document.getElementById('log-older');   // скелет «загружаю раньше»

export function scrollLog() {
  const log = logEl();
  log.scrollTop = log.scrollHeight;
}

export function removeEmpty() {
  const e = document.getElementById('log-empty'); if (e) e.remove();
}

/* Появление нового события снизу; пачка за кадр - лесенкой по 30 мс, не больше 5 ступеней (sam.css, .sam-in) */
const ENTER_STEP_MS = 30;
export const ENTER_MAX_STEPS = 5;
let enterBatch = 0;
export function enter(el, delayIndex) {
  if (!el || !el.classList) return;
  const i = delayIndex ?? Math.min(enterBatch++, ENTER_MAX_STEPS - 1);
  if (delayIndex === undefined && enterBatch === 1) requestAnimationFrame(() => { enterBatch = 0; });
  el.style.animationDelay = `${i * ENTER_STEP_MS}ms`;
  el.classList.add('sam-in');
  el.addEventListener('animationend', () => el.classList.remove('sam-in'), {once: true});
}

/* Лента держит низ, пока человек не ушёл вверх: каркас достраивает раскладку, шрифты и картинки догружаются уже
   после отрисовки, и высота ленты меняется - без этого после F5 лента на кадр встаёт наверх и прыгает вниз */
const BOTTOM_SLACK_PX = 40;   // ближе к низу - считается «внизу»
export function holdBottom() { if (state.autoScroll && state.atBottom) scrollLog(); }
export function trackBottom(log) {
  state.atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < BOTTOM_SLACK_PX;
}
export function watchBottom() {
  new ResizeObserver(holdBottom).observe(logEl());
  new MutationObserver(holdBottom).observe(logEl(), {childList: true});
  document.fonts && document.fonts.ready.then(holdBottom);
  addEventListener('load', holdBottom);
}
