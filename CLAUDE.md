# SAM - инструкции для Claude Code

## Язык
- Общение: **русский**
- Код: **английский**
- **Экономь токены:** короткие ответы, без лишних объяснений, максимум параллельных tool calls. Не читать файлы повторно - архитектура актуальна в README.md, читать его один раз в начале задачи.

## Сессионный контекст

Состояние сессии (где остановились, что делаем дальше) - `.claude/session-context/current.md`, файл вне git. Хук
`.claude/hooks/load-context.sh` показывает его при старте. Внешняя память (MCP Memory и подобное) - по желанию, в репозиторий не входит.

## Документация
- **README.md** - единственный актуальный документ по архитектуре
- При любых изменениях архитектуры - обновлять README.md

## Код

Правила для каждой строки в репозитории, на любом языке, и для служебного скрипта тоже.


- Главное из SOLID - единственная ответственность: у модуля, класса и функции одна причина меняться.
- Чистая архитектура и модульность там, где они к месту: зависимости смотрят внутрь, модуль самостоятелен и зависит только от нужного, слой, который решает, не знает, как хранят и как показывают.
- Композиция вместо наследования: поведение передаётся через конструктор или параметр и собирается в одном месте (корень композиции). Подклассы ради подмены поведения не заводятся.
- Функция делает одно. Длинная работа - оркестратор, который называет шаги и вызывает маленькие функции или классы.
- Никаких магических значений в строке кода: число, путь, шаблон или имя со смыслом - именованная константа там, где ей место, с причиной или замером в комментарии.
- Настройка без правки кода: то, что зависит от машины, окружения или вкуса (порог, адрес, путь, модель), читается из конфига или переменной окружения, у константы остаётся значение по умолчанию.
- Логика, которая решает, отделена от того, что трогает внешний мир (файлы, сеть, процессы, устройства, консоль), чтобы её можно было проверить без них.
- Изменение поведения в том же коммите получает проверку: тест, а где логику не отделить - описанную проверку в документации проекта. Любой коммит собирается и запускается.
- Комментарий и docstring описывают, что делает код и зачем.
- Сбой говорит одной строкой, что не так и что делать: исключение, строка лога или код выхода с сообщением, без стека для ожидаемого случая.
- Перед новым файлом того вида, который в репозитории уже есть (модуль, раздел документации, ADR), открыть двух-трёх соседей этого вида и писать так же.
- Язык комментариев и логов один на проект, как у соседей. Сообщения коммитов - по skill git-style.
- Язык скрипта по его работе: shell для скрипта, который в основном запускает команды; язык со стандартной библиотекой для разбора текста - для скрипта, который читает текст; язык продукта - для продукта.

## Версии

Версия и заметки к выпуску - `version.json`. `dev` - рабочая ветка, `main` - последний выпуск.

## Проект
SAM (Smart Ambient Mate) - система мониторинга, реагирования и ассистирования для большой и малой инфраструктуры, хранитель атмосферы своего места: хранит и улучшает
атмосферу, выявляет, систематизирует и отлаживает процессы, прогнозирует потребности, опора в кризис, реагирует быстро и проактивно; сделана из модульной
платформы ИИ-агента,
которая чинит и дописывает себя. Первая цель - домашний ассистент с целями и приложениями своего дома (голос,
память о людях, радар). Позиционирование - README.md, «Что такое SAM».
Люди дома - в анкете монитора и личном слое `data/SAM.md` (вне git).

## Ключевые файлы

### Gateway (agent runtime)
Рефакторинг SAM-40 в работе: из agent.py вынесены runner, agent_loop, worker,
tools и repos с DI (коммиты `SAM-40` в git log); сам agent.py - 2586 строк
(регистрация tools, их обёртки, сборка deps), цель задачи - меньше 500:

