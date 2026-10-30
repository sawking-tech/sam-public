"""
LLM Client — adapter layer между agent loop и провайдерами.

Адаптеры:
  openai   — прямой AsyncOpenAI с нативными tools (дефолт), как у Hermes и openclaw: у всех поставщиков
             SAM OpenAI-совместимый адрес (LM Studio, vLLM, z.ai, DashScope, Gemini, Anthropic)
  custom   — тот же AsyncOpenAI + проверка синтаксиса вызовов из текста (мета-формат, LLM_TEXT_TOOLS)
  litellm  — через litellm, если понадобится поставщик без OpenAI-совместимого адреса
  auto     — openai; при parse fail × THRESHOLD на local → custom

Переключение:
  .env LLM_ADAPTER=openai|custom|litellm|auto
  POST /sam/llm/switch  {"adapter": "...", "source": "..."}
  python -m gateway.cli llm switch <adapter>
"""
import os
import inspect
import time
import uuid
import asyncio
import weakref
import contextlib
from typing import Callable
from openai import AsyncOpenAI

from pathlib import Path

from . import cooldown_store, llm_monitor, reasoning, token_usage
from .llm_gate import PriorityGate
from .env import env_bool, env_float, env_int

# ─── Adapter state ────────────────────────────────────────────────────────────

ADAPTERS = ("openai", "custom", "litellm", "auto")
DEFAULT_ADAPTER = "openai"
_adapter: str = os.getenv("LLM_ADAPTER") or DEFAULT_ADAPTER
NO_NATIVE_TOOLS_FORMATS = ("harmony",)   # форматы, которым нативные tools не передаются (llm_router.TEXT_ONLY_FORMATS)
MISSING_LIBRARY_STATUS = 500             # нет библиотеки адаптера: как сбой поставщика - строка в лог и замена
_parse_fail_streak: int = 0
PARSE_FAIL_THRESHOLD = env_int("LLM_PARSE_FAIL_THRESHOLD", 3)

# ─── Semaphore ────────────────────────────────────────────────────────────────

LLM_CONCURRENCY = env_int("LLM_CONCURRENCY", 4)
# Просить usage в последнем чанке стрима (stream_options.include_usage, SAM-48).
# Выключено по умолчанию: поддержку у LM Studio, Gemini и z.ai через litellm не
# проверяли вживую, неизвестный параметр может дать 400. Без него стрим записывается
# как вызов без токенов.
STREAM_INCLUDE_USAGE = env_bool("LLM_STREAM_INCLUDE_USAGE", False)
MS_PER_SECOND = 1000

# Сроки ожидания поставщика (SAM-73). Не замерены: взяты с запасом над обычным ходом, чтобы
# срабатывать только на поставщике, который принял запрос и замолчал. Срабатывание - ошибка
# поставщика с кодом TIMEOUT_STATUS: cooldown как у 5xx и замена по цепочке роутера.
CALL_TIMEOUT_S = env_float("LLM_CALL_TIMEOUT_S", 120.0)              # ответ без стрима целиком
FIRST_TOKEN_TIMEOUT_S = env_float("LLM_FIRST_TOKEN_TIMEOUT_S", 60.0)  # стрим: запрос -> первый чанк
DELTA_TIMEOUT_S = env_float("LLM_DELTA_TIMEOUT_S", 30.0)              # стрим: пауза между чанками
RUNAWAY_FINISH = "runaway"   # finish_reason потока, оборванного стражем разноса (SAM-246): ответ - собранное до обрыва
TIMEOUT_STATUS = 504  # Gateway Timeout: classify_error относит его к server
_llm_sem: PriorityGate | None = None
# Свой предел параллельных вызовов поставщика сверх общего: LLM_CONCURRENCY_ZAI, LLM_CONCURRENCY_LMSTUDIO.
# Не задан - поставщика ограничивает только общий LLM_CONCURRENCY
SOURCE_CONCURRENCY_ENV = "LLM_CONCURRENCY_{source}"
_source_sems: dict[str, PriorityGate | None] = {}


