/* Имена сессий агентов: сессия агента - task:<первые 8 знаков id задачи> (gateway/worker.py). */

export const TASK_PREFIX = 'task:';
export const TASK_KEY_LEN = 8;

export const isTaskSession = s => (s || '').startsWith(TASK_PREFIX);
// короткий id задачи - ключ секции агента в ленте и записи в /sam/tasks
export const taskKey = taskId => taskId.slice(0, TASK_KEY_LEN);
export const taskSession = taskId => TASK_PREFIX + taskKey(taskId);
// ключ задачи из имени её сессии
export const sessionTaskKey = sess => sess.slice(TASK_PREFIX.length, TASK_PREFIX.length + TASK_KEY_LEN);

// Сессия события без поля session - сессия по умолчанию шлюза
export const DEFAULT_SESSION_ID = 'default';
// Временные строки ленты: поток печати ответа и «думает»; в кеш кадра не идут и снимаются при сбросе ленты
export const STREAM_ROW_PREFIX = 'stream-';
export const BUSY_ROW_PREFIX = 'busy-row-';
export const isTransientRowId = id => id.startsWith(STREAM_ROW_PREFIX) || id.startsWith(BUSY_ROW_PREFIX);
