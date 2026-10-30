"""
SAM TaskRunner — исполнитель задач с этапами, DoD и streaming feedback.

После каждого этапа:
  ✓ DoD met → тихо идём дальше, шлём краткий статус
  ✗ DoD fail, решаемо → сам решает (retry / skip / replan), сообщает
  ✗ DoD fail, нерешаемо → спрашивает пользователя continue/skip/abort

Feedback идёт через async generator → FastAPI StreamingResponse (SSE).
"""

import asyncio
import re
import json
from datetime import datetime
from typing import AsyncGenerator

from .tasks import (
    get_task, save_task, update_task_status, update_stage,
    advance_stage, mark_task_running, format_task_summary, new_task, cancel_task
)

# Импортируем реализации инструментов из агента
from .agent import (
    _impl_search_web, _impl_mcp_call, _current_llm, _llm_call,
    _strip_thinking, _get_sem
)
from openai import AsyncOpenAI
from .event_bus import event_bus


# ─── Плановщик: текст → структурированные этапы ──────────────────────────────

PLANNER_PROMPT = """Ты планировщик задач SAM. Преобразуй запрос пользователя в список этапов с чёткими DoD.

Ответь ТОЛЬКО валидным JSON — массив этапов. Без markdown, без пояснений.

Схема одного этапа:
{
  "name": "snake_case_name",
  "description": "что делаем",
  "action": {"tool": "tool_name", "args": {"key": "value"}},
  "dod": "критерий готовности — что должно быть правдой после этапа",
  "dod_check": "regex:pattern ИЛИ mcp:tool_name:arg ИЛИ llm",
  "on_fail": "retry | skip | ask_user | abort",
  "max_retries": 2,
  "timeout_s": 30
}

Доступные инструменты для action.tool:
- search_web: {"query": "...", "intent": "youtube|general"}
- mcp_call: {"device": "...", "tool": "...", "args": {...}}

Доступные MCP инструменты на устройстве (control-сервер):
- run_command: {"command": "..."}   — запустить cmd команду (неблокирующий)
- powershell: {"script": "..."}     — powershell скрипт
- get_windows: {}                   — список открытых окон
- get_audio_state: {}               — что играет звук
- show_notification: {"message": "..."}

ПРАВИЛА выбора инструмента для открытия URL/программы:
- Открыть URL в браузере → run_command: {"command": "start https://..."}   ← ВСЕГДА ТАК
- browser_navigate НЕЛЬЗЯ использовать для открытия YouTube/медиа — он ждёт полной загрузки страницы и зависает
- browser_navigate — только для взаимодействия со страницей (клик, ввод текста)

Правила DoD:
- Будь конкретен: не "страница открылась" а "title окна содержит 'Яндекс Музыка'"
- dod_check "regex:pattern" — проверяем результат action регуляркой. Паттерн ТОЛЬКО по домену, без слов из названия трека/фильма. Примеры: "youtube\\.com/watch" (не "youtube.*название"), "music\\.yandex\\.ru" (не "yandex.*плейлист")
- dod_check "mcp:get_windows:{}" — вызываем MCP и проверяем DoD через LLM
- dod_check "llm" — LLM сам оценит результат

Пример для "открой яндекс музыку весенний плейлист":
[
  {
    "name": "find_playlist_url",
    "description": "Найти прямую ссылку на весенний плейлист ЯМ",
    "action": {"tool": "search_web", "args": {"query": "яндекс музыка весенний плейлист 2025 ссылка"}},
    "dod": "Найден URL содержащий music.yandex.ru и playlists",
    "dod_check": "regex:music\\.yandex\\.ru.*(playlists|playlist)",
    "on_fail": "retry",
    "max_retries": 2,
    "timeout_s": 15
  },
  {
    "name": "open_url",
    "description": "Открыть ссылку в браузере",
    "action": {"tool": "mcp_call", "args": {"device": "{device}", "tool": "run_command", "args": {"command": "start {url_from_find_playlist_url}"}}},
    "dod": "Окно браузера с Яндекс Музыкой открыто",
    "dod_check": "mcp:get_windows:{}",
    "on_fail": "retry",
    "max_retries": 1,
    "timeout_s": 10
  },
  {
    "name": "verify_music_playing",
    "description": "Проверить что музыка играет",
    "action": {"tool": "mcp_call", "args": {"device": "{device}", "tool": "get_audio_state", "args": {}}},
    "dod": "Аудио сессия активна в браузере",
    "dod_check": "llm",
    "on_fail": "ask_user",
    "max_retries": 0,
    "timeout_s": 10
  }
]"""