def _get_sem() -> PriorityGate:
    """Места вызова LLM; голосовой ход получает освободившееся место первым (SAM-84, llm_gate)."""
    global _llm_sem
    if _llm_sem is None:
        _llm_sem = PriorityGate(LLM_CONCURRENCY)
    return _llm_sem


def _get_source_sem(source: str) -> PriorityGate | None:
    """Места своего поставщика, если его предел задан настройкой; None - только общие."""
    if source not in _source_sems:
        slots = env_int(SOURCE_CONCURRENCY_ENV.format(source=source.upper()), 0)
        _source_sems[source] = PriorityGate(slots) if slots > 0 else None
    return _source_sems[source]


@contextlib.asynccontextmanager
async def _call_slots(source: str):
    """Место поставщика (если предел задан), затем общее: ждущий своего поставщика не держит общее место."""
    async with contextlib.AsyncExitStack() as stack:
        if (own := _get_source_sem(source)) is not None:
            await stack.enter_async_context(own)
        await stack.enter_async_context(_get_sem())
        yield

# ─── Public API ───────────────────────────────────────────────────────────────

# Поставщики не по HTTP (ACP-агент, ADR-022): источник -> вызов (конфиг, **параметры запроса) с ответом в виде OpenAI;
# заполняет корень композиции, у прочих источников - litellm или custom
_source_calls: dict[str, Callable] = {}


def register_source_call(source: str, call: Callable) -> None:
    _source_calls[source] = call


def set_adapter(adapter: str) -> None:
    global _adapter, _parse_fail_streak
    if adapter not in ADAPTERS:
        raise ValueError(f"Unknown adapter: {adapter!r}. Use: {' | '.join(ADAPTERS)}")
    _adapter = adapter
    _parse_fail_streak = 0  # сбросить при ручном переключении


def inc_parse_fail() -> bool:
    """Зафиксировать провал парсинга. Возвращает True если достигли порога."""
    global _parse_fail_streak
    _parse_fail_streak += 1
    reached = _parse_fail_streak >= PARSE_FAIL_THRESHOLD
    if reached:
        print(f"[LLMClient] ERROR parse_fail={_parse_fail_streak} >= threshold={PARSE_FAIL_THRESHOLD}")
    return reached


def reset_parse_fail() -> None:
    global _parse_fail_streak
    _parse_fail_streak = 0


def get_status(current_llm: dict | None = None) -> dict:
    """current_llm - конфиг последнего хода (его держит agent): адаптер не тянет слой агента импортом."""
    _current_llm = current_llm
    return {
        "adapter": _adapter,
        "effective_adapter": _effective_adapter(_current_llm or {}),
        "parse_fail_streak": _parse_fail_streak,
        "parse_fail_threshold": PARSE_FAIL_THRESHOLD,
        "current_source": (_current_llm or {}).get("source"),
        "current_model": (_current_llm or {}).get("model"),
    }

# ─── Internal ─────────────────────────────────────────────────────────────────

def _effective_adapter(llm_config: dict) -> str:
    """Реальный адаптер с учётом auto-режима."""
    if _adapter != "auto":
        return _adapter
    # auto: при parse fail на local → переключаем на custom
    if _parse_fail_streak >= PARSE_FAIL_THRESHOLD:
        source = llm_config.get("source", "")
        if source in ("lmstudio", "vllm"):
            return "custom"
    return DEFAULT_ADAPTER


def _litellm_model(llm_config: dict) -> str:
    """Конвертирует llm_config в model string для litellm."""
    source = llm_config.get("source", "")
    model = llm_config.get("model", "")
    if source in ("lmstudio", "vllm"):
        # openai-compatible endpoint
        return model if model.startswith("openai/") else f"openai/{model}"
    if source == "gemini":
        return model if model.startswith("gemini/") else f"gemini/{model}"
    if source == "anthropic":
        return model if model.startswith("anthropic/") else f"anthropic/{model}"
    if source in ("dashscope", "zai", "openrouter", "polza"):
        # OpenAI-совместимый вход: префикс litellm "openai/", id модели агрегатора (там свой "openai/gpt-...") идёт следом
        return f"openai/{model}"
    # claude_code, прочие — передаём как есть
    return model


