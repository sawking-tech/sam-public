"""switch_main_model - сменить источник основной модели (LLM_SOURCE) без рестарта шлюза (SAM-129).

Роутер читает LLM_SOURCE на каждый вызов (`llm_router.get_best_llm`), поэтому смена - это запись переменной в
окружение процесса и в .env. Источник проверяется до записи, окружение не трогается: недоступный не ставится.
Переключения идут по одному (замок): параллельные иначе откатили бы друг друга. Главный ход за ACP-агентом
(SAM_AGENT_BACKEND=acp) LLM_SOURCE не читает - переключение отказывается, а не врёт «OK».
Доступность решает та же логика, что у автоматической замены при сбое: конфиг роутера (resolve) и пауза поставщика
после ошибки (cooling, реестр llm_client) - обе приходят параметрами из корня композиции; своей проверки здесь нет.
Логика проверяется без сети и без LM Studio.
"""
from __future__ import annotations

import asyncio
import os
import weakref
from pathlib import Path
from typing import Awaitable, Callable

from .set_env import set_env_impl

SOURCE_ENV = "LLM_SOURCE"
LMSTUDIO_SOURCE = "lmstudio"
LMSTUDIO_MODEL_ENV = "LMSTUDIO_MODEL"
AUTO = "auto"   # пустой LLM_SOURCE - авто-выбор роутера

Resolve = Callable[[str, str], Awaitable[dict | None]]   # (источник, модель или "") -> конфиг или None
# Замок на цикл событий: вызов инструмента идёт и через asyncio.run в своём цикле (BaseTool.call), а замок
# asyncio привязан к одному циклу
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    return _locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())


def _persist(updates: dict[str, str], env_path: Path | str | None) -> str:
    """Записать пары в .env и окружение; первая ошибка - строка, иначе пусто."""
    for key, value in updates.items():
        result = set_env_impl({"key": key, "value": value}, env_path)
        if not result.startswith("OK"):
            return result
    return ""


def _restore(saved: dict[str, str | None], env_path: Path | str | None) -> None:
    """Вернуть прежние значения в .env и окружение после частичной записи."""
    for key, value in saved.items():
        set_env_impl({"key": key, "value": value or ""}, env_path)
        if value is None:
            os.environ.pop(key, None)


MAIN_ORDER_ENV = "SAM_MAIN_MODELS"   # порядок основных моделей (gateway/main_model.py)


def switch_main_impl(name: str, main, env_path: Path | str | None = None) -> str:
    """Основная модель - ведущий (claude-code) или модель шлюза (llm), SAM-176: текущей и первой в порядке, порядок -
    в .env, чтобы пережил рестарт. main - gateway/main_model.MainModel (prefer, order)."""
    if error := main.prefer(name.strip().lower()):
        return error
    if error := _persist({MAIN_ORDER_ENV: ",".join(main.order)}, env_path):
        return error
    return f"OK: основная модель {name}, порядок {','.join(main.order)}; рестарт не нужен"


async def switch_main_model_impl(params: dict, resolve: Resolve, sources: tuple[str, ...],
                                 env_path: Path | str | None = None, agent_backend_acp: bool = False,
                                 cooling: Callable[[], set[str]] = set, main=None,
                                 model_envs: dict[str, str] | None = None,
                                 explain: Callable[[str, str], str] | None = None) -> str:
    """params: main (claude-code | llm) - кто основная; source (из sources или auto) - поставщик модели шлюза,
    model - id модели для источника из model_envs (источник -> переменная окружения с его моделью; по умолчанию
    только lmstudio); без model остаётся прежняя."""
    model_envs = model_envs or {LMSTUDIO_SOURCE: LMSTUDIO_MODEL_ENV}
    if params.get("main"):
        if main is None:
            return "Переключение основной модели здесь недоступно"
        if params.get("source"):   # сначала поставщик: его отказ не должен оставить уже сменённую основную
            result = await switch_main_model_impl({k: v for k, v in params.items() if k != "main"}, resolve, sources,
                                                  env_path, agent_backend_acp, cooling, model_envs=model_envs, explain=explain)
            if not result.startswith("OK"):
                return result
        return switch_main_impl(str(params["main"]), main, env_path)
    source = str(params.get("source") or "").strip().lower()
    model = str(params.get("model") or "").strip()
    if source not in sources and source != AUTO:
        return f"Источник {source!r} неизвестен; есть: {', '.join(sources)}, {AUTO}"
    if model and source not in model_envs:
        return f"Модель задаётся только для {', '.join(model_envs)}; у {source} она в конфиге источника"
    if source in cooling():   # ход и так обойдёт его до конца паузы: «OK» было бы неправдой
        return f"Источник {source} в паузе после ошибки (лимит, доступ, сбой сети): ход его сейчас обходит, смена не сделана"
    if agent_backend_acp:
        return "Главный ход ведёт ACP-агент (SAM_AGENT_BACKEND=acp): LLM_SOURCE на него не действует, смена не сделана"

    async with _lock():
        cfg = await resolve(source, model) if source != AUTO else None
        if source != AUTO and not cfg:
            why = explain(source, model) if explain else "нет ключа или сервис не отвечает"
            return f"Источник {source} сейчас недоступен ({why}): остался прежний"
        updates = {SOURCE_ENV: "" if source == AUTO else source}
        if model:
            updates[model_envs[source]] = model
        saved = {key: os.environ.get(key) for key in updates}
        if error := _persist(updates, env_path):
            _restore(saved, env_path)
            return error
    if source == AUTO:
        return f"OK: {SOURCE_ENV} сброшен, роутер выбирает сам; рестарт не нужен"
    return f"OK: основная модель {cfg['source']} / {cfg['model']}; рестарт не нужен"
