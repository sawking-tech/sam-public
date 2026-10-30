"""Очередь своих реплик голосового слоя (SAM-49): обычное - в паузе, важное - сразу."""
import pytest

from voice.layer import agenda


def _intent(priority="normal"):
    return agenda.Intent(id="a1", text="Уже третий час, ложись", priority=priority, expires=1e12, wav=None)


@pytest.mark.parametrize("priority, speaking, hearing, since_human, picked", [
    ("normal", False, True, 10, False),
    ("normal", False, False, 1, False),
    ("normal", False, False, 10, True),
    ("high", False, True, 0, True),
    ("high", True, False, 10, False),
], ids=["normal-hearing", "normal-human-just-spoke", "normal-pause", "high-over-noise", "high-not-over-own-speech"])
def test_own_line_waits_for_pause_unless_high(priority, speaking, hearing, since_human, picked):
    got = agenda.pick([_intent(priority)], speaking=speaking, hearing=hearing, since_human=since_human,
                      queue_busy=False)
    assert (got is not None and got.id == "a1") if picked else got is None


def test_agenda_line_added_and_done(tmp_path):
    iid = agenda.add(tmp_path, "Уже третий час, ложись")   # без синтеза: аудио не готово, строка остаётся
    assert [i.id for i in agenda.load(tmp_path)] == [iid]
    agenda.mark_done(tmp_path, iid)
    assert agenda.load(tmp_path) == []
