# Хранилище: MongoDB и Qdrant

MongoDB хранит источник правды, Qdrant — оглавление к фактам памяти. Коллекции лежат в базе `sam`. Документы памяти
и их поиск описаны в [memory-search](../features/memory/memory-search.md), здесь схема коллекций.

Код: `gateway/repos/*` — CRUD с зависимостями, в тестах подменяется mongomock.

## Коллекции

| Коллекция | Содержимое | Срок жизни |
|---|---|---|
| `sessions` | история диалога: реплики, вызовы инструментов, сводки | пока жива сессия; сжатие переносит старое в архив |
| `sessions_archive` | оригиналы сжатых сообщений | поиск `GET /sam/archive/search` |
| `_sessions_meta` | кэш сводной сводки сессии | переживает рестарт |
| `_memories` | факты памяти: текст, summary, вопросы, теги | постоянно; архив — полем `archived` |
| `tasks` | задачи агентов | TTL: done 24 ч, failed 2 ч, не запустившаяся 15 мин |
| `worker_sessions` | история закрытых воркеров для воскрешения | TTL 24 ч без активности |
| `_token_usage` | вызовы LLM: сессия, роль, модель, токены, задержка | TTL `SAM_TOKEN_USAGE_TTL_DAYS`, по умолчанию 30 дней |
| `_soul` | разделы души SAM и версии для отката | постоянно |
| `_dialogue_notes` | заметки основной модели о разговоре | старше `SAM_DIALOGUE_NOTES_MAX_AGE_S` не читаются |
| `_dream` | блокировка ночной консолидации | одна запись |
| `_deferred_tasks` | тяжёлые задачи, ждущие бюджета | до запуска |

## История сессии

```json
{"session_id": "homepc", "role": "user",      "content": "текст",  "timestamp": "..."}
{"session_id": "homepc", "role": "assistant", "content": "...",    "timestamp": "..."}
{"session_id": "homepc", "role": "tool_call", "tool_name": "spawn_agent", "tool_args": {}, "timestamp": "..."}
{"session_id": "homepc", "role": "summary",   "content": "резюме", "time_range": "10:00-10:30"}
```

Вызовы инструментов хранятся нейтрально, ролью `tool_call`, и форматируются под текущую модель при чтении истории
([tool-formats](../architecture/gateway/tool-formats.md)). Воркеры пишут в сессию `task:XXXXXX`, отдельно от
пользовательской.

История переживает перезапуск шлюза. При старте она очищается только с `RESET_HISTORY_ON_START=true`. Память в
`_memories` не чистится никогда.

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

TTL-индекс удаляет записи сам: done через 24 ч, failed и cancelled через 2 ч, не запустившуюся через 15 мин. В
`gateway/tasks.py` есть расширенная схема этапов с `dod`, `dod_check`, `on_fail` и `max_retries`. Сейчас это задел:
задача создаётся без этапов, а `task_created` и `stage_update` воркера (`gateway/worker.py`) показывают plan и
execute как псевдоэтапы.

## Память

Документ `_memories` описывает один факт: `text` (полный текст, источник правды), `summary`, `questions`, `tags`,
`created_at`, `updated_at`, `last_used_at`, `author`, `access_count`, `archived`. Qdrant хранит к каждому факту
несколько векторов: один на summary и по одному на каждый вопрос. Текст в ответе всегда берётся из Mongo, поэтому
Qdrant можно пересоздать без потери данных.

## Проверка

`tests/test_session_repo.py`, `tests/test_archive_repo.py`, `tests/test_token_usage.py`, `tests/test_soul_service.py`,
`tests/test_tasks_tool.py`, `tests/test_worker_session.py`.