# ─── Provider error ───────────────────────────────────────────────────────────

class LLMProviderError(Exception):
    """HTTP 403/429/500 от провайдера — сигнал для fallback."""
    def __init__(self, source: str, status_code: int, message: str = ""):
        self.source = source
        self.status_code = status_code
        super().__init__(f"[{source}] HTTP {status_code}: {message}")


# ─── Cooldown registry ────────────────────────────────────────────────────────
# SAM-27 этап 2: используем classify_error из provider_transform.
# 429 → rate_limit (30s), эскалирующий backoff
# 403 → auth (1h)
# 5xx → server (10s)
# overflow → НЕ cooldown (надо урезать контекст, а не ждать)

_cooldowns: dict[str, float] = {}   # source → monotonic timestamp end-of-cooldown
_COOLDOWN_PATH = cooldown_store.default_path(Path(__file__).resolve().parents[1])
_cooldowns.update(cooldown_store.restore(_COOLDOWN_PATH, time.time(), time.monotonic()))   # 403 переживает рестарт (SAM-258)
_cooldown_hits: dict[str, int] = {} # source → consecutive 429 count, сбрасывается ответом поставщика
_429_BACKOFFS = (2, 5, 10, 20)      # секунды (эскалирует с каждым 429)


def cooling_sources() -> set[str]:
    """Провайдеры сейчас в cooldown (пропускать при выборе)."""
    now = time.monotonic()
    return {s for s, until in _cooldowns.items() if until > now}


def _register_classified(source: str, verdict: dict, status_code: int | None) -> None:
    """SAM-27 этап 2: cooldown по классификации ошибки.
    verdict = classify_error(exc) — {kind, retryable, cooldown_s, switch_provider}.
    """
    kind = verdict.get("kind", "unknown")
    cooldown_s = verdict.get("cooldown_s", 60)

    # rate_limit использует эскалирующий backoff
    if kind == "rate_limit":
        count = _cooldown_hits.get(source, 0) + 1
        _cooldown_hits[source] = count
        cooldown_s = _429_BACKOFFS[min(count - 1, len(_429_BACKOFFS) - 1)]
        print(f"[LLMClient] ERROR {source}: rate_limit hit#{count} → {cooldown_s}s cooldown")
    elif kind == "overflow":
        # overflow не помещаем в cooldown — провайдер OK, нужно урезать input
        print(f"[LLMClient] ERROR {source}: overflow (context too long) — НЕ cooldown, нужно урезать prompt")
        return
    else:
        print(f"[LLMClient] ERROR {source}: {kind} ({status_code}) → {cooldown_s}s cooldown")

    _cooldowns[source] = time.monotonic() + cooldown_s
    cooldown_store.remember(_COOLDOWN_PATH, source, cooldown_s, time.time())


def _register_success(source: str) -> None:
    """Ответ поставщика обрывает серию 429: следующий 429 снова даёт первую, короткую паузу."""
    _cooldown_hits.pop(source, None)


def _register_limit(source: str, status_code: int) -> None:
    """Backward compatibility — старая сигнатура для существующих вызовов.
    Внутри теперь делегирует classify_error по статусу."""
    class _SyntheticErr(Exception):
        pass
    err = _SyntheticErr(f"HTTP {status_code}")
    err.status_code = status_code  # type: ignore[attr-defined]
    try:
        from .provider_transform import classify_error
        verdict = classify_error(err)
    except Exception:
        verdict = {"kind": "unknown", "cooldown_s": 60}
    _register_classified(source, verdict, status_code)


# ─── LLM calls ───────────────────────────────────────────────────────────────

