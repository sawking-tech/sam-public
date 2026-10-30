/* Флажок «отладка» во вкладке «Фильтр»: запросы к модели, рассуждения, пустые и фоновые вызовы; выбор переживает F5. */

const DEBUG_KEY = 'sam.monitor.debug';

export function toggleDebug(on) {
  document.body.classList.toggle('show-debug', on);
  try { localStorage.setItem(DEBUG_KEY, on ? '1' : ''); } catch {}
}

// включённая в прошлый раз отладка - флажком и классом страницы, когда разметка готова
export function restoreDebug() {
  try { if (localStorage.getItem(DEBUG_KEY)) document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('debug-btn').checked = true; toggleDebug(true); }); } catch {}
}
