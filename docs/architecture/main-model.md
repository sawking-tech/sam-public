# Основная модель

Шлюз отдаёт каждый вход - голос, чат монитора, inject - основной модели и не знает, кто за ней. Почему так -
ADR об основной модели (ADR-018). Где что лежит на стенде - `stand.md`.

`gateway/main_model.py`, сборка - `gateway/agent.py` (`main_models`):

- Список конкретных моделей по приоритету - `SAM_MAIN_MODELS`, по умолчанию `claude-code,llm`. Неизвестное имя
  или пустой список - ошибка на старте шлюза одной строкой.
- Контракт конкретной модели: имя, проверка доступности `available`, признак `in_gateway` (ответ рождается вызовом
  LLM в шлюзе), доставка `deliver` входа не из голоса и `commands` - её команды для подсказки монитора по `/`
  (SAM-99; у `claude-code` - `SAM_CLAUDE_CODE_COMMANDS`, у остальных пусто).
- Инертность: текущая модель остаётся, пока доступна; недоступна - берётся первая доступная по
  приоритету, событие `main_model_switch` уходит в монитор; никто не доступен - ход идёт текущей.
- Смена без рестарта (SAM-176): `POST /sam/main-model` с `main: claude-code|llm` (инструмент `switch_main_model`
  MCP-сервера `sam`) делает модель текущей и первой, порядок пишется в `SAM_MAIN_MODELS` в `.env`; недоступного
  ведущего не ставит. Маршрут требует ключа привилегий (`stand.md`, блок «Агенты»).
- Ярлык «ведёт» в мониторе и поле `main_model` в `/health` - текущая модель; событие `main_model_turn` - ход,
  отданный модели вне шлюза.

| Модель | in_gateway | Доступна | Как получает вход |
|---|---|---|---|
| `llm` | да | всегда | голосовой ход `/voice/turn` и чат - вызов модели роутера (`LLM_SOURCE`, быстрая - `LLM_FAST_*`) |
| `claude-code` | нет | `lead.alive` в шине моложе `SAM_LEAD_ALIVE_S` (5 с) | голос - тиком из `voice-in.jsonl`; чат монитора и inject - строкой в `lead-in.jsonl`, тикер приносит её тиком `chat` |
| `noop` | нет | всегда | никак: ход принят, ответа нет |

Шаг workflow стенда приходит тем же путём с источником `workflow:<имя>/<шаг>`: `claude-code` - строкой в
`lead-in.jsonl`, `llm` - ходом шлюза (`workflows.md`). Свои реплики SAM (поздний час) делает основная модель, не
отдельный вызов LLM роутера.

При `claude-code` голосовой ход шлюза основную модель не зовёт (`gateway/voice_turn.py`). Быстрый голос при
ведущем (SAM-173, `SAM_FAST_WITH_LEAD`, по умолчанию 1): быстрая модель роутера отвечает сразу или берёт паузу,
ведущий получает фразу тиком и ответ быстрой входом с источником `fast_reply` и делает по существу или
поправляет `say --fix`; workflow проверки быстрой (`fast-review`, `fast-action`, `fast-handoff`) при ведущем не идут (`where: {main: llm}`).
Быстрая не отвечает (холодная сессия или откат по `fast-voice.yml`, `SAM_FAST_WITH_LEAD=0`) - модель шлюза не
зовётся, отвечает сессия. Поиск по памяти идёт на входе до развилки при любой основной модели (SAM-101,
`gateway/recall.py`): сессия получает факты полем `facts` в тике `chat` или тиком `rag` к фразе (`stand.md`,
блок «Память»). История сжимается при любой основной модели: сжатие ставит запись реплики в историю,
а не ход шлюза, сводку пишет модель роутера (SAM-97, `stand.md`, блок «Память»).

## Claude Code - носитель SAM

Сессия Claude Code в роли основной модели и есть SAM (82a4764): история SAM - голос, чат монитора, ходы шлюза,
агенты - её история, обещания в ней - её обещания; переписка самой сессии после `/clear` пропадает. Сессия
действует инструментами стенда первым приоритетом, терминалом вторым (7bdd2dd).

Что сессия получает о себе - SessionStart-хук `.claude/hooks/lead_brief.py`: роль про среду (носитель, блоки среды
по строке, указатель на `docs/features/`), промпт SAM `gateway/prompts/lead.py` - те же разделы, что у модели шлюза,
импортом, в роде голоса из `GET /sam/config` (шлюз лежит - род по умолчанию и строка о причине; SAM-95), шаги старта, раздел «После /clear» из `current.md` и голос из шины за 30
минут (`SAM_LEAD_BRIEF_MINUTES`), между ними - выжимка памяти о доме из `GET /sam/memory/search` (SAM-101).
Хук срабатывает, если сессию открыл `start_sam.ps1` (`SAM_LEAD_SESSION=1`) или
тикер писал в шину последние 10 минут.