async def make_llm_call(
    llm_config: dict,
    task: str = "default",
    on_delta: Callable[[str], None] | None = None,
    on_reset: Callable[[], None] | None = None,
    stop_when: Callable[[str], object] | None = None,
    **kwargs,
):
    """
    Unified LLM call с авто-фолбэком при 403/429/500/503 и молчании дольше срока (SAM-73).
      429/403 → cooldown registry + немедленный fallback (возврат к priority после cooldown)
      500/503 → skip только для этого вызова
      нет ответа за CALL_TIMEOUT_S, первого чанка за FIRST_TOKEN_TIMEOUT_S, следующего
      за DELTA_TIMEOUT_S → ошибка поставщика TIMEOUT_STATUS, cooldown и замена

    Если задан on_delta — переключаемся в streaming:
      - на каждый text-chunk вызываем on_delta(text)
      - tool_calls аккумулируются по index
      - возвращаем response-like объект совместимый с non-stream вариантом
    on_reset вызывается, когда попытка, уже отдавшая дельты, упала: показанное начало
    ответа не продолжится, следующая попытка стримит ответ с начала.
    stop_when(дельта) - страж потока (runaway.Watch.feed): непустой итог закрывает поток, ответ - собранное до обрыва
    с finish_reason RUNAWAY_FINISH и признаком в response.runaway; запасная модель не берётся (SAM-246).
    """
    from .llm_router import get_best_llm

    hard_skip: set = set()  # 500/503 — skip только для текущего вызова
    cfg = llm_config

    while True:
        attempt = _DeltaTracker(on_delta)
        try:
            # Рассуждение по роли и поставщику (SAM-226): на каждой попытке - поставщик мог смениться
            from .provider_transform import strip_unsupported_sampling
            kw = reasoning.apply(cfg, token_usage.current_scope()[1], {**kwargs, "model": cfg["model"]})
            kw = strip_unsupported_sampling(kw, cfg.get("source", ""), cfg["model"])
            if on_delta is not None:
                kw["stream"] = True
                if STREAM_INCLUDE_USAGE:
                    kw["stream_options"] = {"include_usage": True}
            started = time.monotonic()
            source = cfg.get("source", "?")
            call = _source_calls.get(source) or (
                _litellm_call if _effective_adapter(cfg) == "litellm" else _openai_call)
            wait_s = CALL_TIMEOUT_S if on_delta is None else FIRST_TOKEN_TIMEOUT_S
            # Срок считается под семафором: очередь к своим же вызовам - не молчание поставщика
            async with _call_slots(source):
                llm_monitor.publish(cfg, kw)   # в момент отправки, после очереди к слотам
                result = await _await_provider(call(cfg, **kw), source, wait_s, "response")
            if on_delta is not None:
                first_left = max(FIRST_TOKEN_TIMEOUT_S - (time.monotonic() - started), 0)
                result = await _stream_collect(
                    result, attempt.on_delta, source=source,
                    first_timeout_s=first_left, delta_timeout_s=DELTA_TIMEOUT_S, stop_when=stop_when,
                )
            token_usage.record_call(cfg, result, int((time.monotonic() - started) * MS_PER_SECOND))
            _register_success(source)
            return result

        except LLMProviderError as e:
            if attempt.shown and on_reset is not None:
                _safe_reset(on_reset)
            # SAM-27 этап 2: классифицируем ошибку через provider_transform
            try:
                from .provider_transform import classify_error
                synthetic = Exception(str(e))
                synthetic.status_code = e.status_code  # type: ignore[attr-defined]
                verdict = classify_error(synthetic)
            except Exception:
                verdict = {"kind": "unknown", "switch_provider": True,
                           "retryable": False, "cooldown_s": 60}

            kind = verdict.get("kind", "unknown")
            if verdict.get("cooldown_s", 0) > 0 and kind != "overflow":
                _register_classified(e.source, verdict, e.status_code)
            if not verdict.get("switch_provider", True):
                # overflow — нет смысла переключаться, пробросить
                raise

            if kind in ("server", "rate_limit"):
                pass  # cooldown уже зарегистрирован, ищем замену
            else:
                hard_skip.add(e.source)

            skip_now = hard_skip | cooling_sources()
            print(f"[LLMClient] ERROR {kind} от {e.source} (HTTP {e.status_code}: {str(e)[:120]}) → skip={skip_now}, ищем замену...")
            if nxt := await _next_fallback(cfg, skip_now, same_source=kind in ("server", "unknown")):
                print(f"[LLMClient] следующая по приоритету: {nxt['source']}/{nxt['model']}")
                cfg = nxt
                continue
            cfg = await get_best_llm(task=task, skip_sources=skip_now)
            # Роутер вернул пропущенный источник - повтор дал бы бесконечный цикл
            if not cfg or cfg.get("source") in skip_now:
                raise RuntimeError(f"Все LLM провайдеры недоступны: {skip_now}") from e


