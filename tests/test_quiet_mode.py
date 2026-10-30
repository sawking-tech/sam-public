"""Тихий режим на сессию (gateway/quiet_mode.py): сессии независимы, переживает перезапуск, срок снимает, мост не шлёт фразу
без триггера, «включи тихий режим» включает и звучит подтверждение без модели, инструмент quiet, шаг workflow пропускается."""
import pytest

from gateway import addressee as ad
from gateway import quiet_mode as qm
from gateway.tools.quiet import quiet_impl
from tests.fakes_core import CountingLLM, resp, voice_turn_deps


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _book(tmp_path=None, clock=None, events=None):
    path = tmp_path / qm.QUIET_FILE if tmp_path is not None else None
    return qm.QuietBook(path, clock or Clock(), emit=(lambda name, payload: events.append((name, payload))) if events is not None else None)


def test_quiet_state_is_per_session_persists_and_expires(tmp_path):
    clock, events = Clock(), []
    book = _book(tmp_path, clock, events)
    book.on("voice", 10, qm.BY_TOOL)
    assert book.is_quiet("voice") and not book.is_quiet("workpc")      # сессии независимы
    again = _book(tmp_path, clock)                                          # перезапуск: состояние из файла шины
    assert again.is_quiet("voice") and again.active()[0]["session"] == "voice"
    clock.now += 9 * 60
    assert book.is_quiet("voice")
    clock.now += 2 * 60                                                     # срок вышел - тишина снята с событием
    assert not book.is_quiet("voice")
    assert events[-1] == (qm.EVENT, {"session": "voice", "quiet": False, "until": None, "by": qm.BY_EXPIRY})


def test_quiet_session_sends_nothing_until_a_trigger():
    book = _book()
    assert book.gate("voice", "что на ужин") == (True, False)            # не тихо - фраза идёт ходом
    book.on("voice", None, qm.BY_PHRASE)
    assert book.gate("voice", "что на ужин") == (False, False)           # тихо, триггера нет - хода нет, токенов нет
    assert book.gate("voice", "говори громче") == (False, False)         # частое слово тишину не снимает
    assert book.gate("voice", "можно говорить") == (True, True) and not book.is_quiet("voice")
    book.on("voice", None, qm.BY_PHRASE)
    assert book.gate("voice", "Сэм, что на ужин?") == (True, True)       # имя снимает тишину и идёт ходом


async def test_saying_quiet_on_and_off_works_without_the_model():
    from gateway import voice_turn as vt
    # фраза выключения проверяется первой: «выключи тихий режим» содержит «тихий режим»
    assert ad.asks_quiet("включи тихий режим") and not ad.asks_speak("включи тихий режим")
    assert ad.asks_speak("выключи тихий режим") and not ad.asks_quiet("выключи тихий режим")
    assert ad.decide(ad.Heard("включи тихий режим", "owner")).to == ad.BACKGROUND
    llm, said = CountingLLM([resp(content="<voice>Слушаю тебя.</voice>")]), []
    deps = voice_turn_deps(llm)
    deps.decide, deps.say = ad.decide, lambda session, text: said.append((session, text)) or True
    await vt.run_voice_turn(deps, "Включи тихий режим", "s", who="owner")
    await vt.run_voice_turn(deps, "что на ужин", "s", who="owner")
    assert deps.quiet.is_quiet("s") and llm.calls == [] and said == [("s", qm.ENTER_SAY)]
    await vt.run_voice_turn(deps, "выключи тихий режим", "s", who="owner")
    assert not deps.quiet.is_quiet("s") and llm.calls == [] and said[-1] == ("s", qm.EXIT_SAY)
    result = await vt.run_voice_turn(deps, "Сэм, что на ужин?", "s", who="owner")
    assert len(llm.calls) == 1 and result.voice == "Слушаю тебя."


def _quiet_tool(book):
    said = []
    say = lambda session, text: said.append((session, text)) or True
    assert "до отмены" in quiet_impl({"action": "on"}, "kitchen", book, say)
    assert book.is_quiet("kitchen") and not book.is_quiet("voice") and said == [("kitchen", qm.ENTER_SAY)]
    assert "тихо" in quiet_impl({"action": "status"}, "kitchen", book)
    quiet_impl({"action": "off"}, "kitchen", book, say)
    assert not book.is_quiet("kitchen") and said[-1] == ("kitchen", qm.EXIT_SAY)
    assert "минут" in quiet_impl({"action": "on", "minutes": "много"}, "kitchen", book)


async def _workflow_step(book, monkeypatch):
    from gateway import agent, workflows
    from gateway.workflow_spec import Step
    called = []

    async def deliver(*args, **kwargs):   # доставка основной модели - граница наружу
        called.append(args)
        return workflows.StepOutcome("llm", "сказала")
    book.on("voice", None, qm.BY_TOOL)
    monkeypatch.setattr(agent, "_quiet_book", book)
    monkeypatch.setattr(workflows, "deliver_main_model", deliver)
    step = Step(name="nudge", kind=workflows.MAIN_MODEL, text="иди спать")
    out = await agent._workflow_step(step, "workflow:late-night/nudge", "иди спать", "voice")
    assert called == [] and out.target == "quiet"
    await agent._workflow_step(step, "workflow:late-night/nudge", "иди спать", "workpc")
    assert len(called) == 1


@pytest.mark.parametrize("who", ["tool", "workflow_step"])
async def test_quiet_tool_and_workflow_step_respect_quiet(who, monkeypatch):
    """Инструмент quiet включает и выключает тишину сессии хода с подтверждением без модели; шаг workflow
    к основной модели в тихой сессии пропускается до модели."""
    book = _book()
    if who == "tool":
        _quiet_tool(book)
    else:
        await _workflow_step(book, monkeypatch)
