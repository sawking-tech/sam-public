/* Задачи агентов как данные: запись из события task_created, слияние с /sam/tasks, шаги плана, жива ли задача,
   ушла ли в архив панели. Без DOM. */
import {tasks, taskInfo} from './state.js';
import {taskKey} from './ids.js';

const LIVE_STATUSES = new Set(['pending', 'running', 'waiting']);   // gateway/main.py LIVE_TASK_STATUSES
// Закончившаяся задача старше этого уходит в архив панели: свежие видны сразу, без прокрутки мимо вчерашних
const TASK_ARCHIVE_AFTER_MS = 3 * 3600 * 1000;

export function taskLive(key) {
  const st = (tasks[key] && tasks[key].status) || (taskInfo[key] && taskInfo[key].status);
  return LIVE_STATUSES.has(st);
}

export const taskArchived = (t, now) =>
  !LIVE_STATUSES.has(t.status) && now - (Date.parse(t.created) || 0) > TASK_ARCHIVE_AFTER_MS;

export function addTask(ev) {
  tasks[ev.task_id] = {
    id: ev.task_id, name: ev.name || '', desc: ev.description, device: ev.device, status: 'running',
    created: ev.ts_iso || new Date().toISOString(),
    stages: (ev.stages || []).map((s, i) => ({idx: i, name: s.name, status: 'pending'})),
  };
}

// записи /sam/tasks - правда после F5: имя, описание, статус, итог
export function mergeTaskList(list) {
  for (const t of list || []) {
    taskInfo[t.task_id] = t;
    const old = tasks[t.task_id] || {stages: []};
    tasks[t.task_id] = {...old, id: t.task_id, name: t.name || '', desc: t.description || '', device: t.device || '',
      status: t.status || old.status, result: t.error || t.result || '', created: t.created_at || old.created};
  }
}

// шаг плана сменил статус; все шаги готовы - задача готова, один упал - упала. false - задачи в панели нет
export function updateTaskStage(ev) {
  const t = tasks[ev.task_id]; if (!t) return false;
  const s = t.stages.find(s => s.idx === ev.idx); if (s) s.status = ev.status;
  if (t.stages.every(s => s.status === 'done' || s.status === 'skipped')) t.status = 'done';
  if (t.stages.some(s => s.status === 'failed')) t.status = 'failed';
  return true;
}

// конец задачи из живого события; false - задачи в панели нет
export function setTaskStatus(taskId, status) {
  const t = tasks[taskId];
  if (!t) return false;
  t.status = status;
  return true;
}

// задача по id из кнопки отмены: полный id, короткий или его начало
export const findTask = taskId =>
  Object.values(tasks).find(x => x.id === taskId || x.id === taskKey(taskId) || taskId.startsWith(x.id));

// панель: свежие сверху, архив отдельно
export function taskBoard(now, showArchive) {
  const sorted = Object.values(tasks).sort((a, b) => (Date.parse(b.created) || 0) - (Date.parse(a.created) || 0));
  const archived = sorted.filter(t => taskArchived(t, now));
  return {sorted, archived, shown: showArchive ? sorted : sorted.filter(t => !taskArchived(t, now))};
}
