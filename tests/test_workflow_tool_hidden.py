"""SAM-219: вызов инструмента шагом workflow ложится в историю скрытой записью - монитор его видит, модель и сжатие нет."""
import types

from gateway import agent_loop
from gateway.event_bus import EventBus
from gateway.repos.session_repo import IGNORE, SessionRepo
from tests.fakes_core import loop_deps


def _call(name):
    return types.SimpleNamespace(id="wf-1", function=types.SimpleNamespace(name=name, arguments="{}"))


async def test_workflow_call_event_carries_the_hidden_mark_and_a_model_call_does_not():
    async def sync_skills(args):
        return "без изменений"
    deps = loop_deps(tool_impl={"sync_skills": sync_skills})
    await agent_loop.execute_tool(deps, _call("sync_skills"), {}, "voice", hidden=True)
    await agent_loop.execute_tool(deps, _call("sync_skills"), {}, "voice")
    calls = deps.event_bus.by_name("tool_call")
    assert calls[0][IGNORE] == agent_loop.HIDDEN_CALL and IGNORE not in calls[1]


def test_hidden_call_is_stored_but_left_out_of_the_model_history(mongo_db):
    bus = EventBus()
    bus._db = mongo_db
    bus._persist("tool_call", "voice", {"name": "sync_skills", "args": {}, IGNORE: agent_loop.HIDDEN_CALL})
    bus._persist("user_message", "voice", {"content": "Сэм, привет"})
    assert mongo_db.sessions.count_documents({"session_id": "voice"}) == 2
    assert SessionRepo(mongo_db).get_all_for_compression("voice")[0]["role"] == "user"   # сжатию нечего пересказывать
    history = SessionRepo(mongo_db).get_recent("voice", 10)
    assert [h["role"] for h in history] == ["user"]
