"""Состояние радара между перезапусками (SAM-118): файл, свежесть слов, кто уже был дома, и ход хаба после рестарта. Файлы - в tmp_path, часы внедрены."""
import json

from radar import presence as pr
from radar import presence_hub as ph
from radar import presence_state as ps
from radar import presence_tuning as tune
from radar import presence_walk as pw

OWNER, GUEST_OWNER, THIRD = "owner", "guest-owner", "third"          # нейтральные идентификаторы людей
PLACE_KEY = "place-key"                                              # ключ места: проверяется сохранение слов, а не расчёт положения
NOW = 1_800_000_000.0
SOFA_PLACE = {"label": "Диван в кабинете", "x": 8.8, "z": 4.86}


def _hub(clock_at, **kwargs):
    return ph.PresenceHub(ph.MODE_RSSI, kwargs.pop("layout", pr.DEMO_LAYOUT), clock=lambda: clock_at, **kwargs)


def _message(t, who=OWNER):
    return {"t": t, "channel": "told", "device": None, "who": who, "place": PLACE_KEY, "level": None, "pose": None}


def test_radar_state_survives_a_restart(tmp_path, capsys):
    """Хаб пишет переключатели и свежие слова, новый хаб их восстанавливает; старые и будущие слова отброшены,
    просроченный список «дома» пуст; нет файла или битый - None с одной строкой; неизвестное поле старого файла игнорируется."""
    path = tmp_path / "state.json"
    store = ps.StateStore(path, lambda target, text: target.write_text(text, encoding="utf-8"))
    assert store.load() is None
    hub = _hub(NOW, state_file=path)
    hub.set_real(True)
    hub.note_message("told", None, who=OWNER, place=PLACE_KEY, now=NOW - 100)
    hub.save_state()
    saved = store.load()
    assert saved.real is True and [m["who"] for m in saved.messages] == [OWNER]
    assert ps.from_json(ps.to_json(saved)) == saved
    assert ps.from_json('{"real": true, "future_field": 1}').real is True
    again = _hub(NOW, state_file=path)
    assert again.real is True and [m.who for m in again.messages.recent()] == [OWNER]

    old, future = NOW - ps.MAX_MESSAGE_AGE_S - 1, NOW + 4000.0
    store.save(ps.SavedState(saved_at=NOW, messages=[_message(NOW), _message(old, THIRD), _message(future, THIRD)], home=[OWNER, GUEST_OWNER]))
    fresh = _hub(NOW, state_file=path)
    assert [m.who for m in fresh.messages.recent()] == [OWNER] and fresh.walkers.on_map() == [GUEST_OWNER, OWNER]
    assert _hub(NOW + ps.MAX_HOME_AGE_S + 1, state_file=path).walkers.on_map() == []

    path.write_text("{не json", encoding="utf-8")
    assert store.load() is None and _hub(NOW, state_file=path).real is False
    assert "начинаю с чистого" in capsys.readouterr().out


def test_person_home_before_restart_appears_in_place_not_at_the_door():
    """Бывший дома появляется на месте, незнакомый идёт от входа; сохранение до первого кадра список не стирает."""
    entry = (6.2, 8.0)
    walkers = pw.Walkers(lambda pts: [pts[0], pts[-1]], tune.Tuning(), entry=lambda: entry)
    walkers.seed_home([OWNER, GUEST_OWNER])
    assert walkers.on_map() == [GUEST_OWNER, OWNER]                                                       # кадра ещё не было: прежние жильцы
    walkers.apply([{"person": GUEST_OWNER, "x": 8.0, "z": 4.0}], NOW)                                     # первый вызов: старт радара
    row = walkers.apply([{"person": OWNER, "x": 8.3, "z": 4.0}], NOW + pw.ABSENT_ENTRY_S + 5)[0]          # позже старта, но был дома до него
    assert (row["x"], row["z"]) == (8.3, 4.0) and not row.get("walking")
    stranger = walkers.apply([{"person": THIRD, "x": 9.0, "z": 4.0}], NOW + pw.ABSENT_ENTRY_S + 6)[0]     # не был дома: приходит через вход
    assert stranger.get("walking") is True
    assert walkers.on_map() == [GUEST_OWNER, OWNER, THIRD]                                                # после кадров - те, кто на карте
    fresh = pw.Walkers(None, tune.Tuning())
    fresh.seed_home([OWNER, GUEST_OWNER])
    fresh.apply([{"person": OWNER, "x": 1.0, "z": 1.0}], 10.0)
    assert fresh.on_map() == [OWNER]                                                                      # кого не было в первом кадре, на карте нет


