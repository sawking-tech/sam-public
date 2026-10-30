"""Отметка места для отпечатков радара (SAM-118): задержка, затем отсчёты в канал told. Отправка и сон подменены на границе."""
from tests.fakes_presence import load_script

mod = load_script("radar_mark_place")


def test_mark_place_sends_told_samples_after_delay():
    """Сначала задержка, потом N сообщений канала told с местом, устройством и человеком, между ними интервал."""
    events = []
    mod.mark("http://radar", "спальня", "phone-b", "resident-b", samples=3, interval=2.0, delay=30.0,
             send=lambda url, body: events.append(("send", body)), sleep=lambda s: events.append(("sleep", s)))
    told = ("send", {"channel": "told", "device": "phone-b", "who": "resident-b", "place": "спальня"})
    assert events == [("sleep", 30.0), told, ("sleep", 2.0), told, ("sleep", 2.0), told]
