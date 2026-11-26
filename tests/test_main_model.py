"""SAM-91, SAM-176: основная модель берёт на каждый ход первую доступную по приоритету из конфига, держится за
текущую, пока та работает; prefer ставит выбранную первой."""
import pytest

from gateway import main_model as mm
from tests.fakes_core import app_client  # noqa: F401  (фикстура)


def _main(up: dict, order="claude-code,llm", on_switch=None):
    """Ведущий (claude-code) и модель шлюза (llm) с доступностью из словаря up."""
    models = {mm.CLAUDE_CODE: mm.claude_code(lambda: up["cc"], lambda m: None), mm.LLM: mm.llm(lambda: up["llm"])}
    return mm.MainModel(models, mm.parse_order(order), **({"on_switch": on_switch} if on_switch else {}))


def test_default_order_puts_the_gateway_model_first(monkeypatch):
    """Целевой путь - llm за роутером (за ним может стоять Cortex за ACP). claude-code - обход для стендов без
    API ключа, где подписка на CLI работает вместо API: он включается строкой, а не стоит первым по умолчанию."""
    monkeypatch.delenv(mm.ENV, raising=False)
    assert mm.parse_order() == ["llm"]


def test_main_model_follows_priority_with_inertia():
    up, switched = {"cc": False, "llm": True}, []
    main = _main(up, on_switch=lambda was, now: switched.append((was, now)))
    # текущая показывается без проверки доступности
    assert main.current.name == mm.CLAUDE_CODE

    assert main.pick().name == mm.LLM and main.pick().in_gateway   # первая доступная по приоритету
    up["cc"] = True
    assert main.pick().name == mm.LLM                               # инертность: llm работает - возврата нет
    up["llm"] = False
    picked = main.pick()
    assert picked.name == mm.CLAUDE_CODE and not picked.in_gateway  # смена только при отказе текущей
    assert switched == [("claude-code", "llm"), ("llm", "claude-code")]

    up["cc"] = False                                                # никого нет - остаётся текущая
    assert main.pick().name == mm.CLAUDE_CODE

    # порядок - из конфига, без правки кода
    assert _main({"cc": True, "llm": True}, order="llm,claude-code").pick().name == mm.LLM


@pytest.mark.parametrize("order", [["gpt-9"], []], ids=["unknown", "empty"])
def test_bad_main_model_order_fails_in_one_line(order):
    with pytest.raises(ValueError, match="SAM_MAIN_MODELS"):
        mm.MainModel({mm.LLM: mm.llm()}, order)


def test_outside_model_gets_non_voice_input():
    got = []
    mm.claude_code(lambda: True, got.append).deliver({"text": "из монитора", "source": "monitor"})
    mm.llm().deliver({"text": "мимо"})   # модель шлюза отвечает в ходе, доставка ей не нужна
    assert got == [{"text": "из монитора", "source": "monitor"}]


@pytest.mark.parametrize("cc_up, order, name, error, order_after, current", [
    pytest.param(True, "claude-code,llm", "llm", "", ["llm", "claude-code"], "llm", id="known-moves-first"),
    pytest.param(True, "claude-code,llm", "gpt", "нет", ["claude-code", "llm"], "claude-code", id="unknown"),
    pytest.param(False, "llm,claude-code", "claude-code", "недоступна", ["llm", "claude-code"], "llm",
                 id="unavailable-lead"),
    pytest.param(True, "llm", "claude-code", "", ["claude-code", "llm"], "claude-code",
                 id="registered-outside-order"),
])
def test_prefer_moves_model_first(cc_up, order, name, error, order_after, current):
    main = _main({"cc": cc_up, "llm": True}, order=order)
    main.pick()

    result = main.prefer(name)
    assert (error in result) if error else result == ""
    assert main.order == order_after and main.current.name == current


def test_lead_is_available_only_while_lead_alive_is_fresh_and_health_shows_who_leads(tmp_path, monkeypatch,
                                                                                     app_client):  # noqa: F811
    """main-model 12, 14: ведущий claude-code доступен, пока lead.alive моложе SAM_LEAD_ALIVE_S (5 с); пропал -
    ход берёт llm, монитор получает main_model_switch, /health и ярлык «ведёт» показывают текущую. Настоящие
    _lead_online и реестр основной модели шлюза; подменены файл шины (папка теста), модель и пробы компонентов."""
    import os
    import time

    from gateway import agent, main
    client, _ = app_client
    alive = tmp_path / "lead.alive"
    monkeypatch.setattr(agent, "_LEAD_ALIVE_FILE", alive)
    registry = agent.main_models
    monkeypatch.setattr(registry, "_order", [registry._models[mm.CLAUDE_CODE], registry._models[mm.LLM]])
    monkeypatch.setattr(registry, "_current", registry._models[mm.CLAUDE_CODE])
    switched = []
    monkeypatch.setattr(agent.event_bus, "emit", lambda name, data: switched.append((name, data)))
    monkeypatch.setattr(main, "_health_probes", lambda: {})

    async def no_llm(*args, **kwargs):
        return None
    monkeypatch.setattr(main, "get_best_llm", no_llm)

    def age(seconds):
        alive.touch()
        then = time.time() - seconds
        os.utime(alive, (then, then))

    age(4.5)
    assert agent._LEAD_ALIVE_S == 5.0 and registry.pick().name == mm.CLAUDE_CODE
    assert client.get("/health").json()["main_model"] == mm.CLAUDE_CODE
    age(5.5)
    assert registry.pick().name == mm.LLM
    assert switched == [("main_model_switch", {"from": mm.CLAUDE_CODE, "to": mm.LLM})]
    assert client.get("/health").json()["main_model"] == mm.LLM
    alive.unlink()
    assert not agent._lead_online()