async def plan_task(description: str, device: str, llm_config: dict = None) -> list[dict]:
    """LLM преобразует описание задачи в структурированные этапы."""
    from .llm_router import get_best_llm, claude_code_call, _check_lmstudio, _qwen_config
    from openai import RateLimitError as _RateLimitError

    messages = [
        {"role": "system", "content": PLANNER_PROMPT},
        {"role": "user", "content": f"Устройство: {device}\nЗадача: {description}"},
    ]

    # Пробуем провайдеры по порядку с fallback при rate limit
    tried = set()
    for _ in range(4):
        cfg = await get_best_llm(task="planning", skip_sources=tried)
        if not cfg:
            break
        print(f"[Planner] Using {cfg['source']}: {cfg['model']}")
        try:
            if cfg["source"] == "claude_code":
                resp = await claude_code_call(messages, max_tokens=1500)
            else:
                client = AsyncOpenAI(
                    base_url=cfg["model_server"],
                    api_key=cfg.get("api_key", "x"),
                )
                resp = await _llm_call(client, model=cfg["model"], messages=messages,
                                       temperature=0.1, max_tokens=1500)
            raw = _strip_thinking(resp.choices[0].message.content.strip())
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            try:
                stages = json.loads(raw)
                return stages if isinstance(stages, list) else []
            except Exception as e:
                print(f"[Planner] JSON parse error: {e}\nRaw: {raw[:200]}")
                return []
        except _RateLimitError:
            print(f"[Planner] {cfg['source']} rate limited — trying next")
            tried.add(cfg["source"])
        except Exception as e:
            print(f"[Planner] {cfg['source']} error: {e} — trying next")
            tried.add(cfg["source"])

    print("[Planner] All providers exhausted")
    return []


# ─── DoD верификация ──────────────────────────────────────────────────────────

async def verify_dod(stage: dict, action_result: str, context: dict, llm_config: dict) -> tuple[bool, str]:
    """
    Проверяет DoD этапа. Возвращает (met: bool, reason: str).
    """
    dod = stage.get("dod", "")
    dod_check = stage.get("dod_check", "llm")

    if not dod_check or dod_check == "llm":
        return await _llm_dod_check(dod, action_result, llm_config)

    if dod_check.startswith("regex:"):
        pattern = dod_check[6:]
        text_to_check = action_result + " " + json.dumps(context)
        met = bool(re.search(pattern, text_to_check, re.IGNORECASE))
        return met, ("regex matched" if met else f"regex '{pattern}' not found in result")

    if dod_check.startswith("mcp:"):
        parts = dod_check.split(":", 2)
        tool = parts[1] if len(parts) > 1 else ""
        args_str = parts[2] if len(parts) > 2 else "{}"
        try:
            args = json.loads(args_str)
        except Exception:
            args = {}
        device = context.get("device", "")
        mcp_result = await _impl_mcp_call({"device": device, "tool": tool, "args": args})
        # LLM оценивает результат MCP против DoD
        return await _llm_dod_check(dod, mcp_result, llm_config)

    return True, "no check defined"


