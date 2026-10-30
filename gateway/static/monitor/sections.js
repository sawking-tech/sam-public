/* Секции агентов в ленте: все события задачи сворачиваются в группу с шапкой (имя, статус, шаг, итог, счётчик). */
import {state, taskSections, taskInfo} from './state.js';
import {logEl, scrollLog} from './log.js';
import {TASK_PREFIX, taskKey, sessionTaskKey} from './ids.js';
import {taskHeaderHtml, taskHeaderTitle, stepsText, ANSWER_PREFIX_RE} from './blocks/agent.js';

const FINISHED = ['done', 'failed', 'cancelled'];
// подгрузка старых: секция агента -> её первый шаг до подгрузки, ранние шаги встают перед ним
const olderAnchors = new Map();
export const resetOlderAnchors = () => olderAnchors.clear();

const isExpanded = e => e.body.style.display !== 'none';
const setToggle = (e, expanded) => { e.header.querySelector('.toggle').textContent = expanded ? '▼' : '▶'; };

export function ensureTaskSection(taskId, descr) {
  const key = taskKey(taskId);
  if (taskSections[key]) {
    if (descr && !taskSections[key].descr) {
      taskSections[key].descr = descr;
      updateTaskHeader(key);
    }
    return taskSections[key];
  }

  const section = document.createElement('div');
  section.className = 'task-section';
  section.dataset.taskId = key;
  section.dataset.sess = `${TASK_PREFIX}${key}`;  // для filterLog

  const header = document.createElement('div');
  header.className = 'task-section-header';

  const body = document.createElement('div');
  body.className = 'task-section-body';
  body.style.display = 'none';

  section.appendChild(header);
  section.appendChild(body);
  const entry = {
    section, header, body, count: 0, name: '',
    // задача из истории: её конца в снимке нет, крутилка «идёт» висела бы вечно - нейтральная точка до записи задачи
    descr: descr || '', status: state.logTarget ? 'past' : 'running', stage: '',
    last_msg: '', final: '',
  };
  // раскрыта ли - по самой секции: лента агента раскрывает её извне (expandTaskView)
  header.onclick = () => {
    const expanded = !isExpanded(entry);
    body.style.display = expanded ? '' : 'none';
    setToggle(entry, expanded);
  };

  // туда же, куда строки: при снимке и кеше - во фрагмент, иначе секция встаёт не на своё место в ленте
  const replaying = !!state.logTarget;
  // включён фильтр сессии - новая секция подчиняется ему, как остальные (filterLog)
  if (state.filterSess) section.style.display = state.filterSess === `${TASK_PREFIX}${key}` ? '' : 'none';
  (state.logTarget || logEl()).appendChild(section);
  if (state.autoScroll && !replaying) scrollLog();

  // строки подгрузки ленты агента уходят вместе с ней (dropViewRows)
  if (state.viewLoad) section.dataset.viewOnly = '1';
  taskSections[key] = entry;
  applyTaskInfo(key);
  return entry;
}

// Запись задачи (/sam/tasks) - правда о статусе и итоге: события task_* в историю не пишутся, после F5 их нет
export function applyTaskInfo(key) {
  const e = taskSections[key], t = taskInfo[key];
  if (e && t) {
    e.name = t.name || e.name;
    e.descr = t.description || e.descr;
    if (t.status) e.status = t.status;
    const res = t.error || t.result;
    if (res && FINISHED.includes(t.status)) e.final = String(res).replace(ANSWER_PREFIX_RE, '');
  }
  updateTaskHeader(key);
}

export function updateTaskHeader(key) {
  const e = taskSections[key];
  if (!e) return;
  e.header.innerHTML = taskHeaderHtml(key, e, isExpanded(e));
  e.header.title = taskHeaderTitle(key, e);
}

// Новое о задаче из живого события: имя, статус, шаг, последнее сообщение, итог
export function updateTaskStatus(taskId, opts) {
  const key = taskKey(taskId);
  const e = taskSections[key] || ensureTaskSection(taskId);
  if (opts.descr) e.descr = opts.descr;
  if (opts.name) e.name = opts.name;
  if (opts.status) e.status = opts.status;
  if (opts.stage !== undefined) e.stage = opts.stage;
  if (opts.last_msg) e.last_msg = opts.last_msg;
  if (opts.final) e.final = opts.final;
  updateTaskHeader(key);
}

// Строка сессии агента (data-sess="task:...") уходит в его секцию; false - строка не агента
export function routeToTaskSection(el) {
  const sess = el.dataset && el.dataset.sess || '';
  const key = sess.startsWith(TASK_PREFIX) ? sessionTaskKey(sess) : null;
  if (!key) return false;

  // секции ещё нет (снимок или задача без task_created) - заводится сейчас
  const tg = taskSections[key] || ensureTaskSection(key);

  if (state.viewLoad && !tg.section.dataset.viewOnly) el.dataset.viewOnly = '1';
  if (!state.viewLoad && !state.logTarget && tg.section.dataset.viewOnly) keepTaskSection(tg);
  // подгрузка старых: ранние шаги агента встают перед уже показанными, в своём порядке
  if (state.olderBusy) {
    if (!olderAnchors.has(key)) olderAnchors.set(key, tg.body.firstChild);
    tg.body.insertBefore(el, olderAnchors.get(key));
  } else tg.body.appendChild(el);
  tg.count += 1;
  tg.header.querySelector('.count').textContent = stepsText(tg.count);
  if (state.autoScroll && !state.logTarget) scrollLog();
  return true;
}

// Агент из подгрузки ленты ожил (живой шаг): секция переходит в общую ленту - в конец, где идёт сейчас; уходят
// только подгруженные шаги
function keepTaskSection(tg) {
  delete tg.section.dataset.viewOnly;
  for (const el of tg.body.children) el.dataset.viewOnly = '1';
  logEl().appendChild(tg.section);
}

// лента агента - его шаги раскрыты; секция могла появиться только с подгрузкой его истории
export function expandTaskView() {
  if (!state.filterSess || !state.filterSess.startsWith(TASK_PREFIX)) return;
  const tg = taskSections[sessionTaskKey(state.filterSess)];
  if (!tg) return;
  tg.section.style.display = '';
  tg.body.style.display = '';
  const toggle = tg.header.querySelector('.toggle');
  if (toggle) toggle.textContent = '▼';
}

// шагов выбранного агента в ленте: мало - лента агента подгружает его историю
export function sectionSteps(sess) {
  const tg = taskSections[sessionTaskKey(sess)];
  return tg ? tg.body.querySelectorAll('.ev').length : 0;
}