async def _next_fallback(cfg: dict, skip: set, same_source: bool = False) -> dict | None:
    """Следующая модель из списка запасных (cfg["fallbacks"]: ranking оркестратора или список источника): пропускает
    остывающие источники, проб нет. same_source: сбой одной модели (5xx, молчание) не закрывает остальные модели
    того же источника, тогда они берутся, хотя источник в паузе (SAM-261)."""
    from .llm_router import source_config
    rest = list(cfg.get("fallbacks") or [])
    while rest:
        row = rest.pop(0)
        if row["source"] in skip and not (same_source and row["source"] == cfg.get("source")):
            continue
        if nxt := await source_config(row["source"], row["model"]):
            return {**nxt, "fallbacks": rest}
    return None


class _DeltaTracker:
    """Дельты одной попытки: были ли они показаны до её сбоя."""

    def __init__(self, on_delta: Callable[[str], None] | None):
        self._on_delta = on_delta
        self.shown = False

    def on_delta(self, piece: str) -> None:
        if not self.shown:
            token_usage.mark_stage("first_token")   # трасса хода (SAM-81); вне хода - ничего
        self.shown = True
        self._on_delta(piece)


def _safe_reset(on_reset: Callable[[], None]) -> None:
    try:
        on_reset()
    except Exception as e:
        print(f"[LLMClient] ERROR on_reset error: {e}")


async def _await_provider(awaitable, source: str, timeout_s: float | None, what: str):
    """Ждать поставщика не дольше timeout_s (None - без своего срока); молчание - ошибка, как 5xx."""
    try:
        return await asyncio.wait_for(awaitable, timeout_s)
    except asyncio.TimeoutError as e:
        raise LLMProviderError(source, TIMEOUT_STATUS, f"no {what} in {timeout_s:g}s") from e


async def _litellm_call(llm_config: dict, **kwargs):
    try:
        import litellm
    except ImportError:   # без тихой подмены своим путём: адаптер выбран явно, библиотека должна стоять
        raise LLMProviderError(llm_config.get("source", "?"), MISSING_LIBRARY_STATUS,
                               "litellm не установлен: pip install litellm или LLM_ADAPTER=openai") from None

    call_kwargs = {k: v for k, v in kwargs.items() if v is not None}
    call_kwargs["model"] = _litellm_model(llm_config)

    if base_url := llm_config.get("model_server"):
        call_kwargs["api_base"] = base_url
    if api_key := llm_config.get("api_key"):
        call_kwargs["api_key"] = api_key
    if extra_body := llm_config.get("extra_body"):  # поля провайдера сверх OpenAI API (z.ai: thinking)
        call_kwargs["extra_body"] = {**extra_body, **call_kwargs.get("extra_body", {})}

    # litellm не принимает пустой список tools
    if "tools" in call_kwargs and not call_kwargs["tools"]:
        call_kwargs.pop("tools")
        call_kwargs.pop("tool_choice", None)

    try:
        return await litellm.acompletion(**call_kwargs)
    except Exception as e:
        code = getattr(e, "status_code", None)
        if code in (403, 429, 500, 503):
            raise LLMProviderError(llm_config.get("source", "?"), code, str(e)[:120]) from e
        raise


# ─── Streaming accumulator ────────────────────────────────────────────────────


class _FakeFunc:
    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    __slots__ = ("id", "type", "function")

    def __init__(self, tc_id: str, name: str, arguments: str):
        self.id = tc_id
        self.type = "function"
        self.function = _FakeFunc(name, arguments)


class _FakeMessage:
    __slots__ = ("content", "tool_calls", "role")

    def __init__(self, content: str, tool_calls: list | None):
        self.content = content
        self.tool_calls = tool_calls or None
        self.role = "assistant"