Живость - `scripts/lead_heartbeat.py`: фоновый процесс сессии трогает `lead.alive` раз в секунду, хук SessionEnd
удаляет файл. Ответ - `python -m voice.layer.say "текст"`: строка в `say-out.jsonl` и `POST
/sam/session/{id}/reply` - в монитор и историю. `--fix` - поправка, `--from haiku` - быстрый голос. Текст без звука
(схема псевдографикой, список) - `--text-only "текст"` или `--text-only -` с текстом в stdin: `POST
/sam/session/{id}/note`, в монитор моноширинно как есть и в историю с полем `channel: text`; модель видит его в
истории с пометкой `[текстом в монитор, не голосом]` (`gateway/runner.py`). Монитор - основной терминал основной
модели.

Дела SAM сессия делает инструментами MCP-сервера `sam` (SAM-175, `mcp/sam_server.py` поверх REST шлюза, строка в
локальном `.mcp.json`, в агрегатор стенда не входит): `memory_search`, `remember`, `create_agent`, `answer_agent`,
`grant_agent_tool`, `push_ack`, `switch_main_model`.

## Модели

Сессия-ведущий идёт на Sonnet - `"model": "sonnet"` в `.claude/settings.json`; Opus - агентам
кода (`SAM_CLAUDE_CODING_MODEL`, по умолчанию `opus`, `gateway/tools/coding_agents.py`); служебные вызовы
поставщика `claude_code` - `SAM_CLAUDE_LEAD_MODEL` (по умолчанию `sonnet`).

## Источник реплики

При `llm` шлюз до первой дельты хода шлёт мосту `{"type": "source", "source": "fast"|"main"}`, мост пишет строку
say-out с `from=haiku` за быструю модель и `from=gateway` за основную; поправка (`fix`) отзывает только `haiku`.
Озвучка переносит `from` в `said.jsonl`, тикер подписывает `unheard` полем `speaker` (`SAM · быстрая`,
`SAM · основная`, `SAM · ведущий`; `voice/layer/turns.py` `speaker_label`).

## Не сделано

- Приоритет реального времени по статистике сбоев, отказа от tools и бреда рядом с личным приоритетом человека - SAM-91.
- Составная проверка доступности, где таймаут хода перекрывает «зелёный» сигнал - SAM-91.
- Канал WebSocket к сессии вместо файла `lead.alive` - SAM-91.
- История по способностям модели: модели со своей памятью - только при подключении, слабой - окно и резюме;
  запрос содержимого истории сессии у шлюза - SAM-91.
- Проверка правил слуха и времени (SAM-95) прогоном по записанным ослышкам (evals SAM-81): правила в промптах шлюза
  и в брифе есть, поведение моделей на записях не замерено - SAM-95.
- Из `NO_LIES` и правил tools шлюза Claude Code не получает ничего: они названы через spawn_agent,
  общей формулировки для любой модели пока нет - SAM-95.
- Запись важного в память по триггеру (просьба «запомни», заметное событие) - SAM-94; раз в полчаса - workflow
  `memory-review` (`workflows.md`).

## Текстовый ход

Путь `/v1/chat/completions` через очередь сессии, дедуп STT, память и цикл агента.

```mermaid
sequenceDiagram
  autonumber
  participant U as Пользователь
  participant VC as клиент<br/>(chat.py, voice_client.py)
  participant GW as gateway/main.py<br/>FastAPI
  participant SL as session_lock.py<br/>(SAM-01)
  participant SD as stt_dedup.py<br/>(SAM-32)
  participant AG as SAMAgentRunner<br/>(главный агент)
  participant W as _run_worker<br/>(воркер таски)
  participant DB as MongoDB<br/>sessions / tasks
  participant Q as Qdrant RAG

  U->>VC: текст ("открой telegram")
  VC->>GW: POST /v1/chat/completions
  GW->>SD: deduplicate_stt(text, history)
  Note over SD: TF-IDF cosine vs последние 20<br/>user-сообщений сессии
  SD-->>GW: очищенный текст
  alt текст пустой после дедупа
    GW-->>VC: пустой ответ (тихо)
  else есть новое
    GW->>SL: acquire_and_run(session_id, text, handler)
    alt сессия занята
      SL->>SL: queue.append(text)
      SL-->>GW: "" (UI покажет в очереди)
    else свободна
      SL->>AG: run(text)
      AG->>DB: get_session_history(20)
      AG->>Q: get_rag_context(text)
      AG->>AG: _agent_loop (LLM + tools)
      alt action / поиск (правило из SYSTEM_PROMPT)
        AG->>W: spawn_agent → asyncio.create_task<br/>(_run_worker_task)
        W->>Q: RAG по описанию задачи (SAM-02)
        W->>W: _agent_loop с tools<br/>(search_web, mcp_call, ...)
        W-->>AG: report_status("ОТВЕТ: ...")
        AG->>AG: _auto_respond → перефраз<br/>в &lt;voice&gt;...&lt;/voice&gt;
      else разговорный ≤2 фразы (SAM-35)
        AG-->>AG: прямой &lt;voice&gt; БЕЗ spawn_agent<br/>1 LLM-вызов вместо 4
      else action-verb fallback (SAM-07 safety net)
        AG->>AG: модель забыла spawn_agent →<br/>второй turn форсит задачу,<br/>вступление озвучивается
      end
      AG-->>SL: voice-ответ
      SL->>SL: пока в queue что-то есть - склеить<br/>и обработать
      SL-->>GW: финальный voice
    end
    GW-->>VC: response
  end
```