| Файл | Назначение |
|------|------------|
| `gateway/agent.py` | Регистрация tools (BaseTool классы) + lazy deps + тонкие враппера |
| `gateway/main.py` | FastAPI роутер - только маршрутизация |
| `gateway/runner.py` | `SAMAgentRunner.run` → `run_turn(deps)`. Один user turn: история, RAG, guard, agent_loop, voice gate, action-verb retry |
| `gateway/agent_loop.py` | Универсальный async loop: OpenAI tools + Harmony + custom text-parse, loop guard, dedup, parallel tool exec |
| `gateway/worker.py` | `WorkerDeps` + `run_worker / run_worker_task / resume_worker_task` (SAM-38). ContextVars `worker_session_var`, `worker_last_answer_var` |
| `gateway/agent_budget.py` | Шаги агента - пул на дерево (ADR-022, «Бюджет дерева»): `SAM_AGENT_STEPS` (30) на задачу, выдача основной модели не больше `SAM_AGENT_STEPS_CAP` (100); ход только из каналов шаг не тратит, бесплатных вызовов на задачу - `SAM_AGENT_CHANNEL_CALLS` (10) |
| `gateway/privilege_key.py` | Ключ привилегий (заголовок `X-SAM-Privilege`) для `POST /sam/agents`, `/sam/agents/{id}/grant`, `/sam/main-model`, `/sam/llm/switch`, даже с этой машины; лежит в `data/secrets`, его держит MCP sam |
| `gateway/push_queue.py`, `gateway/push_dispatch.py` | Очередь push в основную модель (ADR-020): alone / batch / merge, ровные пачки параллельно по местам модели, подтверждение или повтор каждого пункта |
| `gateway/speech_meta.py` | Что не звучит (ADR-003): любой парный блок `<имя>...</имя>` - мета для монитора и workflow, рассуждение, молчание |
| `gateway/llm_client.py` | LLM adapter layer (cooldown, 429 backoff, 403 quota, 500/503 skip) |
| `gateway/token_usage.py` | SAM-48: usage каждого вызова LLM с сессией и ролью (`usage_scope`), приёмник - `TokenUsageRepo` |
| `gateway/llm_router.py` | Выбор LLM по задаче + prompt_format детект |
| `gateway/provider_transform.py` | sanitize_tools_for_provider + normalize_messages_for_provider (Claude/Mistral/Gemini quirks) |
| `gateway/guard.py` | Safety guard: паттерны + LLM sub-agent для high-risk tools |
| `gateway/compression.py` | SAM-36: плавающее окно памяти + summary chain + архив |
| `gateway/text_utils.py` | strip_thinking, voice extract/clean, channel parse |
| `SAM.md` | Инструкции SAM разделами `## ИМЯ`, как CLAUDE.md: дела, слух, речь, инструменты; шапка - подстановки имён по умолчанию; читается при старте шлюза (`gateway/prompts/instructions.py`) |
| `gateway/prompts/soul_seed.md` | Заготовка души SAM - только характер (`IDENTITY`, `CHARACTER`, `TONE`): засевает пустую коллекцию `_soul`, сама душа живёт в хранилище |
| `data/SAM.md` | Личный слой (вне git): имена в шапке и личные инструкции, читается при каждой сборке промпта (`gateway/prompts/personal.py`) |
| `gateway/soul_service.py`, `gateway/repos/soul_repo.py` | Душа SAM в Mongo `_soul`: засев из заготовки, уборка записей вне неё, правка с проверкой структуры, история и откат |
| `gateway/prompts/` | Пакет: сборка промптов из души и инструкций (`soul.py` - снимок и кэш души, `instructions.py` - SAM.md, `sections.py` - раздел по имени, `prompt_md.py` - формат, `personal.py`, `system.py`, `voice.py`, `lead.py`), WORKER_SYSTEM, GUARD_MSG, etc |
| `gateway/event_bus.py` | EventBus: WS broadcast + MongoDB persist + inject queue |
| `gateway/session_lock.py` | SAM-01: per-session asyncio.Lock + queue accumulation |
| `gateway/stt_dedup.py` | SAM-32: TF-IDF cosine дедуп STT |
| `gateway/catalog.py` | MongoDB CRUD: устройства / люди / питомцы |
| `gateway/household.py`, `gateway/household_api.py` | Состав дома: блок промпта, анкета монитора (`docs/features/household.md`) |
| `gateway/tasks.py` | Task schema, CRUD, TTL индекс |
| `gateway/worker_session.py` | SAM-38: persistent worker sessions для resume |
| `gateway/rag.py` | Industrial RAG: multi-facet TOC + HyDE-style + dedup + MMR |
| `gateway/mcp_client.py` | HTTP MCP client (rpc, tools/list, tools/call) |

