"""switch_main_model (SAM-129, SAM-176): смена основной модели и поставщика без рестарта; недоступное не ставится."""
import asyncio
import os

import pytest

from gateway import llm_client, llm_router, privilege_key
from gateway import main_model as mmod
from gateway.tools import main_model as mm
from tests.fakes_core import app_client  # noqa: F401 - фикстура

SOURCES = ("lmstudio", "zai")
LMS = {"lmstudio": {"source": "lmstudio", "model": "qwen3-30b"}, "zai": {"source": "zai", "model": "glm"}}


def _resolver(available: dict[str, dict]):
    async def resolve(source: str, model: str):
        return available.get(source)
    return resolve


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("LLM_SOURCE=zai\nLMSTUDIO_MODEL=old\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("LLM_SOURCE", "zai")
    monkeypatch.setenv("LMSTUDIO_MODEL", "old")
    monkeypatch.setenv("SAM_MAIN_MODELS", "claude-code,llm")   # запись в monkeypatch: тест не оставит свой порядок
    return path


def _main_models():
    models = {"claude-code": mmod.claude_code(lambda: True, lambda m: None), "llm": mmod.llm()}
    return mmod.MainModel(models, ["claude-code", "llm"])


def _switch(params, env_file, available=LMS, **kw):
    return asyncio.run(mm.switch_main_model_impl(params, _resolver(available), SOURCES, env_file, **kw))


@pytest.mark.parametrize("params, env_after", [
    pytest.param({"source": "lmstudio", "model": "qwen3-30b"}, {"LLM_SOURCE": "lmstudio", "LMSTUDIO_MODEL": "qwen3-30b"},
                 id="lmstudio-with-model"),
    pytest.param({"source": "lmstudio"}, {"LLM_SOURCE": "lmstudio", "LMSTUDIO_MODEL": "old"},
                 id="lmstudio-keeps-previous-id"),
    pytest.param({"source": "auto"}, {"LLM_SOURCE": ""}, id="auto-clears-source"),
    pytest.param({"main": "llm"}, {"SAM_MAIN_MODELS": "llm,claude-code", "LLM_SOURCE": "zai"}, id="main-order"),
])
def test_switch_to_available_source_persists_to_env_and_file(env_file, params, env_after):
    main = _main_models()
    out = _switch(params, env_file, main=main)

    assert out.startswith("OK")
    lines = env_file.read_text(encoding="utf-8").splitlines()
    for key, value in env_after.items():
        assert os.environ[key] == value and f"{key}={value}" in lines
    assert "OTHER=1" in lines
    assert main.current.name == params.get("main", "claude-code")


@pytest.mark.parametrize("params, available, kw, reason", [
    pytest.param({"source": "lmstudio"}, {}, {}, "недоступен", id="unavailable"),
    pytest.param({"source": "gpt"}, LMS, {}, "неизвестен", id="unknown"),
    pytest.param({"source": "zai", "model": "x"}, LMS, {}, "только для lmstudio", id="model-only-for-lmstudio"),
    pytest.param({"source": "lmstudio"}, LMS, {"agent_backend_acp": True}, "ACP", id="acp-backend"),
    pytest.param({"source": "lmstudio"}, LMS, {"cooling": lambda: {"lmstudio"}}, "в паузе", id="in-cooldown"),
    pytest.param({"main": "llm", "source": "gpt"}, LMS, {}, "неизвестен", id="bad-source-keeps-main"),
])
def test_refused_switch_changes_nothing(env_file, params, available, kw, reason):
    before = env_file.read_text(encoding="utf-8")
    main = _main_models()

    assert reason in _switch(params, env_file, available, main=main, **kw)
    assert env_file.read_text(encoding="utf-8") == before
    assert (os.environ["LLM_SOURCE"], os.environ["LMSTUDIO_MODEL"]) == ("zai", "old")
    assert main.current.name == "claude-code"


def test_parallel_switches_end_consistent(env_file):
    async def both():
        resolve = _resolver(LMS)
        await asyncio.gather(mm.switch_main_model_impl({"source": "lmstudio"}, resolve, SOURCES, env_file),
                             mm.switch_main_model_impl({"source": "zai"}, resolve, SOURCES, env_file))
    asyncio.run(both())
    assert f"LLM_SOURCE={os.environ['LLM_SOURCE']}" in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("source, status, llm", [
    pytest.param("zai", 409, None, id="conflict-on-unavailable"),     # ключа z.ai нет - источник недоступен
    pytest.param("lmstudio", 200, "lmstudio", id="reports-turn-model"),
])
def test_main_model_endpoint(app_client, monkeypatch, tmp_path, source, status, llm):  # noqa: F811
    """Ручка идёт настоящим switch_main_model_impl; сеть LM Studio подменена, .env - во временной папке."""
    client, _ = app_client
    monkeypatch.chdir(tmp_path)   # set_env пишет .env текущей папки
    monkeypatch.setenv("LLM_SOURCE", "zai")
    for var in ("ZAI_API_KEY", "SAM_AGENT_BACKEND", "LLM_FAST_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm_client, "_cooldowns", {})

    async def lmstudio(*a, **kw):
        return {"source": "lmstudio", "model": "q", "model_server": "http://lm/v1", "prompt_format": "qwen"}
    monkeypatch.setattr(llm_router, "_check_lmstudio", lmstudio)

    resp = client.post("/sam/main-model", json={"source": source}, headers=privilege_key.headers())
    assert resp.status_code == status
    if llm:
        assert (resp.json()["llm"], resp.json()["model"]) == (llm, "q")
        assert os.environ["LLM_SOURCE"] == "lmstudio" and "LLM_SOURCE=lmstudio" in (tmp_path / ".env").read_text(
            encoding="utf-8")


POLZA_ENVS = {"lmstudio": "LMSTUDIO_MODEL", "polza": "POLZA_MODEL"}


def test_model_of_an_aggregator_is_written_to_its_own_variable(env_file, monkeypatch):
    """Переключение polza + модель из списка пишет POLZA_MODEL; LMSTUDIO_MODEL не трогается."""
    monkeypatch.setenv("POLZA_MODEL", "z-ai/glm-5.3-flash")
    out = asyncio.run(mm.switch_main_model_impl(
        {"source": "polza", "model": "deepseek/deepseek-v4-flash"}, _resolver({"polza": {"source": "polza", "model": "deepseek/deepseek-v4-flash"}}),
        ("lmstudio", "polza"), env_file, model_envs=POLZA_ENVS))
    assert out.startswith("OK") and "deepseek/deepseek-v4-flash" in out
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert "LLM_SOURCE=polza" in lines and "POLZA_MODEL=deepseek/deepseek-v4-flash" in lines and "LMSTUDIO_MODEL=old" in lines


def test_aggregator_model_outside_the_list_is_refused(monkeypatch):
    """Опечатка в имени модели: источник отказывает, ход остаётся на прежней модели (resolve вернул None)."""
    monkeypatch.setenv("POLZA_API_KEY", "test-do-not-use")
    monkeypatch.setenv("POLZA_MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setenv("POLZA_MODELS", "z-ai/glm-5.3-flash,deepseek/deepseek-v4-flash")
    assert asyncio.run(llm_router.source_config("polza", "deepseek/deepseek-v4-flash"))["model"] == "deepseek/deepseek-v4-flash"
    assert asyncio.run(llm_router.source_config("polza", "deepseek/deepseek-v4-flsh")) is None
    assert asyncio.run(llm_router.source_config("polza"))["model"] == "z-ai/glm-5.3-flash"      # без model - активная


def test_model_options_list_active_and_selectable_models_only_for_keyed_aggregators(monkeypatch):
    for key in ("POLZA_API_KEY", "OPENROUTER_API_KEY", "POLZA_MODEL", "POLZA_MODELS"):
        monkeypatch.delenv(key, raising=False)
    assert llm_router.model_options() == {}
    monkeypatch.setenv("POLZA_API_KEY", "test-do-not-use")
    monkeypatch.setenv("POLZA_MODEL", "a/x")
    monkeypatch.setenv("POLZA_MODELS", "a/x, b/y ,")
    assert llm_router.model_options() == {"polza": {"active": "a/x", "models": ["a/x", "b/y"]}}


def _explain_env(monkeypatch, key=True, model="z-ai/glm-5.3-flash", listed="z-ai/glm-5.3-flash,deepseek/deepseek-v4-flash"):
    for var, value in (("POLZA_API_KEY", "test-do-not-use" if key else None), ("POLZA_MODEL", model), ("POLZA_MODELS", listed)):
        monkeypatch.delenv(var, raising=False)
        if value:
            monkeypatch.setenv(var, value)


@pytest.mark.parametrize("kwargs, model, expected", [
    ({"key": False}, "", "нет ключа POLZA_API_KEY в окружении"),
    ({}, "anthropic/claude-sonnet-4.5", "модели anthropic/claude-sonnet-4.5 нет в POLZA_MODELS; допустимые: z-ai/glm-5.3-flash, deepseek/deepseek-v4-flash"),
    ({"model": "", "listed": ""}, "", "не задана модель, задай POLZA_MODEL"),
], ids=["no-key", "model-outside-list", "no-model"])
def test_unavailable_reason_names_the_cause_not_a_guess(monkeypatch, kwargs, model, expected):
    """28 окт: «источник недоступен (нет ключа или сервис не отвечает)» на опечатку в имени модели - SAM гадал про лимит."""
    _explain_env(monkeypatch, **kwargs)
    assert llm_router.unavailable_reason("polza", model) == expected


def test_refused_switch_tells_the_reason_from_the_explainer(env_file, monkeypatch):
    _explain_env(monkeypatch)
    out = asyncio.run(mm.switch_main_model_impl(
        {"source": "polza", "model": "anthropic/claude-sonnet-4.5"}, _resolver({}), ("lmstudio", "polza"), env_file,
        model_envs=POLZA_ENVS, explain=llm_router.unavailable_reason))
    assert "нет в POLZA_MODELS" in out and "остался прежний" in out and "сервис не отвечает" not in out
