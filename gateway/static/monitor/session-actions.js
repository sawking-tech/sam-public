/* Действия с сессией: выбор в селекторе и удаление. Удаление - только после ввода её id; шлюз отказывает каналам
   людей и идущей работе одной строкой. */
import {state, sessionInfo, taskSections} from './state.js';
import {sessSel, SHOW_STALE, TARGET_KEY, revealStale, rememberShown, saveTarget, renderSessionSelect,
  showTargetHint, showDeleteButton} from './sessions.js';
import {setView} from './view.js';
import {pruneDetachedCalls, forgetGuardMarks} from './calls.js';
import {TASK_PREFIX} from './ids.js';

// Селектор: чья лента видна и куда уходит реплика; «все сессии» - лента всех, реплика - в сессию по умолчанию.
// «Глаз» задачи меняет только ленту: адресат - всегда то, что выбрал человек
export function onSessionPicked() {
  const sel = sessSel();
  if (sel.value === SHOW_STALE) { revealStale(); return; }
  rememberShown();
  saveTarget();
  setView(sel.value || null);
  showTargetHint();
  showDeleteButton();
}

// сессию задачи шлюз удаляет без ключа, любую другую - с ключом команд (gateway/main.py delete_session)
const needsDeleteKey = id => !id.toLowerCase().startsWith(TASK_PREFIX);
let deletingSession = '';

export function deleteSession() {
  const id = sessSel().value;
  if (!id) return;
  deletingSession = id;
  document.getElementById('sess-del-title').textContent =
    `Удалить сессию «${id}» навсегда - историю, архив сжатого и заметки о разговоре?`;
  document.getElementById('sess-del-id').value = '';
  document.getElementById('sess-del-key').value = '';
  document.getElementById('sess-del-key-row').hidden = !needsDeleteKey(id);
  document.getElementById('sess-del-msg').hidden = true;
  document.getElementById('sess-del-dlg').showModal();
  document.getElementById('sess-del-id').focus();
}

function deleteSessionMessage(text) {
  const msg = document.getElementById('sess-del-msg');
  msg.textContent = text; msg.hidden = false;
}

export function confirmDeleteSession(event) {
  event.preventDefault();
  const id = deletingSession, dlg = document.getElementById('sess-del-dlg');
  if (document.getElementById('sess-del-id').value.trim() !== id) { deleteSessionMessage('id не совпадает'); return false; }
  const secret = document.getElementById('sess-del-key').value.trim();
  if (needsDeleteKey(id) && !secret) { deleteSessionMessage('Нужен ключ команд'); return false; }
  const headers = needsDeleteKey(id) ? {'X-SAM-Command-Secret': secret} : {};
  fetch(`/sam/sessions/${encodeURIComponent(id)}`, {method: 'DELETE', headers})
    .then(r => r.json().then(d => {
      if (r.ok) { dlg.close(); forgetSession(id); return; }
      deleteSessionMessage(`Сессия не удалена: ${d.detail || r.status}`);
    }))
    .catch(() => deleteSessionMessage('Шлюз не ответил'));
  return false;
}

// сессии больше нет (удалена здесь или в другой вкладке): её строки, пункт селектора, лента и сохранённый адресат
// уходят
export function forgetSession(id) {
  delete sessionInfo[id];
  document.querySelectorAll(`#log [data-sess="${CSS.escape(id)}"]`).forEach(el => el.remove());
  pruneDetachedCalls();
  forgetGuardMarks(id);
  try { if (localStorage.getItem(TARGET_KEY) === id) localStorage.removeItem(TARGET_KEY); } catch {}
  const key = id.startsWith(TASK_PREFIX) ? id.slice(TASK_PREFIX.length) : null;
  if (key) delete taskSections[key];
  const sel = sessSel();
  if (sel.value === id) { sel.value = ''; saveTarget(); }
  if (state.filterSess === id) setView(null);
  renderSessionSelect();
  showTargetHint();
}
