# Агент кода

Агент кода — внешний CLI, которому SAM отдаёт текстовое задание: правку файлов, git, скрипты, разбор незнакомой кодовой
базы. Это отдельный исполнитель, а не воркер шлюза и не агент Cortex: у него нет истории SAM, своих инструментов стенда
и ключей доступа. Инструмент `claude_delegate` есть у воркера и нет у главного агента, потому что правка кода — работа
фона.

`gateway/tools/coding_agents.py` держит реестр каркасов и `delegate_impl`; `gateway/command_auth.py` решает, когда
делегирование требует ключа; `gateway/core_paths.py` отличает ядро SAM от периферии.

## Каркасы

Поддерживаются Claude Code и OpenCode. Claude Code запускается как `claude -p <задача> --output-format json
--dangerously-skip-permissions` с моделью из `SAM_CLAUDE_CODING_MODEL`, OpenCode — как `opencode run <задача>
--dangerously-skip-permissions`. Подтверждений не спрашивают: у процесса нет stdin. CLI ищется в `PATH` или берётся из
`SAM_CLAUDE_CLI` и `SAM_OPENCODE_CLI`. Каркас выбирает явный параметр `agent`, иначе слово в задаче («через OpenCode»),
иначе `SAM_CODING_AGENT`, иначе первый доступный в порядке claude → opencode.

Ответ Claude Code приходит JSON-ом с полями `result` и `is_error`; не JSON — вывод отдаётся как есть. Ошибка
возвращается одной строкой: что не так и что делать.

## Папка и таймаут

Работать можно только внутри корней из `SAM_CODING_ROOTS` (через `os.pathsep`); по умолчанию корень один — папка
репозиториев, где лежит SAM. `workdir` — путь внутри корня, а не задан — первый корень. Таймаут задаёт
`SAM_CODING_TIMEOUT_S`, потолок — `SAM_CODING_MAX_TIMEOUT_S`; по таймауту CLI убивается вместе с дочерними процессами
(`taskkill /T` на Windows, группа процессов на POSIX, `gateway/proc_tree.py`).

## Окружение и права

Ребёнку не передаются секреты SAM: `_agent_env` выкидывает переменные с `KEY`, `SECRET` или `PRIVILEGE` в имени и
`CLAUDECODE`, иначе `claude` не запустится вне нашей сессии. Вместо `.mcp.json` проекта подключается один канал к
создателю (`gateway/tools/agent_channel.py`, ключ `--strict-mcp-config`): `ask_creator`, `report_status` и отправка
сообщений. MCP `sam` с ключом привилегий агенту кода не достаётся.

Правка ядра SAM требует ключа команд. Ядро — весь репозиторий SAM, кроме папок периферии из `SAM_PERIPHERY_DIRS` (по
умолчанию `data/apps` и `data/worktrees`); всё вне репозитория SAM — периферия. Решает `touches_core`. Делегирование в
ядро входит в `PRIVILEGED_TOOLS` и выполняется только в ходе человека, где назван `SAM_COMMAND_SECRET`. Агент идёт вне
такого хода, поэтому получает `AGENT_REFUSED` из `gateway/command_auth.py`: подготовить разбор, план и diff и вернуть
создателю.

## Проверка

`tests/test_coding_agents.py`.