### Tools (gateway/tools/)
Тонкие функции `*_impl(params, deps...)` - все с DI, легко тестируются:

| Файл | Tools |
|------|-------|
| `gateway/tools/memory.py` | `remember`, `update_memory`, `list_memories`, `forget` |
| `gateway/tools/catalog.py` | `get_devices`, `get_catalog`, `set_household_member`, `wake_device` |
| `gateway/tools/tasks.py` | `get_tasks`, `cancel_task` |
| `gateway/tools/web.py` | `search_web` (invidious/google/ddg) |
| `gateway/tools/mcp.py` | `mcp_call` (tools/list, tools/call с error path) |
| `gateway/tools/coding_agents.py` | `claude_delegate` (backend-agnostic: Claude Code / OpenCode) |
| `gateway/tools/forget_topic.py` | `forget_topic` |
| `gateway/tools/capabilities.py` | `list_capabilities` |
| `gateway/tools/self_tune.py` | `tune_self`, `get_self_config` |
| `gateway/tools/soul.py` | `soul_show`, `soul_edit`, `soul_rollback` (характер SAM в хранилище) |
| `gateway/tools/set_env.py` | `set_env`, `get_env`, `list_env` |
| `gateway/tools/agents.py` | создать агента и ответить ему (REST `/sam/agents`, MCP sam), `ask_creator`, письмо собеседнику |
| `gateway/tools/agent_channel.py` | каналы агента разработки из `claude_delegate` к создателю (SAM-164) |
| `gateway/tools/main_model.py` | `switch_main_model` |
| `gateway/tools/quiet.py` | `quiet` |
| `gateway/tools/dialogue_notes.py` | `dialogue_context`, `update_dialogue_notes` (шаги workflow, SAM-131) |
| `gateway/tools/amend_reply.py` | `amend_reply` (шаг workflow, SAM-166) |
| `gateway/tools/apps.py` | `app_make`, `app_update`, `app_list`, `app_stop` (живые приложения, SAM-183) |

### Repos (gateway/repos/)
Mongo CRUD - DI для тестов с mongomock:

| Файл | Что |
|------|-----|
| `gateway/repos/memory_repo.py` | RAG-facts: `_to_object_id`, archive/unarchive, related links |
| `gateway/repos/session_repo.py` | session history CRUD |
| `gateway/repos/meta_repo.py` | `_sessions_meta`: кэш сводного summary для компрессии |
| `gateway/repos/token_usage_repo.py` | `_token_usage`: запись вызовов и `totals_by_role` по сессии |
| `gateway/repos/archive_repo.py` | архив сжатых сообщений (ADR-008) |
| `gateway/repos/dialogue_notes_repo.py` | заметки основной модели о разговоре (SAM-131) |
| `gateway/repos/soul_repo.py` | `_soul`: разделы души, версии, история для отката |

### Cortex (cortex/, модуль по docs/brain-map.md)

| Файл | Назначение |
|------|------------|
| `cortex/acp/agent.py` | Свой агент за ACP (ADR-022): сессия с MCP, ход через `gateway/agent_loop.py`, отмена, разрешения, инъекция `_sam/inject` |
| `cortex/acp/mcp_tools.py` | Инструменты MCP сессии через SDK `mcp`: схемы для модели, реестр для разбора текста, вызов |
| `cortex/acp/updates.py` | События цикла → обновления сессии ACP (текст потоком, вызов и итог инструмента) |
| `cortex/acp/stdio.py` | Потоки протокола на исходных stdin/stdout, `print` - в stderr |
| `cortex/acp/tool_search.py` | Поиск инструментов: мост `tool_search`, BM25 по основам слов, найденное и названное скиллом - со следующего шага |
| `cortex/acp/rights.py` | Права сессии от создателя (SAM-133): `_meta` новой сессии - создатель, выдача инструментов, метка сложности, собеседники, глубина детей; инструменты урезаются до выдачи, модель - по метке |
| `cortex/acp/channels.py` | Каналы агента (SAM-133): `ask_creator`, `report_status`, `request_permission`, `send_to_agent` у сессии с создателем, `spawn_agent` и `grant_child` при праве на детей; проверка по правам, вызов клиенту `_sam/agent_event` |
| `gateway/cortex_workers.py` | Агенты SAM в сессиях Cortex (`spawn_agent framework=cortex`, `SAM_AGENT_FRAMEWORK`): права в `_meta`, каналы и итог - функциями воркеров, ответ создателя - `_sam/inject`; агент живёт задачу: пока идут дети или ждёт ответ, следующий ход по сообщению (`SAM_CORTEX_WAIT_S`) |
| `gateway/tool_text_parse.py` | Разбор вызовов из текста (мета-формат) по переданному реестру - общий для шлюза и Cortex |

