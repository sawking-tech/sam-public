/* Вкладка «Задачи» панели и счётчик задач в полосе: карточки свежих задач, архив под кнопкой, отмена. */
import {taskSections} from './state.js';
import {getJson, JSON_POST} from './api.js';
import {mergeTaskList, taskArchived, taskBoard, findTask} from './tasks-model.js';
import {taskCardHtml} from './blocks/task-card.js';
import {applyTaskInfo} from './sections.js';
import {showViewBar} from './view.js';
import {showTargetHint, renderSessionSelect} from './sessions.js';

const taskCards = {};   // id задачи -> её карточка в панели
let showTaskArchive = false;

function toggleTaskArchive() { showTaskArchive = !showTaskArchive; renderTasks(); }

function archiveButton() {
  let toggle = document.getElementById('tasks-archive');
  if (!toggle) {
    toggle = document.createElement('button');
    toggle.id = 'tasks-archive'; toggle.type = 'button'; toggle.className = 'sam-btn'; toggle.onclick = toggleTaskArchive;
  }
  return toggle;
}

export function renderTasks() {
  const list = document.getElementById('tasks-list');
  const now = Date.now();
  const {sorted, archived, shown} = taskBoard(now, showTaskArchive);
  document.getElementById('task-count').textContent = sorted.length - archived.length;
  const toggle = archiveButton();
  toggle.hidden = !archived.length;
  toggle.textContent = showTaskArchive ? `скрыть архив (${archived.length})` : `архив: ${archived.length}`;
  for (const id of Object.keys(taskCards)) {
    if (!shown.some(t => t.id === id)) taskCards[id].remove();   // ушедшая в архив карточка снимается со страницы
  }
  if (!shown.length) {
    list.replaceChildren(Object.assign(document.createElement('div'), {className: 'tasks-empty', textContent: 'новых задач нет'}), toggle);
    if (!sorted.length) list.firstChild.textContent = 'задач пока нет';
    return;
  }
  list.querySelector('.tasks-empty')?.remove();
  let toggled = false;
  for (const t of shown) {
    if (!toggled && taskArchived(t, now)) { list.appendChild(toggle); toggled = true; }   // архив - под кнопкой
    const html = taskCardHtml(t);
    // карточка меняется, только если изменилась сама задача: раскрытый итог соседних не сворачивается
    let card = taskCards[t.id];
    if (!card) { card = taskCards[t.id] = document.createElement('div'); card.className = 'tcard'; }
    if (card.dataset.sig !== html) { card.innerHTML = html; card.dataset.sig = html; }
    list.appendChild(card);   // уже стоящая карточка переезжает на своё место, не пересоздаётся
  }
  if (!toggled) list.appendChild(toggle);   // архив скрыт: кнопка под свежими
}

// Задачи агентов из sam.tasks: панель после F5 и шапки секций агентов (имя, статус, итог)
export function loadTasks() {
  getJson('/sam/tasks').then(list => {
    mergeTaskList(list);
    for (const t of list || []) if (taskSections[t.task_id]) applyTaskInfo(t.task_id);
    renderTasks();
    showViewBar();
    showTargetHint();   // закончился ли агент-адресат - по записи задачи
    renderSessionSelect();   // живые агенты в селекторе, закончившиеся уходят
  }).catch(() => {});
}

export function cancelTask(taskId) {
  fetch(`/sam/tasks/${taskId}/cancel`, JSON_POST)
    .then(r => {
      if (r.ok) {
        const t = findTask(taskId);
        if (t) { t.status = 'cancelled'; renderTasks(); }
      } else {
        console.warn('cancel not_found:', taskId);
      }
    })
    .catch(() => {});
}
