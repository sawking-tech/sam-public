# Агентная система за ACP и агенты Cortex

Шлюз держит агента подпроцессом и говорит с ним по ACP (Agent Client Protocol, JSON-RPC по stdio); свой агент SAM -
Cortex, внешние (Hermes, OpenCode) подключаются тем же контрактом. Агент SAM для задачи может работать воркером шлюза
или сессией Cortex с правами, выданными создателем. Почему так - ADR-022; воркер шлюза и пул шагов - `workers.md`.

Клиент ACP в шлюзе - `gateway/acp/`, Cortex - `cortex/acp/`, агенты Cortex - `gateway/cortex_workers.py`,
`cortex/acp/rights.py`, `cortex/acp/channels.py`, инструменты создания - `gateway/tools/agents.py`.

## Кто ведёт главный ход

| Что | Как |
|---|---|
| выбор | `SAM_AGENT_BACKEND`: `sam` - свой цикл в шлюзе (по умолчанию), `acp` - агент за ACP; откат одной строкой |
| запуск агента | `SAM_ACP_COMMAND`, `SAM_ACP_ARGS`, `SAM_ACP_CWD`; Cortex - `SAM_ACP_COMMAND=python`, `SAM_ACP_ARGS=-m cortex.acp` |
| MCP в сессии | шлюз передаёт агенту MCP SAM (`SAM_ACP_MCP_NAME`, `SAM_ACP_MCP_URL`) |
| окружение агента | только переменные из `SAM_ACP_ENV` поверх безопасного минимума SDK; своя папка агента - `SAM_ACP_HOME`, `SAM_ACP_HOME_VAR`, `SAM_ACP_HOME_TEMPLATE` |
| поток | `session/update` пересказывается в шину голоса и монитора; `session/request_permission` решает судья SAM |
| сбой | агент упал - закрытый stdio, ход завершается ошибкой, агент поднимается заново; старт не дольше `SAM_ACP_START_TIMEOUT_S` (60 с) |

В роутере агент за ACP - поставщик `acp` для видов задач `turn` и `fast` (`llm-router.md`).

## Cortex

| Что | Как |
|---|---|
| протокол и инструменты | SDK `agent-client-protocol` (агентская сторона), инструменты - MCP сессии через SDK `mcp`, модель - `llm_client` шлюза задачей `SAM_CORTEX_LLM_TASK` (`default`; сам себя, `acp`, не выбирает) |
| ход | тот же `agent_loop`, что в шлюзе (`agent-loop.md`); шагов цикла на ход `SAM_CORTEX_MAX_TURNS` (20) |
| поток | текст - `agent_message_chunk`, вызов и итог инструмента - `tool_call` и `tool_call_update`; рассуждение в ответ не попадает |
| разрешения | инструменты, которые меняют машину (`SAM_CORTEX_PERMISSION_TOOLS`: команды, запись файлов, git на запись, код в браузере), и помеченные `destructiveHint` в MCP идут через `session/request_permission`, то есть через судью SAM; чтение, радар и время - без судьи; политика `deny` сильнее всего |
| подсказка | текст хода от шлюза целиком, сверху короткая рамка Cortex; своей истории у Cortex нет |
| поиск инструментов | больше `SAM_CORTEX_TOOL_SEARCH_MIN` (20) инструментов в сессии - модель видит `load_skill` (`SAM_CORTEX_CORE_TOOLS`) и мост `tool_search` со списком имён; найденное поиском, вызванное по имени или названное скиллом приходит со следующего шага полной схемой. Поиск - BM25 по основам слов (`rank-bm25`, `snowballstemmer`), `SAM_CORTEX_TOOL_SEARCH_LIMIT` (5) находок |
| поздние реплики | реплика, пришедшая во время финального ответа, продолжает ход не больше `SAM_CORTEX_LATE_INJECTS` (3) раз |

Замер 12 окт: описания 70 инструментов стенда - 36 950 знаков на каждый вызов модели, с поиском - 1 900-4 160.

## Агенты SAM в Cortex

Агент создаётся `POST /sam/agents` или инструментом `create_agent` MCP `sam` с `framework: cortex` (SAM-133). Каркас
по умолчанию - `SAM_AGENT_FRAMEWORK`, пусто - воркер шлюза, пока Cortex не прошёл живой прогон. `spawn_agent` выдачу
инструментов не передаёт, поэтому с каркасом `cortex` отказывает.

