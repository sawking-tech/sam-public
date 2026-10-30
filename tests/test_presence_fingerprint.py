"""Отпечатки уровня сигнала по местам (SAM-117): вероятности мест по уровню, накопление, запись по названному месту и маршрут."""
import pytest
from fastapi.testclient import TestClient

from radar import presence as pr
from radar import presence_api
from radar import presence_fingerprint as pf
from radar import presence_hub as ph
from radar import presence_rssi as rs


def sofa_and_bed():
    fp = pf.Fingerprints()
    for db in (-59.0, -60.0, -61.0):
        fp.add("phone", "диван", db)
    for db in (-74.0, -75.0, -76.0):
        fp.add("phone", "кровать", db)
    return fp


def _follow_the_level_and_the_prior():
    fp = sofa_and_bed()
    fp.add("phone", "туалет", -70.0)                                              # одной записи мало: места в расчёте нет
    near_sofa = fp.place_probabilities("Phone", -60.5)
    assert set(near_sofa) == {"диван", "кровать"} and sum(near_sofa.values()) == pytest.approx(1.0) and near_sofa["диван"] > 0.99
    between = fp.place_probabilities("phone", -67.5)                              # посередине: оба места поровну
    assert between["диван"] == pytest.approx(between["кровать"])
    assert fp.place_probabilities("other", -60.0) == {}
    assert fp.place_probabilities("phone", -60.5, prior={"диван": 0.0})["кровать"] > 0.99      # априор исключает место


def _own_spread_wider_than_the_floor():
    fp = pf.Fingerprints()
    for db in (-50.0, -60.0, -70.0):
        fp.add("phone", "шумное", db)                                              # разброс 8 дБ шире пола
    for db in (-60.0, -60.0, -60.0):
        fp.add("phone", "тихое", db)                                               # разброс ноль: берётся пол
    p = fp.place_probabilities("phone", -66.0)
    assert p["шумное"] > p["тихое"]


def _far_level_stays_even():
    far = sofa_and_bed().place_probabilities("phone", -120.0)
    assert sum(far.values()) == pytest.approx(1.0) and abs(far["диван"] - far["кровать"]) < 0.01


def _new_device_borrows_the_model():
    fp = pf.Fingerprints({"redmi-note-13-pro-5g@phy1-ap0": {"кухня": [-60.0] * 5, "спальня": [-75.0] * 5},
                          "s24-ultra@phy1-ap0": {"кухня": [-50.0] * 9, "спальня": [-70.0] * 9},
                          "redmi-note-13-pro-5g@phy0-ap0": {"кухня": [-40.0] * 5}})
    assert fp.stand_in("Redmi-Note-13-Pro@phy1-ap0") == "redmi-note-13-pro-5g@phy1-ap0"
    assert fp.stand_in("Pixel-9@phy1-ap0") == "s24-ultra@phy1-ap0"          # нет своей модели - больше замеров
    assert fp.stand_in("s24-ultra@phy1-ap0") == "s24-ultra@phy1-ap0"
    probs = fp.place_probabilities("Redmi-Note-13-Pro@phy1-ap0", -61.0)
    assert max(probs, key=probs.get) == "кухня"


def _keep_last_and_json():
    fp = pf.Fingerprints()
    for i in range(pf.KEEP_LAST + 10):
        n = fp.add("Phone", "Диван", -60.0 - (i % 2))
    assert n == pf.KEEP_LAST and (fp.summary()[0]["device"], fp.summary()[0]["place"]) == ("phone", "диван")
    assert pf.Fingerprints.from_json(fp.to_json()).summary() == fp.summary()


@pytest.mark.parametrize("case", [_follow_the_level_and_the_prior, _own_spread_wider_than_the_floor, _far_level_stays_even, _new_device_borrows_the_model, _keep_last_and_json],
                         ids=["level_prior", "spread", "far", "borrowed_model", "keep_last_json"])
def test_place_probabilities_follow_level_spread_prior_and_borrowed_model(case):
    case()


def test_naming_a_place_records_a_fingerprint_per_band_and_the_route_lists_and_erases_it(tmp_path):
    file = tmp_path / "fp.json"
    hub = ph.PresenceHub(ph.MODE_RSSI, {**pr.EMPTY_LAYOUT}, clock=lambda: 10.0, fingerprints_file=file)
    assert "told" not in hub.enabled_channels                                      # «со слов» выключено, а отпечаток всё равно пишется
    hub.feed_stations([rs.Station("p1", "phy1-ap0", -60.0, 100, "My-Phone")], now=10.0)
    hub.note_message("chat", "phone", "resident-a", place="диван")
    hub.note_message("chat", "tablet", "resident-a", place="кухня")                # устройства не видно: нечего записывать
    assert hub.fingerprints.summary() == [{"device": "my-phone@phy1-ap0", "place": "диван", "n": 1, "mean_db": -60.0, "min_db": -60.0, "max_db": -60.0}]
    hub.devices.update([rs.Station("p2", "phy0-ap0", -45.0, 100, "My-Phone")], 11.0)         # телефон ушёл на другой диапазон
    hub.note_message("told", "phone", "resident-a", place="диван")
    assert sorted((r["device"], r["mean_db"]) for r in hub.fingerprints.summary()) == [("my-phone@phy0-ap0", -45.0), ("my-phone@phy1-ap0", -60.0)]
    client = TestClient(presence_api.create_app(hub))
    assert {r["place"] for r in client.get("/sam/presence/fingerprints").json()["fingerprints"]} == {"диван"}
    hub.fingerprints.add("tablet@phy0-ap0", "диван", -50.0)
    assert client.delete("/sam/presence/fingerprints", params={"place": "Диван", "device": "my-phone@phy0-ap0"}).json() == {"removed": 1}
    assert client.delete("/sam/presence/fingerprints", params={"place": "диван"}).json() == {"removed": 2}
    assert client.get("/sam/presence/fingerprints").json()["fingerprints"] == []
    assert "диван" not in file.read_text(encoding="utf8")                         # файл следует за удалением
