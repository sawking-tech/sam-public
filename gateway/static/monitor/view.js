/* Лента: всех сессий, одной сессии или одного агента. Выбор - селектором сессии или «глазом» задачи; назад -
   кнопкой над лентой, селектором или «назад» браузера. */
import {state, taskSections, taskInfo} from './state.js';
import {logEl, scrollLog} from './log.js';
import {filterLog} from './feed.js';
import {updateTaskHeader, expandTaskView, sectionSteps} from './sections.js';
import {pruneDetachedCalls} from './calls.js';
import {loadOlder} from './older.js';
import {sessSel, saveTarget, pruneTaskOptions, showTargetHint} from './sessions.js';
import {taskLive} from './tasks-model.js';
import {TASK_PREFIX, taskSession} from './ids.js';

const VIEW_MIN_ROWS = 30;    // меньше строк выбранной ленты на экране - подгрузить её раньше
const VIEW_FILL_PAGES = 10;  // не больше стольких порций за раз: дальше - прокруткой вверх

// История браузера: вход в ленту - своя запись (её «назад» возвращает), выход кнопкой - та же запись становится
// пустой (monStub) и следующий вход берёт её снова. history.back() не зовётся: его popstate приходил бы позже
// следующего выбора ленты и отменял его
export function setView(sess, fromHistory) {
  sess = sess || null;
  if (sess === state.filterSess) return;
  const leaving = state.filterSess;
  dropViewRows();
  state.viewCursor = sess
    ? {before: state.oldestIso || new Date().toISOString(), before_id: state.oldestId, done: false} : null;
  filterLog(sess);
  if (!sess) {
    // адресат остаётся, кроме закончившегося агента, выбранного ради его ленты: фраза оживила бы его; пункты
    // закончившихся агентов уходят
    const sel = sessSel();
    if (sel.value === leaving && leaving.startsWith(TASK_PREFIX) && !taskLive(leaving.slice(TASK_PREFIX.length))) {
      sel.value = ''; saveTarget();
    }
    pruneTaskOptions();
  }
  showViewBar();
  showTargetHint();
  if (!fromHistory) {
    const st = history.state || {};
    if (sess) history[st.monView || st.monStub ? 'replaceState' : 'pushState']({monView: sess}, '');
    else if (st.monView) history.replaceState({monStub: true}, '');
  }
  fillView();
}

export function viewTaskSession(taskId) {
  setView(taskSession(taskId));
}

// строки, подгруженные для прежней ленты одной сессии, в общей ленте были бы не на своём месте
function dropViewRows() {
  const touched = new Set();
  document.querySelectorAll('#log [data-view-only]').forEach(el => {
    const sec = el.closest('.task-section');
    if (el.classList.contains('task-section')) delete taskSections[el.dataset.taskId];
    else if (sec && taskSections[sec.dataset.taskId]) { taskSections[sec.dataset.taskId].count--; touched.add(sec.dataset.taskId); }
    el.remove();
  });
  touched.forEach(updateTaskHeader);
  pruneDetachedCalls();
}

function viewRowCount() {
  const sess = state.filterSess;
  if (!sess) return 0;
  if (sess.startsWith(TASK_PREFIX)) return sectionSteps(sess);
  return [...logEl().children].filter(el => el.style.display !== 'none' && el.dataset.sess === sess).length;
}

async function fillView() {
  const view = state.filterSess;
  for (let i = 0; i < VIEW_FILL_PAGES && view && view === state.filterSess && state.viewCursor && !state.viewCursor.done
       && viewRowCount() < VIEW_MIN_ROWS; i++) {
    if (state.olderLoading) break;   // идёт подгрузка прокруткой - она и наполнит
    await loadOlder();
  }
  expandTaskView();
  showViewBar();
  if (state.autoScroll) scrollLog();
}

export function showViewBar() {
  const bar = document.getElementById('view-bar');
  const sess = state.filterSess;
  bar.hidden = !sess;
  if (!sess) return;
  let text;
  if (sess.startsWith(TASK_PREFIX)) {
    const key = sess.slice(TASK_PREFIX.length);
    const t = taskInfo[key];
    text = `агент ${t && t.name ? t.name : key}` + (t && t.status ? ` · ${t.status}` : '') +
      (viewRowCount() ? '' : ' · шагов в истории нет');
  } else {
    text = `сессия ${sess} · реплика уходит сюда` + (viewRowCount() ? '' : ' · записей в истории нет');
  }
  document.getElementById('view-label').textContent = text;
}
