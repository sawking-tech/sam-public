"""SAM-99: монитор по «/» подсказывает скиллы стенда и команды текущей основной модели; выбранная команда уходит
основной модели командой, а не фразой."""
import pytest

from gateway import agent, main, main_model as mm, monitor_commands as mc, skills
from tests.fakes_core import app_client  # noqa: F401 - фикстура

DRIFT = skills.Skill("drift-guard", "Методология против дрейфа", "тело")


def _claude(up=lambda: True, delivered=None, commands=lambda: []):
    return mm.claude_code(up, (delivered if delivered is not None else []).append, commands)


@pytest.fixture
def client(app_client, monkeypatch):
    monkeypatch.setattr(agent, "_skills_index", [DRIFT])
    return app_client[0]


def test_command_list_is_stand_skills_then_model_commands(client, monkeypatch):
    monkeypatch.setenv(mm.CLAUDE_CODE_COMMANDS_ENV, "git-style, drift-guard, ,")
    assert [c["name"] for c in mm.commands_from_env()] == ["git-style", "drift-guard"]
    model = _claude(commands=mm.commands_from_env)
    # Скилл стенда не затеняется одноимённой командой модели
    assert mc.available([DRIFT], model) == [
        {"name": "drift-guard", "description": "Методология против дрейфа", "source": "skill"},
        {"name": "git-style", "description": "", "source": "claude-code"}]
    monkeypatch.setenv(mm.CLAUDE_CODE_COMMANDS_ENV, "")
    assert mm.commands_from_env() == []

    up = {"cc": True}
    models = mm.MainModel({mm.CLAUDE_CODE: _claude(up=lambda: up["cc"], commands=lambda: [{"name": "git-style"}]),
                           mm.LLM: mm.llm()}, ["claude-code", "llm"])
    monkeypatch.setattr(main, "main_models", models)
    got = client.get("/sam/commands").json()
    assert got["model"] == "claude-code" and [c["name"] for c in got["commands"]] == ["drift-guard", "git-style"]
    up["cc"] = False
    models.pick()
    got = client.get("/sam/commands").json()
    assert got["model"] == "llm" and [c["name"] for c in got["commands"]] == ["drift-guard"]


@pytest.mark.parametrize("model, body, status, delivered, turns, emitted", [
    (mm.CLAUDE_CODE, {"name": "drift-guard", "args": "начало задачи"}, 200,
     [{"text": "/drift-guard начало задачи", "source": "monitor", "session": "voice", "command": "drift-guard"}],
     [], [("user_message", {"content": "/drift-guard начало задачи", "session": "voice",
                            "command": "drift-guard"})]),
    (mm.LLM, {"name": "/drift-guard"}, 200, [], [("voice", f"{mc.COMMAND_MARK} /drift-guard")], None),
    (mm.CLAUDE_CODE, {"name": "rm"}, 404, [], [], []),
], ids=["outside-model-gets-command", "gateway-model-gets-marked-turn", "unknown-refused"])
def test_monitor_command_reaches_main_model_as_command(client, monkeypatch, model, body, status, delivered, turns,
                                                       emitted):
    got_delivered, got_turns, got_emitted = [], [], []
    models = {mm.CLAUDE_CODE: _claude(delivered=got_delivered), mm.LLM: mm.llm()}
    monkeypatch.setattr(main, "main_models", mm.MainModel({model: models[model]}, [model]))
    monkeypatch.setattr(main.event_bus, "emit", lambda t, d: got_emitted.append((t, d)))

    async def _turn(session, text, handler):   # ход модели шлюза - граница: дальше настоящий LLM
        got_turns.append((session, text))
    monkeypatch.setattr(main, "acquire_and_run", _turn)

    r = client.post("/sam/session/voice/command", json=body)
    assert r.status_code == status
    if status == 404:
        assert "/drift-guard" in r.json()["detail"]
    assert got_delivered == delivered and got_turns == turns
    if emitted is not None:
        assert got_emitted == emitted