def test_told_place_puts_the_person_there_and_the_latest_words_win():
    """Слова о месте плана ставят человека туда; считаются только последние слова человека: один человек - в одном месте."""
    layout = {**pr.DEMO_LAYOUT, "places": [SOFA_PLACE, {"label": "Туалет", "x": 7.7, "z": 7.5}]}
    hub = _hub(1000.0, layout=layout)
    hub.set_real(True)
    hub.note_message("told", None, who=GUEST_OWNER, place="диван в кабинете", now=900.0)
    assert [(e.person, e.x, e.z) for e in hub.messages.evidence(1000.0)] == [(GUEST_OWNER, 8.8, 4.86)]
    hub.note_message("told", None, who=GUEST_OWNER, place="туалет", now=990.0)
    assert [(e.person, e.x, e.z) for e in hub.messages.evidence(1000.0)] == [(GUEST_OWNER, 7.7, 7.5)]
    assert [t["place"] for t in hub.messages.told_now(1000.0, 0.5)] == ["туалет"]


def test_place_points_take_the_pose_of_the_furniture_under_them():
    """Точка у края дивана - сидеть на диване, далёкая точка - стоять."""
    layout = {**pr.DEMO_LAYOUT, "objects": [{"title": "Диван", "kind": "furniture", "x": 8.8, "z": 4.9, "w": 2.0, "d": 0.9}],
              "places": [{"label": "Диван в кабинете", "x": 8.7, "z": 4.86}, {"label": "Коридор", "x": 3.0, "z": 1.0}]}
    points = _hub(1.0, layout=layout).place_points()
    assert (points["диван в кабинете"]["pose"], round(points["диван в кабинете"]["x"], 2), round(points["диван в кабинете"]["z"], 2)) == ("sit", 9.8, 5.35)
    assert points["коридор"]["pose"] == "stand"


def _pile(hub):
    """(человек, гости, место) из копилки привычек."""
    return sorted((person, guests, place) for day in hub.habit_stats.days.values() for person, slots in day.items()
                  for by_guests in slots.values() for guests, places in by_guests.items() for place in places)


def test_habit_pile_records_only_sure_observations_and_live_words(tmp_path):
    """В копилку идут уверенные строки без ручной вероятности (другие уверенные - гости) и слова, пока в силе; копилка переживает рестарт."""
    layout = {**pr.DEMO_LAYOUT, "places": [SOFA_PLACE]}
    path = tmp_path / "habits.json"
    hub = _hub(NOW, layout=layout, habit_stats_file=path)
    rows = [{"person": OWNER, "x": 8.8, "z": 4.86, "confidence": 0.99, "sources": ["wifi"]},
            {"person": GUEST_OWNER, "x": 8.8, "z": 4.86, "confidence": 0.99, "sources": ["debug", "wifi"]},     # ручная вероятность - не данные
            {"person": THIRD, "x": 8.8, "z": 4.86, "confidence": 0.5, "sources": ["wifi"]}]                    # неуверен - не данные
    hub.recorder.record(rows, hub.messages.told_now(NOW, ph.TOLD_SURE_STRENGTH), 5.0, NOW)
    hub.save_habit_stats()
    again = _hub(NOW, layout=layout, habit_stats_file=path)
    assert _pile(again) == [(OWNER, "", "диван в кабинете")]
    assert json.loads(path.read_text(encoding="utf-8"))

    words = _hub(NOW, layout=layout)                                                                            # слова через кадр хаба
    words.set_real(True)
    words.note_message("told", None, who=GUEST_OWNER, place="диван в кабинете", now=NOW - 600)
    words.set_person(OWNER, 1.0, "диван в кабинете")                                                            # ручная вероятность - не данные
    words.step(NOW + 1.0)
    assert _pile(words) == [(GUEST_OWNER, "", "диван в кабинете")]
    words.step(NOW + 4 * 3600)                                                                                  # слова выдохлись: нового слота нет
    assert len(_pile(words)) == 1
