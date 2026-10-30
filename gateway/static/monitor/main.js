/* Монитор SAM - корень сборки: связывает модули страницы, вешает обработчики и запускает загрузку.
   Слои: события шлюза (events.js) -> лента (feed.js, sections.js, calls.js, live-rows.js) -> блоки по типу записи
   (blocks/, чистые функции данные -> HTML). Панель: задачи, настройки, душа, фильтр; низ - сессии и строка ввода.

   Модуль подключён в <head> перед shell.js: модуль отложен, как defer, и выполняется раньше каркаса - как прежний
   встроенный скрипт. Поэтому history.state читается до panel.js (он при загрузке переписывает адрес и обнуляет
   state), а лента рисуется по DOMContentLoaded, когда каркас уже перенёс её в рамку (перенос узла обнуляет прокрутку). */
import {handle, setBootView} from './events.js';
import {connect} from './connection.js';
import {logEl, trackBottom, watchBottom} from './log.js';
import {clearLog, toggleAuto} from './feed.js';
import {loadOlder, setEventHandler, OLDER_EDGE_PX} from './older.js';
import {setView, viewTaskSession} from './view.js';
import {loadHealth, refreshCompression, refreshHistoryOff, COMPRESSION_REFRESH_MS, HISTORY_OFF_POLL_MS} from './status.js';
import {renderTasks, loadTasks, cancelTask} from './tasks-panel.js';
import {loadSessions, showTargetHint, sessSel} from './sessions.js';
import {onSessionPicked, deleteSession, confirmDeleteSession} from './session-actions.js';
import {doSend, loadCommands, watchComposer} from './composer.js';
import {onImagePicked, removeImage, watchImageInput} from './attachments.js';
import {panicCancel, cancelTurn} from './actions.js';
import {loadSettings, resetSettings} from './settings.js';
import {loadSoul, pickSoulSection, saveSoulSection, discardSoulEdit} from './soul.js';
import {loadHousehold, addHouseholdMember} from './household.js';
import {toggleDebug, restoreDebug} from './debug-filter.js';
import {savePaint, paintFromCache} from './paint.js';

setBootView((history.state && history.state.monView) || null);
setEventHandler(handle);   // порция старых идёт тем же разбором, что живая лента

// Обработчики из разметки (onclick, onchange, onsubmit) и из HTML блоков (cancelTurn, cancelTask, viewTaskSession,
// _removeImg) зовут функции страницы по имени - модуль отдаёт их окну
Object.assign(window, {
  panicCancel, cancelTurn, cancelTask, viewTaskSession, setView, clearLog, toggleAuto, toggleDebug,
  resetSettings, pickSoulSection, saveSoulSection, discardSoulEdit, addHouseholdMember,
  deleteSession, confirmDeleteSession, onImagePicked, doSend, _removeImg: removeImage,
});

loadHealth();
setInterval(refreshCompression, COMPRESSION_REFRESH_MS);

// длинный текст разворачивается щелчком; выделение текста щелчком не считается
document.addEventListener('click', (e) => {
  const c = e.target.closest('.clip');
  if (!c || String(getSelection())) return;
  c.classList.toggle('open');
  e.stopPropagation();
});
logEl().addEventListener('scroll', (e) => {
  trackBottom(e.target);
  if (e.target.scrollTop < OLDER_EDGE_PX) loadOlder();
});
watchBottom();
addEventListener('popstate', e => setView(e.state && e.state.monView, true));
sessSel().addEventListener('change', onSessionPicked);
watchImageInput();
watchComposer();

refreshHistoryOff();
setInterval(refreshHistoryOff, HISTORY_OFF_POLL_MS);

/* Каркас: статус и кнопки - в полосу, задачи, настройки, душа и фильтр - вкладками панели */
addEventListener('DOMContentLoaded', () => {
  if (!window.SAMShell || !SAMShell.enabled) return;
  SAMShell.setStatus(document.getElementById('mon-status'));
  SAMShell.setTools(...document.getElementById('mon-tools').children);
  const tasksTitle = document.createElement('span');
  tasksTitle.append('Задачи');
  SAMShell.setDefaultPanel('Монитор', [
    {key: 'tasks', title: tasksTitle, node: document.getElementById('tab-tasks')},
    {key: 'settings', title: 'Настройки', node: document.getElementById('tab-settings')},
    {key: 'household', title: 'Дом', node: document.getElementById('tab-household')},
    {key: 'soul', title: 'Душа', node: document.getElementById('tab-soul')},
    {key: 'filter', title: 'Фильтр', node: document.getElementById('tab-filter')},
  ]);
  document.querySelectorAll('.sam-tab[data-tab="settings"]').forEach(b => b.addEventListener('click', loadSettings));
  document.querySelectorAll('.sam-tab[data-tab="soul"]').forEach(b => b.addEventListener('click', loadSoul));
  document.querySelectorAll('.sam-tab[data-tab="household"]').forEach(b => b.addEventListener('click', loadHousehold));
  loadSettings();
});

addEventListener('pagehide', savePaint);
restoreDebug();

renderTasks();
loadTasks();
showTargetHint();
loadSessions();
loadCommands();
// кадр из кеша и снимок рисуются, когда каркас уже поставил ленту на место
addEventListener('DOMContentLoaded', () => { paintFromCache(); connect(handle); });
