/* Состояние страницы монитора - одно место для данных, которые читают несколько модулей. Здесь только данные:
   что с ними делать, решают модули ленты, задач и сессий. */

export const state = {
  autoScroll: true,     // флажок «держать ленту внизу»
  atBottom: true,       // человек у низа ленты (прокрутка не ушла вверх)
  evN: 0,               // событий в ленте - счётчик в полосе
  filterSess: null,     // видна лента одной сессии или агента, null - всех
  // Снимок и история: при открытии и после F5 снимок шлюза - вся правда о ленте и заменяет её разом (и кадр из кеша);
  // после обрыва связи живая лента остаётся (в ней итоги вызовов и шаги агентов, которых нет в истории), снимок
  // дописывает только то, что новее последнего показанного; старые - порциями при прокрутке вверх
  logTarget: null,      // фрагмент, куда идут строки снимка или порции старых; null - прямо в ленту
  oldestIso: null, oldestId: '',   // самая ранняя запись общей ленты: с неё просят порцию старых
  olderLoading: false,  // порция старых запрошена
  olderBusy: false,     // порция старых разбирается
  olderDone: false,     // общая лента дошла до начала истории
  feedGen: 0,           // номер снимка: порция старых, запрошенная до него, к новой ленте не пристраивается
  // Лента одной сессии или агента берёт свои старые записи своим курсором (шлюз: session=); начинает с самой ранней
  // записи общей ленты - всё новее уже в ней. Её строки помечены data-view-only и уходят, когда лента сменилась
  viewCursor: null,     // {before, before_id, done}
  viewLoad: null,       // идёт разбор порции ленты этой сессии
  paintedFromCache: false,   // лента нарисована кадром из кеша до снимка (paint.js)
  // куда уходит реплика, пока видна лента всех сессий: устройство этой машины (/sam/sessions, default); до ответа
  // шлюза адресата нет и реплика не уходит - иначе она легла бы в чужую сессию
  defaultSession: '',
};

export const busySessions = new Set();   // сессии, где идёт ход: пока есть, под полосой бежит скан-луч
export const tasks = {};          // задачи панели: id -> {id, name, desc, device, status, result, created, stages}
export const taskInfo = {};       // короткий id задачи -> запись из /sam/tasks: имя, описание, статус, итог (правда после F5)
export const sessionInfo = {};    // id сессии -> {kind, last, count, stale, protected}
export const taskSections = {};   // короткий id задачи -> секция агента в ленте (sections.js)
export const compressionRows = {};   // сессия -> {row, calls}: строка идущего сжатия
// вызовы инструментов (calls.js)
export const callRows = {};       // session|call_id -> {row}: пометка guard находит строку своего вызова
export const guardMarks = {};     // session|call_id -> пометка guard, пришедшая раньше вызова (вызовы из текста)
export const pendingCalls = {};   // call_id -> {group, dotEl}: вызов ждёт итог
export const callByName = {};     // имя инструмента -> [call_id, ...]: итог без id находит вызов по имени

export function clearAll(...maps) {
  for (const m of maps) for (const k of Object.keys(m)) delete m[k];
}