### MCP / Static / Infra

| Файл | Назначение |
|------|------------|
| `gateway/static/monitor.html` | WebSocket монитор (300 events replay), вкладка «Душа» - правка характера SAM |
| `radar/` | Радар присутствия отдельным приложением (SAM-198): `python -m radar.presence_service` (:8010, `start_radar.ps1`), функции для SAM - `radar/presence_sam_api.py` (`/sam/tools/*`), шлюз зовёт их через `gateway/radar_client.py` и модулей радара не импортирует, страница - `radar/static/` |
| `gateway/soul_api.py` | REST души: `/sam/soul`, запись и откат с ключом привилегий или ключом команд |
| `gateway/apps_api.py` | Страница «Приложения» `/apps` (встроенное приложение шлюза, SAM-204) и API стенда `/sam/apps`: список, настройки, старт, остановка, закрепление, удаление; docker - через `gateway/apps.py` |
| `chat.py` | терминальный чат с gateway (`python chat.py --device workpc`), основа TUI-пульта SAM-24 |
| `mcp/mcp_aggregator.py` | Агрегатор MCP серверов → один порт |
| `mcp/windows_control_server.py` | Windows: run_command, powershell, screenshot, get_windows |
| `mcp/client_agent.py` | Регистрация + heartbeat клиентского ПК |
| `mcp/configs/local.json` | Конфиг MCP серверов для текущего ПК |
| `docker/docker-compose.dev.yml` | MongoDB + Qdrant + n8n для dev |
| `.env` | API ключи и переменные (не в git) |

### Тесты (tests/)
Pytest + pytest-asyncio, mongomock для Mongo, FakeQdrant + FakeEmbedder
для RAG. 1987 тестов, по умолчанию собирается 1980: маркеры `integration` и `quality` исключаются по
умолчанию.

Покрыты unit-тестами: `agent_loop`, `worker`, `runner`, `rag`, `guard`,
`compression`, `provider_transform`, `llm_client`, `session_lock`,
`stt_dedup`, `mcp_client`, `tools/` (без своих тестов memory, catalog, forget_topic, self_tune, set_env), все 6 `repos/*`, `event_bus`.

## Автономный режим разработки

**Claude запускает сервисы сам** - не ждать команды от пользователя.

### Запуск / рестарт всего
```bash
powershell -File start_sam.ps1
```
Запускает: Docker (MongoDB+Qdrant+n8n) → синтез Silero :10202 и эмбеддер :10204 (SAM-172) → MCP :3000 → client_agent, голос (`voice.layer.voice`:
слух, озвучка, узел звука :10210) → Gateway :8000 (страница приложений `/apps` - его же) → мост `voice.layer.bridge` (скрытый процесс, лог
`logs/bridge.log`) → вкладка `wt` с `claude` в папке SAM (SAM-91, `scripts/sam_lead.ps1`). Без ключей, если уже
запущено, - рестарт (порты 8000/3000, мост). Мост второй раз не запускается: скрипт ищет его процесс по командной
строке. Вкладка открывается, только если `data/voice/bus/lead.alive` старше 5 с (тот же порог, что у шлюза);
`-NoLead` её не открывает. `-Stop` и рестарт гасят вкладки-надзиратели этого репозитория и процессы компонентов по
командной строке, мост тоже; сессию Claude Code, тикер, надзор и живость не трогают. Чужой процесс на 8000 или 3000
не гасится: скрипт называет его, а шлюз или агрегатор не поднимает. Терминал, команда, аргументы
и первое сообщение сессии - `SAM_LEAD_TERMINAL`, `SAM_LEAD_COMMAND`, `SAM_LEAD_ARGS`, `SAM_LEAD_PROMPT` (окружение
или `.env`).
Компоненты работают скрытыми процессами, вывод каждого - в `logs/<компонент>.log` (`SAM_COMPONENT_TABS=1` - вкладками wt,
как раньше; 15 окт вкладки с непрерывным выводом вешали терминал). Одиночный ключ (`-Gateway`, `-Mcp`, `-Voice`,
`-Client`, `-Cosy`) сначала гасит старый процесс своего компонента. Через 5 с после старта скрипт пишет красную строку, если компонент запущен дважды (10 окт).

