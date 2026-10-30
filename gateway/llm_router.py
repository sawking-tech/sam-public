"""
LLM Router — выбор лучшего провайдера по типу задачи.

Источники (по убыванию качества):
  claude_code  — claude CLI (Pro подписка, без API ключа)
  anthropic    — Anthropic API (claude-haiku, требует ключ)
  gemini       — Gemini Flash API
  lmstudio     — локальная модель в LM Studio
  dashscope    — Qwen DashScope
  zai          — GLM в облаке z.ai (ZAI_API_KEY; по умолчанию эндпоинт Coding Plan)
  openrouter   — агрегатор OpenRouter (OPENROUTER_API_KEY, OPENROUTER_MODEL)
  polza        — агрегатор polza.ai (POLZA_API_KEY, POLZA_MODEL)

Задачи:
  "default"  — главный агент / ответы пользователю
  "planning" — TaskPlanner: структурированный JSON, нужна высокая точность
  "guard"    — проверка безопасности: быстро, локально
  "verify"   — DoD верификация: быстро, дёшево
  "turn"     — главный ход (текст и голос): при SAM_AGENT_BACKEND=acp его ведёт ACP-агент (ADR-022), иначе как default

Приоритеты:
  planning:  Anthropic API -> Gemini Flash -> LM Studio -> Claude Code CLI -> Qwen
  default:   LM Studio -> Gemini Flash -> Qwen

Claude Code CLI — последний приоритет: тратит лимиты Pro подписки.
"""

import os
import re
import json
import asyncio
import functools
import ssl
import subprocess
import time

import certifi
import httpx
from typing import Any
from .env import env_bool, env_float

MIN_PARAMS_BILLION = 4

# SAM-57: модель LM Studio выбирается по id (LMSTUDIO_MODEL), а не первой из /v1/models:
# порядок там зависит от загрузки, и поведение SAM было случайным. Без env берётся
# единственная модель; несколько - ошибка без подмены.
LMSTUDIO_MODEL_ENV = "LMSTUDIO_MODEL"
MODELS_IN_ERROR = 5   # сколько id показать в строке ошибки
# Форматы без нативных tools (llm_client.NO_NATIVE_TOOLS_FORMATS): для них разбор вызовов из текста включён всегда
TEXT_ONLY_FORMATS = ("harmony",)
_lmstudio_error: str = ""

# z.ai: OpenAI-совместимый API GLM. Coding Plan - подписка с недельным лимитом, общий API - по балансу
# (проверено 9 окт 2026: оба отдают /models по одному ключу). Рассуждение - gateway/reasoning.py (SAM-226).
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-5.3-flash")

# Агрегаторы с OpenAI-совместимым входом: источник -> (адрес, переменная ключа, переменная модели). Модель обязательна
# в окружении: каталоги агрегаторов меняются быстрее кода (27 окт Google снял gemini-2.0-flash), имя в коде устарело бы
AGGREGATORS = {
    "openrouter": ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
    "polza": ("POLZA_BASE_URL", "https://polza.ai/api/v1", "POLZA_API_KEY", "POLZA_MODEL"),
}

# Gemini: OpenAI-совместимый вход. 27 окт Google снял gemini-2.0-flash (404), имя читается из окружения, чтобы
# следующий вывод модели из эксплуатации не требовал правки кода
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Быстрая модель голоса: локальная без рассуждения. Замер 9 окт на голосовом промпте, RTX 3090 Ti:
# Qwen3-30B-A3B-Instruct-2507 - первый токен 0,06-0,2 с, Gemma 4 E4B и 26B-A4B рассуждают, 2-5 с.
# Модель не загружена - голос идёт основной: загрузка на ходу заняла бы 30 с. off - без быстрой модели.
FAST_SOURCE = "lmstudio"
FAST_LMSTUDIO_MODEL = "qwen/qwen3-30b-a3b-2507"
FAST_OFF = "off"
# Пометка конфига маршрута fast: по ней монитор отличает вызов быстрой модели (llm_monitor)
ROUTE_KEY = "route"
ROUTE_FAST = "fast"
LMSTUDIO_LOADED = "loaded"   # state модели в REST API LM Studio /api/v0/models/<id>


