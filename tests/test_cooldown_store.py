"""Долгие паузы поставщиков переживают перезапуск шлюза (SAM-258)."""
from gateway import cooldown_store as store


def test_a_long_pause_survives_a_restart_with_its_remaining_time(tmp_path):
    path = tmp_path / "c.json"
    store.remember(path, "openrouter", 3600, wall_now=1000.0)
    restored = store.restore(path, wall_now=1600.0, mono_now=50.0)       # прошло 600 с, монотонные часы другие
    assert restored == {"openrouter": 50.0 + 3000.0}


def test_expired_pause_is_not_restored(tmp_path):
    path = tmp_path / "c.json"
    store.remember(path, "openrouter", 3600, wall_now=1000.0)
    assert store.restore(path, wall_now=1000.0 + 3600, mono_now=0.0) == {}


def test_short_pauses_are_not_written(tmp_path):
    path = tmp_path / "c.json"
    store.remember(path, "polza", store.PERSIST_MIN_S - 1, wall_now=1000.0)
    assert not path.exists()


def test_broken_or_missing_file_means_no_pauses(tmp_path):
    assert store.restore(tmp_path / "none.json", 1.0, 1.0) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    assert store.restore(bad, 1.0, 1.0) == {}


def test_new_record_drops_expired_ones_and_keeps_live_ones(tmp_path):
    path = tmp_path / "c.json"
    store.remember(path, "old", 3600, wall_now=0.0)
    store.remember(path, "a", 3600, wall_now=1000.0)
    store.remember(path, "b", 3600, wall_now=5000.0)               # old (до 3600) и a (до 4600) просрочены
    assert set(store.read_until(path)) == {"b"}