async def _llm_dod_check(dod: str, result: str, llm_config: dict) -> tuple[bool, str]:
    client = AsyncOpenAI(
        base_url=llm_config["model_server"],
        api_key=llm_config.get("api_key", "x"),
    )
    resp = await _llm_call(
        client,
        model=llm_config["model"],
        messages=[
            {"role": "system", "content": "Ты верификатор. Последнее слово ответа ДОЛЖНО быть PASS или FAIL. Пример: 'URL найден PASS' или 'URL не найден FAIL'"},
            {"role": "user", "content": f"DoD: {dod}\n\nРезультат действия:\n{result[:800]}"},
        ],
        temperature=0.0,
        max_tokens=200,
    )
    text = _strip_thinking(resp.choices[0].message.content.strip())
    # Ищем PASS/FAIL — последнее слово (как в guard) или первое
    words = text.upper().split()
    last_word = words[-1] if words else ""
    first_word = words[0] if words else ""
    if "PASS" in last_word:
        met, verdict = True, last_word
    elif "FAIL" in last_word:
        met, verdict = False, last_word
    elif "PASS" in first_word:
        met, verdict = True, first_word
    else:
        met, verdict = False, first_word
    reason = text[:100]
    return met, reason


# ─── Подстановка контекста в args ─────────────────────────────────────────────

def _resolve_args(args: dict, context: dict) -> dict:
    """Подставляет {var_from_prev_stage} из контекста в аргументы."""
    resolved = {}
    for k, v in args.items():
        if isinstance(v, str):
            for ctx_k, ctx_v in context.items():
                v = v.replace(f"{{{ctx_k}}}", str(ctx_v))
        elif isinstance(v, dict):
            v = _resolve_args(v, context)
        resolved[k] = v
    return resolved


# ─── Исполнение одного этапа ──────────────────────────────────────────────────

async def execute_stage_action(stage: dict, context: dict) -> str:
    action = stage["action"]
    tool = action["tool"]
    args = _resolve_args(action.get("args", {}), context)

    if tool == "search_web":
        return await _impl_search_web(args)
    elif tool == "mcp_call":
        return await _impl_mcp_call(args)
    else:
        return f"Unknown tool: {tool}"


# ─── Главный runner со streaming feedback ─────────────────────────────────────

