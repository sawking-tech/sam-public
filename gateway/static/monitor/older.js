/* Старые записи - порцией сверху (/sam/monitor/history), без сдвига того, что на экране. Порция идёт через тот же
   разбор событий, что живая лента: обработчик передаёт корень сборки (setEventHandler). */
import {state} from './state.js';
import {logEl, olderMark} from './log.js';
import {resetOlderAnchors} from './sections.js';

export const OLDER_EDGE_PX = 120;   // столько до верха ленты - пора подгрузить раньше
let handleEvent = () => {};
export const setEventHandler = fn => { handleEvent = fn; };

export async function loadOlder() {
  const view = state.filterSess, cur = view ? state.viewCursor : null;
  if (state.olderLoading || state.logTarget) return;   // logTarget - идёт снимок, он заменит ленту
  if (view ? (!cur || cur.done) : (state.olderDone || !state.oldestIso)) return;
  state.olderLoading = true;
  const gen = state.feedGen;
  const log = logEl(), mark = olderMark();
  mark.hidden = false;
  try {
    const q = new URLSearchParams({before: cur ? cur.before : state.oldestIso});
    const bid = cur ? cur.before_id : state.oldestId;
    if (bid) q.set('before_id', bid);
    if (view) q.set('session', view);
    const r = await fetch(`/sam/monitor/history?${q}`);
    const page = await r.json();
    // пока ждали ответ, пришёл снимок или сменилась лента: порция уже не к этой ленте
    if (state.logTarget || gen !== state.feedGen || view !== state.filterSess) return;
    state.olderBusy = true;        // только на время разбора порции: живые события в ожидании ответа идут как обычно
    state.viewLoad = view;
    resetOlderAnchors();
    const before = log.scrollHeight;
    state.logTarget = document.createDocumentFragment();
    for (const ev of page.events || []) handleEvent(ev);
    const frag = state.logTarget;
    state.logTarget = null;
    if (view) for (const el of frag.children) el.dataset.viewOnly = '1';
    mark.after(frag);
    log.scrollTop += log.scrollHeight - before;
    // конец истории - порция без записей; порция из записей без событий (тело скилла) - не конец
    if (cur) Object.assign(cur, {before: page.oldest || cur.before, before_id: page.oldest_id || '', done: !page.oldest});
    else { state.oldestIso = page.oldest || state.oldestIso; state.oldestId = page.oldest_id || ''; state.olderDone = !page.oldest; }
  } catch (e) {
    console.warn('история не подгрузилась', e);
  } finally {
    state.olderBusy = false; state.olderLoading = false; state.viewLoad = null;
    mark.hidden = true;
  }
}
