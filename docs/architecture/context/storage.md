# Хранилище: MongoDB и Qdrant

MongoDB - источник правды, Qdrant - оглавление к фактам памяти. Коллекции в базе `sam`. Отдельные документы
памяти и её поиск - `../features/memory/memory-search.md`; здесь схема коллекций.

`gateway/repos/*` - CRUD с зависимостями, в тестах mongomock.

## Коллекции

| Коллекция | Что | Жизнь |
|---|---|---|
| `sessions` | история диалога: реплики, вызовы инструментов, сводки | живёт, пока жива сессия; чистится сжатием в архив |
| `sessions_archive` | оригиналы выжатых сообщений | поиск `GET /sam/archive/search` |
| `_sessions_meta` | кэш сводной сводки сессии | переживает рестарт |
| `_memories` | факты памяти: текст, summary, вопросы, теги | постоянная, архив - полем `archived` |
| `tasks` | задачи агентов | TTL: done 24 ч, failed 2 ч, не запустилась 15 мин |
| `worker_sessions` | история закрытых воркеров для воскрешения | TTL 24 ч без активности |
| `_token_usage` | вызовы LLM: сессия, роль, модель, токены, задержка | TTL `SAM_TOKEN_USAGE_TTL_DAYS` (30) |
| `_soul` | разделы души SAM, версии для отката | постоянная |
| `_dialogue_notes` | заметки основной модели о разговоре | старше `SAM_DIALOGUE_NOTES_MAX_AGE_S` не читаются |
| `_dream` | блокировка ночной консолидации | одна запись |
| `_deferred_tasks` | тяжёлые задачи, ждущие бюджета | до запуска |

## История сессии

```json
{"session_id": "homepc", "role": "user",      "content": "текст", "timestamp": "..."}
{"session_id": "homepc", "role": "assistant", "content": "...",   "timestamp": "..."}
{"session_id": "homepc", "role": "tool_call", "tool_name": "spawn_agent", "tool_args": {}, "timestamp": "..."}
{"session_id": "homepc", "role": "summary",   "content": "резюме...", "time_range": "10:00-10:30"}
```

Вызовы инструментов хранятся нейтрально (`role: tool_call`) и форматируются под текущую модель при чтении истории -
`llm-strategies.md`. Воркеры пишут в сессию `task:XXXXXX`, отдельно от пользовательской.

История переживает перезапуск шлюза; очищается при старте только с `RESET_HISTORY_ON_START=true`. Память
(`_memories`) не чистится никогда.

## Задача

```json
{
  "id": "uuid",
  "description": "открыть весенний плейлист",
  "device": "homepc",
  "status": "pending|running|done|failed|cancelled",
  "stages": [{"name": "plan", "status": "done"}, {"name": "execute", "status": "running"}],
  "current_stage": 1,
  "expires_at": "...",
  "result": "..."
}
```

TTL-индекс удаляет записи сам: done - 24 ч, failed и cancelled - 2 ч, не запустившаяся - 15 мин. В `gateway/tasks.py`
есть расширенная схема этапов с `dod`, `dod_check`, `on_fail`, `max_retries` - задел: задача создаётся без этапов,
а `task_created` и `stage_update` воркера (`gateway/worker.py`) показывают plan и execute как псевдоэтапы.

## Память

Документ `_memories` - один факт: `text` (полный текст, источник правды), `summary`, `questions`, `tags`,
`created_at`, `updated_at`, `last_used_at`, `author`, `access_count`, `archived`. Qdrant хранит к каждому факту
несколько векторов: summary и по вектору на вопрос (HyDE-подход). Текст в ответе всегда берётся из Mongo;
Qdrant можно пересоздать, не потеряв ничего.

## Проверка

`tests/test_session_repo.py`, `tests/test_archive_repo.py`, `tests/test_token_usage.py`, `tests/test_soul_service.py`,
`tests/test_tasks_tool.py`, `tests/test_worker_session.py`.