class _FakeChoice:
    __slots__ = ("message", "finish_reason", "index")

    def __init__(self, message: _FakeMessage, finish_reason: str | None):
        self.message = message
        self.finish_reason = finish_reason
        self.index = 0


class _FakeResponse:
    __slots__ = ("choices", "usage", "runaway")

    def __init__(self, choices: list, usage=None, runaway=None):
        self.choices = choices
        self.usage = usage  # из последнего чанка, если поставщик прислал
        self.runaway = runaway  # признак разноса, по которому поток оборван (SAM-246)


async def _stream_collect(stream, on_delta: Callable[[str], None],
                          source: str = "stream",
                          first_timeout_s: float | None = None,
                          delta_timeout_s: float | None = None,
                          stop_when: Callable[[str], object] | None = None) -> _FakeResponse:
    """Итерирует стрим OpenAI/litellm chunks, аккумулирует в полный ответ.
    source - поставщик стрима: обрыв ставит в cooldown его, а не безымянный «stream».
    first_timeout_s и delta_timeout_s - сроки первого и каждого следующего чанка (SAM-73);
    None - ждать, сколько ждёт библиотека.

    Для каждого text-delta вызывает on_delta(text). Tool calls собираются
    по index в полные function calls. Возвращает объект, совместимый по
    интерфейсу с обычным completion response (.choices[0].message.{content,tool_calls}).
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tcs_acc: dict[int, dict] = {}
    finish_reason: str | None = None
    usage = None
    stopped = None

    chunks = stream.__aiter__()
    wait_s = first_timeout_s
    try:
        while True:
            try:
                chunk = await _await_provider(chunks.__anext__(), source, wait_s, "stream chunk")
            except StopAsyncIteration:
                break
            wait_s = delta_timeout_s
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            ch = choices[0]
            delta = getattr(ch, "delta", None)
            if delta is None:
                continue

            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                try:
                    on_delta(text)
                except Exception as e:
                    print(f"[Stream] on_delta error: {e}")
                if stop_when is not None and (stopped := stop_when(text)):
                    finish_reason = RUNAWAY_FINISH
                    print(f"[Stream] ERROR разнос генерации {source}: {getattr(stopped, 'detail', stopped)}, поток оборван")
                    await _close_stream(stream)
                    break

            # GLM-4.x / DeepSeek-R1 / Qwen3-thinking: LM Studio отделяет
            # thinking-токены в delta.reasoning_content. Аккумулируем отдельно
            # и в финале оборачиваем в <think>...</think> — strip_thinking()
            # в text_utils.py уберёт это перед озвучкой как обычный thinking.
            reasoning_text = getattr(delta, "reasoning_content", None)
            if reasoning_text:
                reasoning_parts.append(reasoning_text)

            tcs_delta = getattr(delta, "tool_calls", None) or []
            for tc in tcs_delta:
                idx = getattr(tc, "index", 0) or 0
                entry = tcs_acc.setdefault(
                    idx, {"id": None, "name": None, "arguments": ""},
                )
                if getattr(tc, "id", None):
                    entry["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        entry["name"] = fn.name
                    arg_piece = getattr(fn, "arguments", None)
                    if arg_piece:
                        entry["arguments"] += arg_piece

            fr = getattr(ch, "finish_reason", None)
            if fr:
                finish_reason = fr
    except Exception as exc:
        # Ошибки во время итерации стрима (APIError от LM Studio/OpenAI и т.п.)
        # не ловятся try в _openai_call/_litellm_call — там завершается только
        # инициация запроса. Конвертируем в LLMProviderError чтобы make_llm_call
        # мог переключить провайдера / уйти в overflow path.
        code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if not isinstance(code, int):
            code = 500
        if code in (403, 429, 500, 503):
            raise LLMProviderError(source, code, str(exc)[:200]) from exc
        raise

    tcs: list = []
    for idx in sorted(tcs_acc.keys()):
        e = tcs_acc[idx]
        if not e["name"]:
            continue
        tc_id = e["id"] or f"call_{uuid.uuid4().hex[:8]}"
        tcs.append(_FakeToolCall(tc_id, e["name"], e["arguments"] or "{}"))

    full_content = "".join(content_parts)
    if reasoning_parts:
        # Заворачиваем thinking в <think>...</think> — text_utils.strip_thinking
        # вырежет его перед озвучкой, парсер tool-вызовов проигнорирует.
        thinking = "".join(reasoning_parts)
        full_content = f"<think>{thinking}</think>\n{full_content}"

    msg = _FakeMessage(full_content, tcs if tcs else None)
    return _FakeResponse([_FakeChoice(msg, finish_reason)], usage, stopped)


async def _close_stream(stream) -> None:
    """Закрыть поток поставщика, чтобы генерация на его стороне прекратилась; сбой закрытия обрыв не отменяет."""
    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        print(f"[Stream] закрытие потока не удалось: {e}")


def _strip_images_from_messages(messages: list) -> list:
    """Убрать image_url parts из content list — для retry на text-only моделях."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text_parts = [
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            images_count = sum(
                1 for p in c
                if isinstance(p, dict) and p.get("type") == "image_url"
            )
            text = " ".join(t for t in text_parts if t).strip()
            if images_count:
                text = (text or "image") + f" [{images_count} image(s) skipped — text-only model]"
            out.append({**m, "content": text})
        else:
            out.append(m)
    return out