| Что | Как |
|---|---|
| права | инструменты выдаёт создатель списком, не шире своих; пустая выдача у Cortex отклоняется (`CORTEX_NEEDS_GRANT`): только каналы связи и `tool_search` - нужно явно; у воркера шлюза пустой список по-прежнему даёт все его инструменты |
| метка сложности | `smart`, `medium` (по умолчанию), `weak`: источник роутера из `SAM_AGENT_LLM_<МЕТКА>`, пусто - подборщик по задаче ([llm-router](llm-router.md), «Модель под задачу агента») |
| каналы | `report_status` и `ask_creator` создателю, `request_permission` (инструмент и зачем), `send_to_agent` - только агентам из `peers` или после выданного права `agent:<id>`; ответ создателя приходит в ход `_sam/inject` |
| дети | глубина `depth` до `SAM_AGENT_MAX_DEPTH` (1), детей `SAM_AGENT_MAX_CHILDREN` (1); уровень считается от основной модели: её агент создаёт не больше одного агента, тот - ни одного; создав ребёнка, агент теряет `spawn_agent`; шлюз перепроверяет пределы по своей записи задачи |
| права ребёнка | не шире прав родителя; запрос права ребёнка решает родитель, чего у родителя нет - пересылает выше |
| жизнь | агент живёт задачу: пока идут дети или ждёт ответ, следующий ход - по сообщению; ходов на задачу `SAM_CORTEX_TASK_TURNS` (10); нет ответа за `SAM_CORTEX_WAIT_S` (600 с) - задача закрывается итогом последнего хода; итог создателю - `[TASK_DONE]` |
| шаги | пул шагов считает цикл воркера шлюза (`workers.md`); сессию Cortex ограничивают ходы на задачу и шаги цикла на ход |
| ключ привилегий | `POST /sam/agents`, `/sam/agents/{id}/grant`, `/sam/main-model`, `/sam/llm/switch` требуют `X-SAM-Privilege` и с этой машины (`gateway/privilege_key.py`, SAM-177); ключ в `data/secrets/privilege.key` (`SAM_PRIVILEGE_FILE`) передаёт только MCP `sam` ведущего; инструмент с `privilege.key` в аргументах запрещён политикой (`gateway/tool_policy.json`) |

Агенты основной модели вне шлюза идут от сессии `SAM_AGENT_PARENT_SESSION`.

## Hermes

Прокси-режим держится конфигом `gateway/acp/hermes/config.yaml` (пустой `platform_toolsets.acp`, отключённые наборы,
память выключена), только в Hermes не старше 7 окт: в версии 24 июля пустой список наборов становится полным набором.
Установка - venv клона с опциями `acp` и `mcp` (`uv sync --extra acp --extra mcp`, Python 3.14); без `mcp` сессия
молча остаётся без инструментов. Hermes требует окно модели от 64 тыс. токенов, иначе ACP отдаёт «Internal error»;
локальная модель грузится с `--context-length 65536`. Проба - `scripts/acp_probe.py`.

## Рабочее место и план

Инструмент `workspace` (SAM-234) открывает агенту ветку `agent/<имя>` от dev в `data/worktrees`. Инструмент `plan` (SAM-235)
ведёт план одним вызовом: `where`, `check`, `take`, `done`, `add`; интерпретатор и путь к `plan.mjs` берёт сам
(`SAM_PLAN_TOOL`, файл плана - `SAM_PLAN_FILE`), оба в наборе основной модели и воркера.

## Итог агента принимается с артефактом

Шлюз сверяет итог воркера перед статусом `done` (`gateway/finish_contract.py`, SAM-237). Пустое «Готово.» и текст короче
`SAM_RESULT_MIN_CHARS` (15) не принимаются; задача кода (в описании коммит, рабочее место, ветка `agent/`) обязана назвать
хэш коммита, который существует в репозитории (`git cat-file`). Не прошло - статус `unverified`, итог начинается с
`[НЕ ПОДТВЕРЖДЕНО]` и причины, создатель получает его как обычный итог и может ответить агенту (воскрешение, как для вопроса).

## Проверка

`tests/test_acp.py`, `tests/test_cortex_acp.py`, `tests/test_cortex_rights.py`, `tests/test_cortex_workers.py`,
`tests/test_agents_api.py`, `tests/test_tools_workspace.py`, `tests/test_tools_plan.py`, `tests/test_finish_contract.py`.
