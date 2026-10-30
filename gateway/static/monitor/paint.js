/* Первый кадр после F5: уходя со страницы, вкладка сохраняет вид ленты (HTML последних строк) и рисует его до
   снимка шлюза; снимок потом заменяет ленту целиком. Только вид - событий и сведения с историей тут нет.
   sessionStorage - только эта вкладка; старше PAINT_TTL_MS не рисуется. */
import {state} from './state.js';
import {isTransientRowId} from './ids.js';
import {logEl, removeEmpty, scrollLog} from './log.js';

const PAINT_KEY = 'sam.monitor.paint';
const PAINT_ROWS = 80;                   // строк хватает на экран и запас для прокрутки вверх
const PAINT_TTL_MS = 10 * 60 * 1000;     // десять минут: за это время шлюз мог сжать или скрыть записи
const PAINT_KEY_OLD = 'sam.monitor.tail';   // кеш событий прежней версии - удаляется

// в вид не идут отладочные строки (тяжёлые, скрыты), скрытые фильтром, недописанный поток печати и «SAM думает»
const isPaintable = r => r.id !== 'log-older' && !r.classList.contains('ev-debug') && r.style.display !== 'none'
  && !isTransientRowId(r.id);

export function savePaint() {
  // кадр ленты одной сессии после F5 выглядел бы как общая лента с дырами
  if (state.filterSess) { try { sessionStorage.removeItem(PAINT_KEY); } catch {} return; }
  const rows = [...logEl().children].filter(isPaintable).slice(-PAINT_ROWS);
  try { sessionStorage.setItem(PAINT_KEY, JSON.stringify({saved: Date.now(), html: rows.map(r => r.outerHTML).join('')})); }
  catch { try { sessionStorage.removeItem(PAINT_KEY); } catch {} }   // не влез - старый кадр не рисуем
}

export function paintFromCache() {
  let cached = null;
  try { sessionStorage.removeItem(PAINT_KEY_OLD); cached = JSON.parse(sessionStorage.getItem(PAINT_KEY) || 'null'); } catch {}
  if (!cached || Date.now() - cached.saved > PAINT_TTL_MS || !cached.html) return;
  removeEmpty();
  logEl().insertAdjacentHTML('beforeend', cached.html);
  state.paintedFromCache = true;
  scrollLog();
}