# Клиенты AsyncOpenAI по циклу событий и (адрес, ключ). Новый клиент на каждый вызов стоил 0,36 с до запроса
# (SSL-контекст с сертификатами, замер 9 окт на Windows): при первом токене локальной модели 0,1-0,2 с это
# главная задержка хода. Клиент привязан к циклу его пула соединений, поэтому ключ верхнего уровня - цикл.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple, AsyncOpenAI]]" =     weakref.WeakKeyDictionary()


def _openai_client(base_url: str | None, api_key: str) -> AsyncOpenAI:
    per_loop = _clients.setdefault(asyncio.get_running_loop(), {})
    key = (base_url, api_key)
    if key not in per_loop:
        per_loop[key] = AsyncOpenAI(base_url=base_url, api_key=api_key)
    return per_loop[key]


# Ключ агентов у поставщика (28 окт, Polza): вызовы роли worker идут им - свой суточный лимит и неофициальные
# провайдеры; остальные роли - основным ключом источника. Нет такого ключа - основной
ROLE_KEY_ENVS = {(token_usage.ROLE_WORKER, "polza"): "POLZA_AGENTS_API_KEY"}


def api_key_for(llm_config: dict, role: str, environ=None) -> str:
    """Ключ вызова: ключ роли у поставщика, если задан, иначе ключ конфига модели."""
    env = os.environ if environ is None else environ
    name = ROLE_KEY_ENVS.get((role, llm_config.get("source", "")))
    return (env.get(name, "").strip() if name else "") or llm_config.get("api_key", "lm-studio")


async def _openai_call(llm_config: dict, **kwargs):
    client = _openai_client(llm_config.get("model_server"), api_key_for(llm_config, token_usage.current_scope()[1]))
    call_kwargs = {k: v for k, v in kwargs.items() if v is not None}
    # Нативные tools всем, как у Hermes, OpenCode и openclaw (замер 12 окт: без них qwen звал инструменты только
    # разбором текста); harmony (gpt-oss) - разбор из текста по своему формату
    if llm_config.get("prompt_format") in NO_NATIVE_TOOLS_FORMATS:
        call_kwargs.pop("tools", None)
        call_kwargs.pop("tool_choice", None)

    try:
        return await client.chat.completions.create(**call_kwargs)
    except Exception as e:
        code = getattr(e, "status_code", None)
        # 400 + Invalid 'content' → модель не vision, retry без картинок
        if code == 400 and "Invalid 'content'" in str(e):
            msgs = call_kwargs.get("messages", [])
            if any(isinstance(m.get("content"), list) for m in msgs):
                print(f"[LLMClient] ERROR 400 Invalid content → strip images and retry")
                call_kwargs["messages"] = _strip_images_from_messages(msgs)
                return await client.chat.completions.create(**call_kwargs)
        if code in (403, 429, 500, 503):
            raise LLMProviderError(llm_config.get("source", "?"), code, str(e)[:120]) from e
        raise
