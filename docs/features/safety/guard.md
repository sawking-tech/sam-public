# Guard: что SAM не выполнит

Перед исполнением инструмента вызов проходит до трёх проверок. Уровень 0 - политика по имени и аргументам,
уровень 1 - паттерны опасных команд, уровень 2 - судья на модели. Голосовой ход guard не проходит: ему дан один
инструмент `spawn_agent`, действие уходит воркеру, проверки остаются там ([быстрая-модель-и-поправка](features/voice/speech.md), ADR-010).

`gateway/tool_policy.py` (`gateway/tool_policy.json`, `SAM_TOOL_POLICY`), `gateway/guard.py`,
`gateway/readonly_command.py`, `gateway/turn_taint.py`.

## Уровень 0: политика

Правило `tool(шаблон аргументов) -> allow | ask | deny`; из подходящих побеждает последнее. `deny` - вызов не
исполняется, `ask` - идёт к судье уровня 2.

| По умолчанию | Что |
|---|---|
| `mcp_call` с `powershell`, `run_command`, `bg_start`, `open_terminal` с командой | `ask` |
| `set_env` | `deny`, кроме `PLAYWRIGHT_MCP_EXTENSION_TOKEN` |
| любой инструмент, в аргументах которого назван `privilege.key` | `deny` (SAM-177) |

Команда только на чтение из списка `read_only_commands` (`gateway/readonly_command.py`, SAM-127) идёт без судьи.

## Уровень 1: паттерны

Мгновенно, для любого инструмента: `rm -rf`, `format c:`, `del /s /q`, `chmod 777`, `curl | bash`, `/etc/shadow`,
`DROP TABLE`, `> /dev/sda`, `mkfs`, fork-бомба. Совпало - вызов заблокирован.

## Уровень 2: судья

Вопрос модели «SAFE или UNSAFE?», ответ читается последним словом после `strip <think>`. Зовётся для
`claude_delegate` и для всего, что политика пометила `ask`. Сбой судьи - блок.

## Уровень 3: синтаксис

Ответ без нативных вызовов проверяется на вызов в неверном формате (`custom` адаптер, [agent-loop](architecture/gateway/agent-loop.md)): найден -
блок и повтор с подсказкой.

## Защита цикла

| Что | Как |
|---|---|
| один инструмент 3 раза подряд | подсказка с накопленными результатами, событие `loop_guard` |
| 5 раз подряд | следующий вызов модели с `tool_choice="none"`: только ответ |
| опрос без прогресса | тот же вызов с тем же результатом `SAM_NO_PROGRESS_LIMIT` (3) раза, событие `no_progress` |
| повтор вызова | тот же вызов за прогон не исполняется повторно, кроме `READONLY_TOOLS` |

У шлюзового `mcp_call` повтор считается по вызову целиком - имени и аргументам (`agent_loop.loop_signature`):
пять разных команд подряд - законная работа, а не цикл (28 окт: 52 из 53 принудительных завершений приходились на
`mcp_call`).

## Пределы агентов

Не больше `SAM_MAX_WORKERS` (4) воркеров одновременно; лишний `spawn_agent` получает отказ одной строкой.
Глубина - 1: у воркера шлюза нет `spawn_agent`; агент Cortex создаёт не больше одного ребёнка, тот - ни одного
(`SAM_AGENT_MAX_DEPTH`, `SAM_AGENT_MAX_CHILDREN`, по 1; ADR-022).

Шаги - пул на дерево (`gateway/agent_budget.py`, ADR-022). Шаг - одно обращение агента к модели. Пул задачи -
меньшая из прикидки создателя (`steps`, по умолчанию `SAM_AGENT_STEPS`, 30, не больше `SAM_AGENT_STEPS_CAP`, 100)
и оценки самого агента (`estimate_steps`). Ребёнку пул выделяется из остатка родителя; на доле
`SAM_AGENT_BUDGET_WARN` (0,8) агент получает подсказку, не хватает - `request_steps` с причиной, создатель выдаёт
`grant_steps` или отказывает; неистраченное закрытой задачи возвращается родителю. Оборот, в котором агент зовёт
только каналы (`ask_creator`, `request_steps`, `estimate_steps`, `report_status`, `send_to_agent`, `grant_steps`),
шага не стоит; таких обращений на задачу не больше `SAM_AGENT_CHANNEL_CALLS` (10).

## Проверка

`tests/test_guard.py`, `tests/test_guard_scope.py`, `tests/test_tool_policy.py`, `tests/test_readonly_command.py`,
`tests/test_agent_steps.py`, `tests/test_agent_steps_out.py`.
