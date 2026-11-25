# REST API шлюза

Все ручки на одном порту, `:8000`. Ключ и что закрыто без него - `access.md`; ключ привилегий - там же.

Полный список ручек - `gateway/main.py`. Ниже главные; остальные ручки сессии (`/sam/session/{id}/...`: отмена хода
и очереди, правка последних реплик) и радара (`/sam/presence/...`) - в `gateway/main.py` и `radar/presence_api.py`.

| Метод | Путь | Что |
|---|---|---|
| POST | `/v1/chat/completions` | главный вход, OpenAI-совместимый |
| POST | `/sam/event` | событие от n8n или Home Assistant |
| POST | `/sam/client/register`, `/heartbeat`, `/offline` | устройство пришло, живо, ушло |
| GET | `/sam/catalog`, `/sam/catalog/devices/online` | каталог людей, устройств и питомцев; кто онлайн |
| GET | `/sam/sessions` | сессии с видом (`person`, `agent`, `check`), свежие первыми |
| DELETE | `/sam/sessions/{id}` | удалить сессию со всеми следами; голос, эта машина, работающий агент и идущий ход - отказ |
| GET | `/sam/tasks` | последние задачи агентов любого статуса, не больше `SAM_MONITOR_TASKS_LIMIT` (30) |
| POST | `/sam/tasks/{id}/cancel` | отмена задачи; принимает 8-символьный префикс id |
| GET | `/sam/monitor/history` | порция истории монитора раньше `before` и `before_id` |
| POST | `/sam/sessions/{id}/compress` | явное сжатие истории |
| POST | `/sam/inject` | сообщение в сессию; `task:<id>` - работающему агенту в ход, закрытому - воскрешение с историей |
| POST | `/sam/agents` | агент от основной модели вне шлюза; ключ привилегий |
| POST | `/sam/agents/{id}/grant` | выдать агенту Cortex инструмент по его запросу; ключ привилегий |
| POST | `/sam/agents/{id}/inbox`, `/ask`, `/status` | канал агента кода к создателю |
| POST | `/sam/main-model`, `/sam/llm/switch` | смена основной модели и адаптера LLM; ключ привилегий |
| POST | `/sam/session/{id}/reply`, `/note`, `/command` | ответ и заметка основной модели вне шлюза, команда монитора |
| POST | `/sam/push/ack` | подтверждение пунктов push моделью вне шлюза |
| POST | `/sam/stand/watch`, `/sam/stand/restart` | отчёт надзора и перезапуск компонента стенда |
| GET | `/sam/commands`, `/sam/config`, `/sam/memory/search` | команды для `/` монитора, настройки, поиск по памяти |
| GET | `/sam/metrics/tokens/sessions/{id}` | токены сессии по ролям main / worker / guard / summarize |
| GET | `/health` | состояние: основная модель, компоненты, сжатие, workflow |
| GET | `/monitor` | монитор |
| * | `/apps/{slug}/...` | страница живого приложения с CSP sandbox; по id - 308 на slug |
| WS | `/ws/monitor` | поток событий монитора |
| WS | `/voice/turn` | голосовой ход (`../features/fast-voice.md`, ADR-010) |

## Агент от основной модели вне шлюза

Ведущий создаёт агента SAM тем же путём, что `spawn_agent`: задача в MongoDB, событие `task_created`, воркер в фоне.

```
POST /sam/agents {"description": "...", "level": "smart|medium|weak", "tools": ["search_web", ...],
                  "device": "", "name": "", "framework": "sam|cortex", "peers": [], "depth": 0, "steps": 30}
-> {"status": "ok", "task_id": "abcd1234", "session": "task:abcd1234", "level": ..., "tools": [...],
    "device": ..., "model": ...}
```

- `level` - метка сложности, по умолчанию `medium`; модель - источник роутера из
  `SAM_AGENT_LLM_SMART|MEDIUM|WEAK`, пусто или недоступен - выбор роутера.
- `tools` - права: подмножество инструментов воркера, без поля - все. `ask_creator`, `report_status`,
  `estimate_steps` и `request_steps` есть всегда. Права и метка хранятся в задаче и действуют после воскрешения.
- `framework` - воркер шлюза или сессия Cortex (по умолчанию `SAM_AGENT_FRAMEWORK`, иначе `sam`). Агенту Cortex
  `tools` обязательны, `peers` - кому он может писать, `depth` - поколений детей, до `SAM_AGENT_MAX_DEPTH`.
- `steps` - прикидка шагов (`../features/guard.md`, шаги агентов).
- Ручка требует ключ привилегий `X-SAM-Privilege`.
- Отказ - код HTTP и одна строка в `detail`: 400 запрос, 429 все `SAM_MAX_WORKERS` заняты, 503 нет MongoDB,
  устройства или модели.
- Вопрос агента (`ask_creator`) идёт во входящие основной модели (`lead-in.jsonl`): тикер приносит его тиком `chat`
  с `source: agent_question`, `session: task:<id>`. Ответ - `POST /sam/inject` в эту сессию. Итог агента приходит
  туда же (`source: agent_done`) с вопросами, которые он задавал.
- События агента в мониторе относятся к сессии `SAM_AGENT_PARENT_SESSION` (по умолчанию `voice`).
- Ядро - `gateway/tools/agents.py` (`create_agent_impl`, `answer_agent_impl`, `ask_creator_impl`);
  эндпоинты - обёртки, зависимости собирает `gateway/agent.py`.

## Проверка

`tests/test_access.py`, `tests/test_agents_api.py`, `tests/test_health.py`, `tests/test_main_boundaries.py`.