# ─── Claude Code adapter ─────────────────────────────────────────────────────

# Модель служебных вызовов поставщика claude_code (Sonnet; Opus - агентам кода, SAM_CLAUDE_CODING_MODEL).
# Сессия-ведущий берёт модель из "model" в .claude/settings.json проекта
LEAD_MODEL_ENV = "SAM_CLAUDE_LEAD_MODEL"
DEFAULT_LEAD_MODEL = "sonnet"

class _ClaudeCodeResponse:
    """Имитирует структуру ответа OpenAI для совместимости."""
    def __init__(self, text: str):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


async def claude_code_call(messages: list[dict], max_tokens: int = 2000) -> Any:
    """
    Вызывает Claude через CLI (Pro подписка, без API ключа).
    Принимает messages в формате OpenAI, возвращает объект совместимый с resp.choices[0].message.content
    """
    # Собираем промпт из messages
    parts = []
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    for m in messages:
        if m["role"] == "user":
            parts.append(m["content"])
        elif m["role"] == "assistant":
            parts.append(f"[assistant]: {m['content']}")

    prompt = f"{system}\n\n{chr(10).join(parts)}" if system else "\n".join(parts)

    from .tools.coding_agents import claude_model_args
    try:
        proc = await asyncio.create_subprocess_exec(
            _claude_cli(), "--dangerously-skip-permissions", "-p", prompt,
            "--output-format", "json", *claude_model_args(LEAD_MODEL_ENV, DEFAULT_LEAD_MODEL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[:200])
        data = json.loads(stdout.decode("utf-8"))
        text = data.get("result", data.get("content", ""))
        return _ClaudeCodeResponse(text)
    except asyncio.TimeoutError:
        raise RuntimeError("Claude CLI timeout (120s)")


def _claude_cli() -> str:
    """Путь к claude CLI: SAM_CLAUDE_CLI или поиск в PATH (на ПК - claude.exe; прежний claude.cmd там нет)."""
    from .tools.coding_agents import resolve_cli
    return resolve_cli("SAM_CLAUDE_CLI", "claude") or "claude"


def _claude_code_available() -> bool:
    """Проверяет что claude CLI доступен."""
    try:
        result = subprocess.run([_claude_cli(), "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


async def get_best_llm(task: str = "default", skip_sources: set = None) -> dict | None:
    """Определяет лучший LLM для данного типа задачи. skip_sources — пропустить провайдеры.

    Принудительный выбор: LLM_SOURCE=lmstudio|gemini|dashscope|anthropic|claude_code|zai
    """
    forced = os.getenv("LLM_SOURCE", "").strip().lower()
    skip = skip_sources or set()

    # Быстрая модель голоса выбирается раньше принудительной основной: LLM_SOURCE задаёт основную
    if task == "fast" and (cfg := await _fast_llm(skip)):
        return _main_route(cfg) if await _same_as_main(cfg, forced, skip) else cfg
    if task in TURN_TASKS and (cfg := _agent_turn_llm(skip)):
        return cfg
    if forced and forced not in skip:
        cfg = await _source_config(forced)
        if cfg:
            return cfg
        print(f"[LLM Router] LLM_SOURCE={forced!r} недоступен, авто-выбор")

    if task == "planning":
        return await _get_planning_llm(skip)
    return await _get_default_llm(skip)


async def _same_as_main(fast: dict, forced: str, skip: set) -> bool:
    """Быстрая и основная - одна модель (SAM-170): тогда отвечает основная, проверять её ответ некому и незачем.
    Основная - как у хода: агент за ACP, иначе LLM_SOURCE, иначе авто; источник другой - без опроса поставщика."""
    if (acp := _agent_turn_llm(skip)) is not None:
        return (acp.get("source"), acp.get("model")) == (fast.get("source"), fast.get("model"))
    if forced and forced != fast.get("source"):
        return False
    main = await _source_config(forced) if forced and forced not in skip else await _get_default_llm(skip)
    return bool(main) and (main.get("source"), main.get("model")) == (fast.get("source"), fast.get("model"))


def _main_route(cfg: dict) -> dict:
    """Конфиг без метки маршрута fast: ответ считается ответом основной (voice_turn.answer_source)."""
    return {k: v for k, v in cfg.items() if k != ROUTE_KEY}


# Задачи главного хода: голос просит "fast" (без быстрой модели - основную), текстовый ход - "turn"
TURN_TASKS = ("turn", "fast")


def _agent_turn_llm(skip: set) -> dict | None:
    """Главный ход ведёт ACP-агент (ADR-022), если так выставлен SAM_AGENT_BACKEND и агент настроен."""
    from .acp import config as acp_config
    if acp_config.backend() != acp_config.BACKEND_ACP or acp_config.SOURCE in skip:
        return None
    cfg = acp_config.from_env()
    return acp_config.llm_config(cfg) if cfg else None


async def _fast_llm(skip: set) -> dict | None:
    """Быстрая модель голоса (SAM-56): LLM_FAST_SOURCE и LLM_FAST_MODEL; недоступна - основная."""
    source = (os.getenv("LLM_FAST_SOURCE") or FAST_SOURCE).strip().lower()
    if source == FAST_OFF or source in skip:
        return None
    model = (os.getenv("LLM_FAST_MODEL") or (FAST_LMSTUDIO_MODEL if source == "lmstudio" else "")).strip()
    if source == "lmstudio":
        cfg = await _check_lmstudio(model, loaded_only=True) if model else None
        return {**cfg, ROUTE_KEY: ROUTE_FAST} if cfg else None
    cfg = await _source_config(source)
    if cfg and model:
        cfg = {**cfg, "model": model}
    return {**cfg, ROUTE_KEY: ROUTE_FAST} if cfg else None


# Служебные вызовы истории (SAM-226): сводка куска и слияние сводок - на локальной модели без рассуждения. Она уже
# загружена для быстрого голоса, бесплатна и отвечает быстрее облака; вызовы идут в паузах разговора.
# off - на основной модели. Источник недоступен или модель не загружена - основная модель роутера
SERVICE_SOURCE_ENV = "SAM_SERVICE_LLM_SOURCE"
SERVICE_MODEL_ENV = "SAM_SERVICE_LLM_MODEL"
SERVICE_OFF = "off"


async def get_service_llm() -> dict | None:
    """Модель служебных вызовов истории; недоступна - основная (get_best_llm)."""
    source = (os.getenv(SERVICE_SOURCE_ENV) or FAST_SOURCE).strip().lower()
    model = (os.getenv(SERVICE_MODEL_ENV) or (FAST_LMSTUDIO_MODEL if source == "lmstudio" else "")).strip()
    cfg = None
    if source == "lmstudio":
        cfg = await _check_lmstudio(model, loaded_only=True) if model else None
    elif source != SERVICE_OFF:
        cfg = await _source_config(source)
        if cfg and model:
            cfg = {**cfg, "model": model}
    if cfg is None and source != SERVICE_OFF:
        print(f"[LLM Router] служебная модель {source}/{model or '-'} недоступна - основная")
    return cfg or await get_best_llm(task="default")


# Источники, которые знает _source_config: список для проверки ввода (switch_main_model)
SOURCES = ("lmstudio", "gemini", "dashscope", "anthropic", "zai", *AGGREGATORS, "claude_code", "acp")


async def source_config(source: str, model: str = "") -> dict | None:
    """Конфиг конкретного источника или None (модель агента по метке сложности, SAM-91).
    model - id модели LM Studio вместо LMSTUDIO_MODEL; окружение при этом не меняется."""
    if source == "lmstudio" and model:
        return await _check_lmstudio(model)
    if source in AGGREGATORS and model:
        return _aggregator_config(source, model)
    return await _source_config(source)


async def _source_config(source: str) -> dict | None:
    """Возвращает конфиг для конкретного провайдера (или None если недоступен)."""
    if source == "lmstudio":
        return await _check_lmstudio()
    if source == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if key:
            return {"model": GEMINI_MODEL, "model_server": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key": key, "source": "gemini"}
    if source == "dashscope":
        key = os.getenv("DASHSCOPE_API_KEY")
        if key:
            return _qwen_config()
    if source == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        if key:
            return {"model": "claude-haiku-4-5-20251001", "model_server": "https://api.anthropic.com/v1", "api_key": key, "source": "anthropic"}
    if source == "zai":
        return _zai_config()
    if source in AGGREGATORS:
        return _aggregator_config(source)
    if source == "claude_code" and _claude_code_available():
        return {"model": "claude-pro", "model_server": None, "api_key": None, "source": "claude_code"}
    if source == "acp":
        from .acp import config as acp_config
        cfg = acp_config.from_env()
        return acp_config.llm_config(cfg) if cfg else None
    return None


async def _get_planning_llm(skip: set = frozenset()) -> dict | None:
    """Для планирования нужна точность — Anthropic/Gemini/LM Studio первый приоритет."""

    # 1. Claude Haiku (Anthropic API)
    if "anthropic" not in skip:
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            print("[LLM Router] Planning -> Claude Haiku (Anthropic API)")
            return {
                "model": "claude-haiku-4-5-20251001",
                "model_server": "https://api.anthropic.com/v1",
                "api_key": anthropic_key,
                "source": "anthropic",
                "extra_headers": {"anthropic-version": "2023-06-01"},
            }

    # 2. Gemini Flash — хорошо справляется с JSON
    if "gemini" not in skip:
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            print("[LLM Router] Planning -> Gemini Flash")
            return {
                "model": GEMINI_MODEL,
                "model_server": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key": gemini_key,
                "source": "gemini",
            }

    # 3. LM Studio (локальная модель)
    if "lmstudio" not in skip:
        lm = await _check_lmstudio()
        if lm:
            return lm

    # 4. Claude Code CLI (Pro подписка) — последний: тратит лимиты основной подписки
    if "claude_code" not in skip and _claude_code_available():
        print("[LLM Router] Planning -> Claude Code CLI (Pro, fallback)")
        return {
            "model": "claude-pro",
            "model_server": None,
            "api_key": None,
            "source": "claude_code",
        }

    if "dashscope" not in skip:
        return _qwen_config()

    return None


async def _get_default_llm(skip: set = frozenset()) -> dict | None:
    """Для обычных задач — локальная модель первый приоритет.
    skip - поставщики, упавшие в этом вызове или в cooldown: без него
    make_llm_call получал тот же источник снова и крутился по кругу (SAM-72)."""

    if "lmstudio" not in skip:
        lm = await _check_lmstudio()
        if lm:
            return lm

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key and "gemini" not in skip:
        return {
            "model": GEMINI_MODEL,
            "model_server": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key": gemini_key,
            "source": "gemini",
        }

    if "dashscope" not in skip:
        return _qwen_config()
    return None


def _qwen_config() -> dict:
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    return {
        "model": "qwen-plus",
        "model_server": base_url,
        "api_key": os.getenv("DASHSCOPE_API_KEY"),
        "source": "dashscope",
    }


def pick_lmstudio_model(model_ids: list[str], wanted: str) -> tuple[str | None, str]:
    """(id, ошибка). wanted задан - только он; не задан - единственная модель."""
    shown = ", ".join(model_ids[:MODELS_IN_ERROR])
    if wanted:
        if wanted in model_ids:
            return wanted, ""
        return None, f"{LMSTUDIO_MODEL_ENV}={wanted!r} нет в LM Studio (есть: {shown})"
    if len(model_ids) == 1:
        return model_ids[0], ""
    if not model_ids:
        return None, ""
    return None, (f"LM Studio отдаёт {len(model_ids)} моделей, выбери одну в "
                  f"{LMSTUDIO_MODEL_ENV} (есть: {shown})")


def lmstudio_error() -> str:
    """Почему LM Studio пропущена при последней проверке; пусто - всё в порядке."""
    return _lmstudio_error


def _set_lmstudio_error(err: str) -> None:
    global _lmstudio_error
    if err and err != _lmstudio_error:
        print(f"[LLM Router] {err}")  # одна строка на смену причины, не на каждый ход
    _lmstudio_error = err


def text_tools_enabled(prompt_format: str) -> bool:
    """Разбирать вызовы tools из текста ответа (LLM_TEXT_TOOLS, по умолчанию да)."""
    if prompt_format in TEXT_ONLY_FORMATS:
        return True
    return env_bool("LLM_TEXT_TOOLS", True)


# Проверка LM Studio держится в кэше несколько секунд (ADR-027): голосовой ход спрашивал /v1/models два-три раза,
# 0,45 с на ход; загрузка и выгрузка модели заметны через столько же секунд
LMSTUDIO_CACHE_S = env_float("SAM_LMSTUDIO_CACHE_S", 5.0)
_lmstudio_cache: dict[tuple, tuple[float, dict | None]] = {}


async def _check_lmstudio(wanted: str | None = None, loaded_only: bool = False) -> dict | None:
    """Конфиг LM Studio из кэша на LMSTUDIO_CACHE_S; устарел - новая проверка (_probe_lmstudio)."""
    key = (os.getenv("LMSTUDIO_URL", ""), os.getenv(LMSTUDIO_MODEL_ENV, ""), wanted, loaded_only)
    hit = _lmstudio_cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < LMSTUDIO_CACHE_S:
        return dict(hit[1]) if hit[1] else None
    cfg = await _probe_lmstudio(wanted, loaded_only)
    _lmstudio_cache[key] = (now, cfg)
    return dict(cfg) if cfg else None


async def _probe_lmstudio(wanted: str | None = None, loaded_only: bool = False) -> dict | None:
    """Конфиг LM Studio. wanted - id модели вместо LMSTUDIO_MODEL; loaded_only - только уже загруженная:
    /v1/models отдаёт и скачанные, а запрос к незагруженной грузит её на ходу."""
    lmstudio_url = os.getenv("LMSTUDIO_URL", "http://127.0.0.1:1234")
    try:
        async with httpx.AsyncClient(timeout=2.0, verify=_ssl_context()) as client:
            resp = await client.get(f"{lmstudio_url}/v1/models")
            if resp.status_code != 200:
                return None
            ids = [m["id"] for m in resp.json().get("data", [])]
            if wanted is not None:
                model_id = wanted if wanted in ids else None
            else:
                model_id, err = pick_lmstudio_model(ids, os.getenv(LMSTUDIO_MODEL_ENV, "").strip())
                _set_lmstudio_error(err)
            if not model_id:
                return None
            if loaded_only and not await _lmstudio_loaded(client, lmstudio_url, model_id):
                return None
            params = _extract_params(model_id)
            if 0 < params < MIN_PARAMS_BILLION:
                print(f"[LLM Router] LM Studio: {model_id} ({params}B) — слишком маленькая, пропуск")
                return None
            size_str = f"{params}B" if params else "неизв. размер"
            print(f"[LLM Router] LM Studio: {model_id} ({size_str}) — используем")
            # Определяем формат промпта по имени модели
            mid_lower = model_id.lower()
            if "gpt-oss" in mid_lower or "harmony" in mid_lower:
                prompt_format = "harmony"
            elif "glm" in mid_lower or "chatglm" in mid_lower:
                prompt_format = "glm"
            elif "gemma" in mid_lower:
                prompt_format = "gemma"
            elif "qwen" in mid_lower:
                prompt_format = "qwen"
            else:
                prompt_format = "standard"

            return {
                "model": model_id,
                "model_server": f"{lmstudio_url}/v1",
                "api_key": "lm-studio",
                "source": "lmstudio",
                "prompt_format": prompt_format,
                "text_tools": text_tools_enabled(prompt_format),
                # enable_thinking намеренно не отключаем:
                # Qwen3 использует thinking для правильного формирования tool_calls.
                # Thinking стрипается из финального ответа в _strip_thinking().
            }
    except (httpx.ConnectError, httpx.TimeoutException):
        return None


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Один SSL-контекст на процесс: httpx собирает его заново на каждый клиент, 0,36 с на проверку LM Studio
    перед каждым голосовым ходом (замер 9 окт). Сертификаты те же, что у httpx по умолчанию (certifi)."""
    return ssl.create_default_context(cafile=certifi.where())


async def _lmstudio_loaded(client: httpx.AsyncClient, lmstudio_url: str, model_id: str) -> bool:
    resp = await client.get(f"{lmstudio_url}/api/v0/models/{model_id}")
    return resp.status_code == 200 and resp.json().get("state") == LMSTUDIO_LOADED


def aggregator_models(source: str) -> list[str]:
    """Модели, между которыми можно переключаться на лету: <ИМЯ_МОДЕЛИ>S из окружения через запятую (POLZA_MODELS)."""
    model_env = AGGREGATORS[source][3]
    return [m.strip() for m in os.getenv(f"{model_env}S", "").split(",") if m.strip()]


def model_options() -> dict[str, dict]:
    """Для монитора и переключения: у агрегатора с ключом - активная модель и список для выбора."""
    return {source: {"active": os.getenv(spec[3], "").strip(), "models": aggregator_models(source)}
            for source, spec in AGGREGATORS.items() if os.getenv(spec[2])}


def unavailable_reason(source: str, model: str = "") -> str:
    """Одна строка: почему источник не дал конфиг, для отказа смены модели (SAM-238): модель просили угадывать причину."""
    if source in AGGREGATORS:
        _, _, key_env, model_env = AGGREGATORS[source]
        if not os.getenv(key_env):
            return f"нет ключа {key_env} в окружении"
        listed, active = aggregator_models(source), os.getenv(model_env, "").strip()
        if model and listed and model not in {*listed, active}:
            return f"модели {model} нет в {model_env}S; допустимые: {', '.join(listed)}"
        if not (model or active):
            return f"не задана модель, задай {model_env}"
    return "нет ключа или сервис не отвечает"


def _aggregator_config(source: str, model: str = "") -> dict | None:
    """Агрегатор OpenAI-совместимого входа; None, если нет ключа или модели (в лог - что задать). model - выбор на
    лету: допустима модель из списка <ИМЯ_МОДЕЛИ>S или активная; список пуст - любая (проверка опечаток только по списку)."""
    url_env, default_url, key_env, model_env = AGGREGATORS[source]
    key = os.getenv(key_env)
    if not key:
        return None
    active = os.getenv(model_env, "").strip()
    chosen = model.strip() or active
    if not chosen:
        print(f"[LLM Router] {source}: задай {model_env} (id модели из каталога агрегатора)")
        return None
    listed = aggregator_models(source)
    if model.strip() and listed and chosen not in {*listed, active}:
        print(f"[LLM Router] {source}: модели {chosen} нет в {model_env}S; есть: {', '.join(listed)}")
        return None
    cfg = {"model": chosen, "model_server": os.getenv(url_env, default_url), "api_key": key, "source": source}
    others = [m for m in listed if m != chosen]
    if others:   # запасные того же источника по порядку списка: сбой одной модели не гасит источник (SAM-261)
        cfg["fallbacks"] = [{"source": source, "model": m} for m in others]
    return cfg


def _zai_config() -> dict | None:
    """GLM в облаке z.ai; None, если ключа нет."""
    key = os.getenv("ZAI_API_KEY")
    if not key:
        return None
    return {"model": ZAI_MODEL, "model_server": ZAI_BASE_URL, "api_key": key, "source": "zai"}


def get_available_sources() -> list[str]:
    """Список доступных провайдеров (для отображения в /health)."""
    sources = []
    if os.getenv("ANTHROPIC_API_KEY"):
        sources.append("anthropic")
    if os.getenv("GEMINI_API_KEY"):
        sources.append("gemini")
    if os.getenv("DASHSCOPE_API_KEY"):
        sources.append("dashscope")
    if os.getenv("ZAI_API_KEY"):
        sources.append("zai")
    sources.extend(s for s, (_, _, key_env, _) in AGGREGATORS.items() if os.getenv(key_env))
    sources.append("lmstudio")  # всегда в списке, может быть недоступен
    return sources


def _extract_params(model_id: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*b", model_id.lower())
    return float(match.group(1)) if match else 0