async def run_task(
    db,
    task_id: str,
    llm_config: dict,
    user_decision_callback=None,   # async fn(question) → "continue"|"skip"|"abort"
) -> AsyncGenerator[str, None]:
    """
    Исполняет задачу поэтапно. Yields строки с промежуточными статусами.
    user_decision_callback вызывается когда нужно решение пользователя.
    """
    task = get_task(db, task_id)
    if not task:
        yield f"❌ Task {task_id} not found\n"
        return

    mark_task_running(db, task_id)
    yield f"▶ Начинаю: {task['description']}\n"

    context = {"device": task.get("device", "")}
    stages = task["stages"]

    for stage in stages:
        # Проверяем не отменена ли задача
        fresh = get_task(db, task_id)
        if fresh and fresh.get("status") == "cancelled":
            yield "⛔ Задача отменена пользователем.\n"
            event_bus.emit("task_cancelled", {"task_id": task_id[:8]})
            return

        idx = stage["idx"]
        name = stage["name"]
        attempt = 0
        max_retries = stage.get("max_retries", 2)

        while attempt <= max_retries:
            update_stage(db, task_id, idx, {
                "status": "running",
                "started_at": datetime.utcnow(),
                "retries": attempt,
            })

            if attempt == 0:
                yield f"  ⟳ [{idx+1}/{len(stages)}] {name}: {stage['description']}\n"
            else:
                yield f"  ↺ [{idx+1}] {name}: повтор {attempt}/{max_retries}\n"
            event_bus.emit("stage_update", {
                "task_id": task_id[:8], "name": name, "idx": idx, "total": len(stages),
                "status": "running", "attempt": attempt, "message": stage.get("description", ""),
            })

            # Выполняем действие
            try:
                result = await asyncio.wait_for(
                    execute_stage_action(stage, context),
                    timeout=stage.get("timeout_s", 30),
                )
            except asyncio.TimeoutError:
                result = f"Timeout ({stage.get('timeout_s')}s)"
            except Exception as exc:
                result = f"Ошибка: {exc}"

            # Извлекаем URL из результата для контекста следующих этапов
            urls = re.findall(r'https?://\S+', result)
            if urls:
                context[f"url_from_{name}"] = urls[0]
            context[f"result_from_{name}"] = result[:500]

            # Проверяем DoD
            dod_met, dod_reason = await verify_dod(stage, result, context, llm_config)

            if dod_met:
                update_stage(db, task_id, idx, {
                    "status": "done",
                    "result": result[:1000],
                    "done_at": datetime.utcnow(),
                })
                yield f"  ✓ {name}: готово\n"
                event_bus.emit("stage_update", {
                    "task_id": task_id[:8], "name": name, "idx": idx, "total": len(stages),
                    "status": "done", "attempt": attempt,
                })
                break

            # DoD не выполнен
            attempt += 1
            on_fail = stage.get("on_fail", "retry")

            if attempt <= max_retries and on_fail == "retry":
                yield f"  ⚠ {name}: {dod_reason[:80]} — retry {attempt}\n"
                continue

            # Исчерпали попытки — решаем что делать
            if on_fail == "skip":
                update_stage(db, task_id, idx, {"status": "skipped", "error": dod_reason})
                yield f"  → {name}: пропущен ({dod_reason[:60]})\n"
                event_bus.emit("stage_update", {
                    "task_id": task_id[:8], "name": name, "idx": idx, "total": len(stages),
                    "status": "skipped", "message": dod_reason[:80],
                })
                break

            elif on_fail == "abort":
                update_stage(db, task_id, idx, {"status": "failed", "error": dod_reason})
                update_task_status(db, task_id, "failed", error=f"Stage {name} failed: {dod_reason}")
                yield f"  ✗ {name}: критическая ошибка — {dod_reason[:80]}\nЗадача прервана.\n"
                event_bus.emit("stage_update", {
                    "task_id": task_id[:8], "name": name, "idx": idx, "total": len(stages),
                    "status": "failed", "message": dod_reason[:80],
                })
                event_bus.emit("task_failed", {"task_id": task_id[:8], "reason": dod_reason[:80]})
                return

            elif on_fail == "ask_user":
                question = f"Этап '{name}' не выполнен: {dod_reason[:100]}. Продолжить (skip) или отменить (abort)?"
                yield f"  ❓ {question}\n"

                decision = "skip"  # дефолт если нет callback
                if user_decision_callback:
                    try:
                        decision = await asyncio.wait_for(user_decision_callback(question), timeout=30)
                    except asyncio.TimeoutError:
                        yield "  ⏱ Таймаут ответа — пропускаю этап\n"

                if decision == "abort":
                    update_stage(db, task_id, idx, {"status": "failed", "error": dod_reason})
                    update_task_status(db, task_id, "failed", error="Отменено пользователем")
                    yield "Задача отменена.\n"
                    event_bus.emit("task_failed", {"task_id": task_id[:8], "reason": "Отменено пользователем"})
                    return
                else:
                    update_stage(db, task_id, idx, {"status": "skipped", "error": dod_reason})
                    yield f"  → {name}: пропущен по решению пользователя\n"
                    break

            else:  # replan или unknown
                update_stage(db, task_id, idx, {"status": "failed", "error": dod_reason})
                yield f"  ✗ {name}: не выполнен ({dod_reason[:60]})\n"
                event_bus.emit("stage_update", {
                    "task_id": task_id[:8], "name": name, "idx": idx, "total": len(stages),
                    "status": "failed", "message": dod_reason[:80],
                })
                break

        advance_stage(db, task_id, idx + 1)

    update_task_status(db, task_id, "done", result="Все этапы завершены")
    yield f"✅ Задача завершена: {task['description']}\n"
    yield format_task_summary(get_task(db, task_id)) + "\n"
    event_bus.emit("task_done", {"task_id": task_id[:8], "description": task["description"]})
