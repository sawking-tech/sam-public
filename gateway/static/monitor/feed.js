/* Лента: строка из описания блока, добавление (в секцию агента, во фрагмент снимка или в ленту), фильтр по сессии,
   сброс и очистка. Все вставки строк в ленту идут отсюда. */
import {state, busySessions, taskSections, pendingCalls, callByName, compressionRows, callRows, guardMarks, clearAll}
  from './state.js';
import {logEl, olderMark, scrollLog, enter} from './log.js';
import {routeToTaskSection, expandTaskView} from './sections.js';
import {rowClass, rowHtml} from './blocks/row.js';
import {TASK_PREFIX, isTransientRowId} from './ids.js';

export function makeRow(ts, mark, dot, bodyHtml, sess, mid) {
  const d = document.createElement('div');
  d.className = rowClass(mark);
  if (sess) d.dataset.sess = sess;
  if (mid) d.dataset.mid = mid;
  d.innerHTML = rowHtml(ts, mark, dot, bodyHtml);
  return d;
}

// строка из описания блока {mark, dot, html}
export const blockRow = (ts, spec, sess, mid) => makeRow(ts, spec.mark, spec.dot, spec.html, sess, mid);

export function append(el) {
  // строка агента уходит в его секцию
  if (routeToTaskSection(el)) return;

  const log = state.logTarget || logEl();
  log.appendChild(el);
  if (state.filterSess) {
    const s = el.dataset && el.dataset.sess;
    if (s !== state.filterSess) el.style.display = 'none';
  }
  if (!state.logTarget) enter(el);
  if (state.autoScroll && !state.logTarget) scrollLog();
}

export function addDiv() {
  const d = document.createElement('div'); d.className = 'ev-div'; append(d);
}

// SAM-92: запись с пометкой ignore (после F5 приходит полем replay) - скрыть сразу
export function markIgnored(row, ev) {
  if (row && ev.ignore) row.classList.add('ignored');
  return row;
}

/* ── Фильтр по сессии ── */
export function filterLog(sess) {
  state.filterSess = sess || null;
  const filterSess = state.filterSess;
  const isTaskFilter = filterSess && filterSess.startsWith(TASK_PREFIX);

  Array.from(logEl().children).forEach(el => {
    if (el.id === 'log-older') return;   // скелет «загружаю раньше» - свой hidden
    if (!filterSess) {
      el.style.display = '';
      return;
    }
    const s = el.dataset && el.dataset.sess;
    if (isTaskFilter) {
      // лента агента - только его секция
      el.style.display = (s === filterSess) ? '' : 'none';
    } else if (el.classList && el.classList.contains('task-section')) {
      // лента сессии - её строки и строки без сессии; секции агентов - только явным выбором
      el.style.display = 'none';
    } else {
      el.style.display = (!s || s === filterSess) ? '' : 'none';
    }
  });

  if (isTaskFilter) expandTaskView();

  if (state.autoScroll) scrollLog();
}

/* ── Сброс ── */
// состояние ленты: ссылки на секции агентов, ожидающие итога вызовы, строки сжатия - вместе с лентой
export function resetFeed() {
  clearAll(taskSections, pendingCalls, callByName, compressionRows, callRows, guardMarks);
  state.evN = 0;   // seenMids не трогаем: очищенная лента не должна наполниться снимком после обрыва
}

// после обрыва строки «идёт сейчас» уже неправда: ход мог закончиться, итог вызова - потеряться
export function dropTransient() {
  [...logEl().querySelectorAll('[id]')].filter(r => isTransientRowId(r.id)).forEach(r => r.remove());
  busySessions.clear();
  if (window.SAMShell) SAMShell.setBusy(false);
  for (const [cid, c] of Object.entries(pendingCalls)) { if (c.dotEl) c.dotEl.className = 'ev-dot dot-muted'; delete pendingCalls[cid]; }
  clearAll(callByName, callRows, guardMarks);
}

export function clearLog() {
  logEl().replaceChildren(olderMark());
  resetFeed();
  // пустая лента стоит прокрученной в самый верх - без этого подгрузка старых тут же наполнила бы её снова;
  // история вернётся после F5
  state.olderDone = true;
  document.getElementById('ev-count').textContent = '';
}

export function toggleAuto() {
  state.autoScroll = document.getElementById('auto-btn').checked;
  if (state.autoScroll) scrollLog();
}