Сессия из вкладки получает `SAM_LEAD_SESSION=1` и первым сообщением просьбу выполнить шаги старта. SessionStart-хук
`.claude/hooks/lead_brief.py` печатает их в разделе «Старт»:
- живость сессии: `python -u scripts/lead_heartbeat.py` (Bash run_in_background, без лимита Monitor), если
  `lead.alive` не обновляется;
- тикер: Monitor `python -u -m voice.layer.ticker`; строки надзора: Monitor `tail -n 0 -F logs/stand_watch.log` (надзор
  поднимают модули стенда, SAM-195).
Сессия, открытая руками, получает тот же бриф, пока тикер писал в шину последние 10 минут.
Проверка: `/health` - `main_model` и `components`; MCP - `tools/list` не пуст; один процесс на компонент.

### Остановка
```bash
powershell -File start_sam.ps1 -Stop
```

### Проверка
```bash
curl http://localhost:8000/health   # {"status":"ok","main_model":"...","llm":"...","model":"...","components":{...}}
curl http://localhost:3000/health   # {"status":"ok","tools":N,"servers":[...]}
```

### Выбор LLM провайдера
Две разные переменные: `LLM_SOURCE` выбирает поставщика (`gateway/llm_router.py:141`),
`LLM_ADAPTER` - чем к нему ходить (`gateway/llm_client.py:33`).
```bash
# .env:
LLM_SOURCE=dashscope   # dashscope | gemini | lmstudio | anthropic | claude_code | zai | acp
LLM_SOURCE=            # пусто = авто (lmstudio → gemini → dashscope)
LLM_ADAPTER=openai     # openai (по умолчанию, AsyncOpenAI с нативными tools) | custom (+ syntax guard мета-формата) | litellm | auto
```
После смены - `powershell -File start_sam.ps1` (рестарт).

## Архитектура (кратко)
- **Gateway**: FastAPI тонкий роутер (`gateway/main.py`)
- **Agent**: свой цикл `gateway/agent_loop.py`, вызовы через AsyncOpenAI, litellm - запасным (`LLM_ADAPTER`). Из пакета qwen_agent берутся только `BaseTool` и `register_tool` для схем tools (`gateway/agent.py:31`), его агент не используется
- **Оркестратор и воркеры** (ADR-005): главный агент разговаривает сам, действие на устройстве или поиск отдаёт `spawn_agent`, воркер выполняет в фоне и возвращает итог через `[TASK_DONE]`
- **Голосовая система**: звучит весь текст ответа, кроме парных блоков `<имя>...</имя>` (ADR-003, `gateway/speech_meta.py`)
- **История**: MongoDB нейтральный формат (`role: tool_call`), форматируется при чтении под модель
- **Qdrant API**: `.search()` устарел → использовать `.query_points(...).points`
- **LLM стратегии**: `harmony` (gpt-oss-20b, special tokens), `qwen`/`standard` (`<tool_call>` теги)

## Стек
- FastAPI + AsyncOpenAI + MongoDB + Qdrant + python-dotenv
- LM Studio (localhost:1234) → Gemini Flash → Qwen DashScope
- mcp_aggregator.py - несколько stdio MCP серверов → один HTTP порт
- Embeddings: paraphrase-multilingual-MiniLM-L12-v2 (CPU, офлайн)

## .env переменные
```bash
GEMINI_API_KEY=
ANTHROPIC_API_KEY=
DASHSCOPE_API_KEY=
LMSTUDIO_URL=http://127.0.0.1:1234
LMSTUDIO_MODEL=        # id модели (SAM-57); пусто - единственная загруженная, несколько - LM Studio пропускается, причина в /health
LLM_TEXT_TOOLS=1       # 0 - только нативные tools
GOOGLE_API_KEY=
GOOGLE_CX=
MONGODB_URI=mongodb://localhost:27017
QDRANT_URL=http://localhost:6333
SAM_SERVER=http://localhost:8000
MCP_PORT=3000
```
