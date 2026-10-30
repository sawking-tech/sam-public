/* Селектор сессии внизу: чья лента видна и куда уходит реплика. Вид сессии - от шлюза (gateway/session_kinds.py),
   группы - люди, агенты, проверки; свежие сверху. Закончившиеся агенты в селекторе не стоят (их лента - «глазом»
   задачи), устаревшие проверки свёрнуты в один пункт «показать». */
import {state, sessionInfo, tasks, taskInfo} from './state.js';
import {getJson} from './api.js';
import {TASK_PREFIX, taskSession} from './ids.js';
import {taskLive} from './tasks-model.js';

const SESSION_GROUPS = [['person', 'Люди'], ['agent', 'Агенты'], ['check', 'Проверки']];
const KIND_AGENT = 'agent', KIND_CHECK = 'check';
export const SHOW_STALE = '__stale__';   // пункт селектора «показать устаревшие», не сессия
export const TARGET_KEY = 'sam_session';   // выбор адресата переживает F5
const view = {showStale: false, shown: '', sig: ''};   // раскрыты ли устаревшие, выбранное, состав списка

export const sessSel = () => document.getElementById('sess-sel');
// адресат реплики: выбранная сессия, при «все сессии» - сессия по умолчанию
export const getSess = () => sessSel().value || state.defaultSession;

export function trackSess(s) {
  // агенты - только явно (trackActiveTask при живом task_created): иначе снимок заполнил бы селектор прошлыми
  if (sessionInfo[s] || s.startsWith(TASK_PREFIX)) return;
  sessionInfo[s] = {kind: KIND_CHECK, last: new Date().toISOString(), count: 0, stale: false};
  renderSessionSelect();
}

export function trackActiveTask(taskId) {
  const s = taskSession(taskId);
  sessionInfo[s] = {...(sessionInfo[s] || {}), kind: KIND_AGENT, last: new Date().toISOString(), stale: false};
  renderSessionSelect();
}

function sessionShown(id, info, cur) {
  if (id === cur) return true;
  if (info.kind === KIND_AGENT) return taskLive(id.slice(TASK_PREFIX.length));
  return view.showStale || !info.stale;
}

function sessionLabel(id, info) {
  if (info.kind !== KIND_AGENT) return id;
  const t = taskInfo[id.slice(TASK_PREFIX.length)];
  return t && t.name ? `${id} · ${t.name}` : id;
}

// закончившийся агент уходит из селектора, если его не выбрали
export function renderSessionSelect() {
  const sel = sessSel();
  const cur = sel.value;
  const groups = Object.fromEntries(SESSION_GROUPS.map(([k, title]) => {
    const g = document.createElement('optgroup'); g.label = title; return [k, g];
  }));
  let hidden = 0;
  const ids = Object.keys(sessionInfo).sort((a, b) =>
    String(sessionInfo[b].last || '').localeCompare(String(sessionInfo[a].last || '')));
  for (const id of ids) {
    const info = sessionInfo[id];
    if (!sessionShown(id, info, cur)) { if (info.kind !== KIND_AGENT) hidden++; continue; }
    const o = new Option(sessionLabel(id, info), id);
    o.title = `${info.count || 0} записей` + (info.last ? `, последняя ${new Date(info.last).toLocaleString('ru-RU')}` : '');
    (groups[info.kind] || groups[KIND_CHECK]).appendChild(o);
  }
  const filled = Object.values(groups).filter(g => g.children.length);
  // тот же состав - список не пересобирается: открытый человеком селектор не закрывается от события
  const sig = JSON.stringify([filled.map(g => [g.label, [...g.children].map(o => [o.value, o.text])]), hidden]);
  if (sig !== view.sig) {
    view.sig = sig;
    const all = new Option('все сессии', '');
    const more = hidden ? [new Option(`устаревшие проверки: ${hidden} - показать`, SHOW_STALE)] : [];
    sel.replaceChildren(all, ...filled, ...more);
    sel.value = cur;
    if (sel.value !== cur) sel.value = '';
  }
  view.shown = sel.value;
  showDeleteButton();
}

// пункт «показать устаревшие» - не сессия: раскрыть их и вернуть прежний выбор
export function revealStale() {
  view.showStale = true;
  sessSel().value = view.shown;
  renderSessionSelect();
}
export const rememberShown = () => { view.shown = sessSel().value; };

export function loadSessions() {
  getJson('/sam/sessions')
    .then(rows => {
      for (const r of rows || []) {
        sessionInfo[r.id] = {kind: r.kind, last: r.last, count: r.count, stale: r.stale, protected: r.protected};
        if (r.default) state.defaultSession = r.id;
      }
      sessSel().title =
        `Чья лента видна и куда уходит реплика; «все сессии» - лента всех, реплика - в ${state.defaultSession}`;
      renderSessionSelect();
      restoreTarget();
    })
    .catch(() => {});
}

// удалить нельзя: «все сессии», живого агента и защищённую (каналы людей)
export function showDeleteButton() {
  const v = sessSel().value, info = sessionInfo[v];
  const live = v.startsWith(TASK_PREFIX) && taskLive(v.slice(TASK_PREFIX.length));
  document.getElementById('sess-del').hidden = !v || live || !!(info && info.protected);
}

export function saveTarget() {
  try { localStorage.setItem(TARGET_KEY, sessSel().value); } catch {}
}

// адресат из прошлого раза - когда пункты селектора уже есть
function restoreTarget() {
  let saved = '';
  try { saved = localStorage.getItem(TARGET_KEY) || ''; } catch {}
  // удалённую (в том числе в другой вкладке) и агента не возвращаем: первая фраза создала бы её заново или оживила
  if (!saved || saved.startsWith(TASK_PREFIX) || !sessionInfo[saved]) return;
  if (sessionInfo[saved].stale) { view.showStale = true; renderSessionSelect(); }
  const sel = sessSel();
  sel.value = saved;
  view.shown = sel.value;
  showTargetHint();
  showDeleteButton();
}

export function pruneTaskOptions() {
  const sel = sessSel();
  for (const o of [...sel.options]) {
    if (o.value.startsWith(TASK_PREFIX) && o.value !== sel.value && !taskLive(o.value.slice(TASK_PREFIX.length))) o.remove();
  }
}

// У поля ввода - куда уйдёт фраза: при «все сессии» - сессия по умолчанию, у закончившегося агента - что он оживёт
export function showTargetHint() {
  const el = document.getElementById('target-hint');
  const v = sessSel().value;
  el.className = 'foot-lbl';
  if (!v) { el.textContent = state.defaultSession ? `реплика → ${state.defaultSession}` : 'адресат ещё не известен'; return; }
  if (sessionInfo[v] && sessionInfo[v].kind === KIND_CHECK) { el.textContent = `реплика → ${v}`; return; }
  if (v.startsWith(TASK_PREFIX) && !taskLive(v.slice(TASK_PREFIX.length))) {
    const key = v.slice(TASK_PREFIX.length), t = taskInfo[key] || tasks[key];
    el.textContent = `фраза оживит агента ${(t && t.name) || key}`;
    el.className = 'foot-lbl t-amber';
    return;
  }
  el.textContent = '';
}
