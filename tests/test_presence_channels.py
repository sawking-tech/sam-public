"""Каналы свидетельств радара (SAM-105): Wi-Fi по хозяину устройства, время суток, чат и слова с именем и устройством, сведение в хабе и места устройства по уровню."""
from datetime import datetime

import pytest

from radar import presence_fuse as fz
from radar import presence_hub as ph
from radar import presence_messages as pm
from radar import presence_time as pt
from radar import presence_wifi as pw
from tests.fakes_presence import isolated_hub_env

A, B = "resident-a", "resident-b"
OWNERS = {"beta": A, "alpha": B}
BED = [(4.1, 0.9, 1.0, "кровать")]                                                         # где устройство по итогу ста бросков: одно пятно, вся доля
NOW = 10_000.0
PC_SPOT = {"x": 8.0, "z": 4.0, "pose": "sit", "owner": B}


def owner_of(name):
    low = (name or "").lower()
    return next((person for part, person in OWNERS.items() if part in low), None)


def wifi(devices, spots=None):
    return pw.evidence_from_devices(devices, owner_of, spots, NOW)


def dev(name, present=True, activity=0.0, ago=0.0, **extra):
    return {"name": name, "present": present, "activity": activity, "seen_ago_s": ago, **extra}


def chat_from_pc():
    return pm.evidence_from(pm.Message(NOW, "chat", "desk-pc", B), {"desk-pc": PC_SPOT})


# --- Wi-Fi: присутствующее устройство хозяина ---

def _one_spot():
    [e] = wifi([dev("Beta-Max-13")], lambda d: BED)
    assert (e.person, e.weight, e.x, e.z, e.pose, e.face) == (A, pw.PRESENT_WEIGHT, 4.1, 0.9, None, None)


def _no_spots():
    for spots in (None, lambda d: []):
        assert [(e.person, e.weight, e.x, e.z) for e in wifi([dev("Beta-Max-13")], spots)] == [(A, pw.PRESENT_WEIGHT, None, None)]


def _shares_split_the_weight():
    spots = [(8.0, 4.0, 0.7, "стол в кабинете"), (9.8, 5.3, 0.3, "диван в кабинете")]
    got = wifi([dev("Beta-Max-13", signal_db=-46.0)], lambda d: spots)
    assert [(e.person, e.x, e.z) for e in got] == [(A, 8.0, 4.0), (A, 9.8, 5.3)]
    assert sum(e.weight for e in got) == pytest.approx(pw.PRESENT_WEIGHT)
    assert "стол в кабинете" in got[0].note and "70%" in got[0].note


def _activity_raises_weight_to_the_cap_and_drives_the_silhouette():
    calm, busy = wifi([dev("beta")], lambda d: BED)[0], wifi([dev("beta", activity=1.0)], lambda d: BED)[0]
    assert calm.weight < busy.weight <= pw.MAX_WEIGHT
    assert fz.fuse([calm], NOW)[0].activity == 0.0 and fz.fuse(wifi([dev("alpha", activity=0.8)], lambda d: BED), NOW)[0].activity == 0.8


@pytest.mark.parametrize("case", [_one_spot, _no_spots, _shares_split_the_weight, _activity_raises_weight_to_the_cap_and_drives_the_silhouette],
                         ids=["one_spot", "no_spots", "shares", "activity"])
def test_owned_present_device_is_evidence_at_its_places_split_by_shares(case):
    case()


# --- Wi-Fi: ушедшее устройство ---

def _no_owner_nothing_and_left_is_against():
    assert wifi([dev("tablet-x")]) == []
    [away] = wifi([dev("beta", present=False, ago=120)])
    assert away.away and away.person == A and away.weight == pw.AWAY_WEIGHT
    assert wifi([dev("beta", present=False, ago=pw.AWAY_FORGET_S + 1)]) == []     # давно выключен: не признак ухода


def _confidence_drops_but_the_person_stays():
    home = fz.fuse([chat_from_pc()] + wifi([dev("alpha")], lambda d: BED), NOW)[0].confidence
    gone = fz.fuse([chat_from_pc()] + wifi([dev("alpha", present=False, ago=95)]), NOW)[0].confidence
    assert home == pytest.approx(1.0) and gone == pytest.approx(0.5)
    assert fz.fuse(wifi([dev("alpha", present=False, ago=95)]), NOW) == []          # одно «против» человека не создаёт


def _band_switch_and_ownerless_pc_are_not_leaving():
    gone = {"present": False, "seen_ago_s": 5000.0}
    other_band = {"name": "Beta-Max", "present": True, "signal_db": -65.0, "seen_ago_s": 0.0}
    assert not any(e.away for e in wifi([{**gone, "name": "Beta-Max"}, {**gone, "name": "Other-PC"}, other_band]))
    assert [e.away for e in wifi([{**gone, "name": "Beta-Max"}])] == [True]       # пропал совсем: признак ухода остаётся


@pytest.mark.parametrize("case", [_no_owner_nothing_and_left_is_against, _confidence_drops_but_the_person_stays, _band_switch_and_ownerless_pc_are_not_leaving],
                         ids=["left", "confidence_drops", "band_switch"])
def test_device_leaving_lowers_confidence_but_never_creates_or_removes_a_person(case):
    case()


# --- сообщения: чат, голос, «со слов» ---

def _told_about_a_lying_place_lives_for_hours():
    told = pm.evidence_from(pm.Message(NOW, "told", None, A, place="beta"), {"beta": {"x": 4.1, "z": 0.9, "pose": "lie", "face": 0.0}})
    assert (told.person, told.weight, told.pose, told.half_life_s) == (A, pm.TOLD_WEIGHT, "lie", pm.TOLD_REST_HALF_LIFE_S)
    [est] = fz.fuse([told] + wifi([dev("beta")], lambda d: BED), NOW)
    assert est.confidence == pytest.approx(1 - (1 - pm.TOLD_WEIGHT) * (1 - pw.PRESENT_WEIGHT))
    assert fz.fuse([told], NOW + pm.TOLD_REST_HALF_LIFE_S)[0].confidence == pytest.approx(pm.TOLD_WEIGHT / 2)


def _told_about_a_sitting_place_fades_in_a_quarter_of_an_hour():
    told = pm.evidence_from(pm.Message(NOW, "told", None, A, place="sofa"), {"sofa": {"x": 9.8, "z": 5.3, "pose": "sit", "face": 0.0}})
    assert told.half_life_s == pm.TOLD_HALF_LIFE_S == 900.0
    assert fz.fuse([told], NOW + pm.TOLD_HALF_LIFE_S)[0].confidence == pytest.approx(fz.fuse([told], NOW)[0].confidence / 2, rel=0.05)


def _unknown_place_or_no_name_gives_nothing():
    assert pm.evidence_from(pm.Message(NOW, "told", None, A, place="nowhere"), {}) is None
    assert pm.evidence_from(pm.Message(NOW, "told", None, None, place="beta"), {"beta": {"x": 1, "z": 1}}) is None


def _chat_from_a_named_device_is_certain():
    assert chat_from_pc().weight == 1.0
    [est] = fz.fuse([chat_from_pc()] + wifi([dev("alpha")], lambda d: BED), NOW)
    assert est.confidence == pytest.approx(1.0) and set(est.sources) == {"messages", "wifi"}


def _voice_weight_follows_loudness():
    spots = {"pc": {"x": 1.0, "z": 1.0}}
    weight = lambda level: pm.evidence_from(pm.Message(NOW, "voice", "pc", B, level=level), spots).weight
    assert weight(448) < weight(1209) == pytest.approx(weight(None)) and weight(448) > 0.4 * pm.VOICE_KNOWN_WEIGHT
    assert pm.proximity(None) is None and pm.proximity(pm.LEVEL_FAR - 50) == 0.0 and pm.proximity(pm.LEVEL_NEAR + 500) == 1.0
    assert 0.4 < pm.proximity((pm.LEVEL_FAR + pm.LEVEL_NEAR) / 2) < 0.6


@pytest.mark.parametrize("case", [_told_about_a_lying_place_lives_for_hours, _told_about_a_sitting_place_fades_in_a_quarter_of_an_hour,
                                  _unknown_place_or_no_name_gives_nothing, _chat_from_a_named_device_is_certain, _voice_weight_follows_loudness],
                         ids=["told_lying", "told_sitting", "told_unknown", "chat", "voice"])
def test_message_evidence_weight_and_fading_by_channel_level_and_pose(case):
    case()


def test_latest_place_of_a_person_wins_and_far_places_are_not_averaged():
    channel = pm.MessageChannel({"desk": {"x": 8.0, "z": 4.0, "pose": "sit"}, "kitchen-mic": {"x": 11.0, "z": 7.0, "pose": "stand"}})
    channel.feed(pm.Message(NOW - 60, "chat", "desk", B))
    channel.feed(pm.Message(NOW, "voice", "kitchen-mic", B))
    assert [(e.x, e.z) for e in channel.evidence(NOW)] == [(11.0, 7.0)]                    # не точка между кухней и столом
    channel.feed(pm.Message(NOW + 30, "chat", "desk", B))
    assert [(e.x, e.z) for e in channel.evidence(NOW + 30)] == [(8.0, 4.0)]
    channel.feed(pm.Message(NOW + 31, "voice", "kitchen-mic", None))                         # голос без опознания: человека нет
    assert len(channel.evidence(NOW + 31)) == 2

    sofa = fz.Evidence(source="messages", person=A, weight=0.85, t=NOW, x=9.8, z=5.3, pose="sit", note="со слов: диван")
    level = fz.Evidence(source="wifi", person=A, weight=0.5, t=NOW, x=9.3, z=2.5, pose="sit")           # другой диван, 2,8 м
    [est] = fz.fuse([sofa, level], NOW)
    assert (est.x, est.z) == (9.8, 5.3)                                                    # место с большим весом, не точка в стене
    [merged] = fz.fuse([sofa, fz.Evidence(source="wifi", person=A, weight=0.5, t=NOW, x=9.5, z=5.1)], NOW)
    assert 9.5 < merged.x < 9.8 and 5.1 < merged.z < 5.3                                   # близкие усредняются
    assert fz.fuse([sofa], NOW)[0].evidence == [{"x": 9.8, "z": 5.3, "w": 0.85, "source": "messages", "note": "со слов: диван"}]


def test_routine_window_through_midnight_gives_a_weak_guess_at_a_known_place():
    assert pt.in_window(23 * 60 + 30, "23:00", "08:00") and pt.in_window(3 * 60, "23:00", "08:00") and not pt.in_window(12 * 60, "23:00", "08:00")
    night = datetime(2026, 10, 10, 3, 0).timestamp()
    routines = [{"person": A, "from": "23:00", "to": "08:00", "place": "beta"}, {"person": B, "from": "23:00", "to": "08:00", "place": "nowhere"}]
    [e] = pt.evidence_from_routines(routines, {"beta": {"x": 4.95, "z": 0.3, "pose": "lie"}}, night)
    assert (e.person, e.weight, e.pose) == (A, pt.DEFAULT_WEIGHT, "lie")


# --- хаб ---

def hub_with_layout():
    layout = {"walls": [{"x1": 0, "z1": 0, "x2": 10, "z2": 0, "t": 0.2, "h": 2.5}, {"x1": 10, "z1": 0, "x2": 10, "z2": 8, "t": 0.2, "h": 2.5},
                        {"x1": 10, "z1": 8, "x2": 0, "z2": 8, "t": 0.2, "h": 2.5}, {"x1": 0, "z1": 8, "x2": 0, "z2": 0, "t": 0.2, "h": 2.5}],
              "objects": [{"title": "Стул", "kind": "furniture", "x": 7.75, "z": 3.75, "w": 0.5, "d": 0.5, "h": 0.85, "angle": 0.0}],      # под сидящим за компьютером есть сиденье
              "router": {"x": 5, "z": 4, "h": 2.4}, "rooms": [{"id": "h", "title": "дом", "x": 0, "z": 0, "w": 10, "d": 8}],
              "spots": {"desk-pc": PC_SPOT}}
    return ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: NOW, owners={"beta": A})


def test_hub_shows_a_person_from_any_channel_and_keeps_them_while_a_sign_is_left():
    hub = hub_with_layout()
    assert hub.identities(NOW) == []
    hub.note_message("chat", "unknown-laptop", B, now=NOW)                  # устройство не из плана: ничего
    assert hub.identities(NOW) == []
    hub.note_message("chat", "desk-pc", B, now=NOW)
    [person] = hub.identities(NOW)
    assert person["person"] == B and person["confidence"] == 1.0 and person["pose"] == "sit"
    later = hub.identities(NOW + 1200)                                      # 20 минут спустя: слабее, но не исчез
    assert later and later[0]["confidence"] < 0.1


def test_channels_are_chosen_by_env_and_told_is_off_by_default(tmp_path):
    hub = hub_with_layout()
    hub.note_message("told", None, A, place="beta", now=NOW)
    assert hub.identities(NOW) == []                                       # «со слов» выключено: слова не принимаются
    only_wifi = ph.build(isolated_hub_env(tmp_path, **{ph.FLAG_ENV: "rssi", ph.CHANNELS_ENV: "wifi"}))
    assert only_wifi.enabled_channels == {"wifi"} and len(only_wifi.channels) == 1
    assert ph.build(isolated_hub_env(tmp_path, **{ph.FLAG_ENV: "rssi"})).enabled_channels == {"wifi", "messages", "time", "pc"}


PLAN_PLACES = {**ph.pr.EMPTY_LAYOUT, "places": [{"label": "Стол", "x": 8.0, "z": 4.0}, {"label": "Кровать", "x": 4.8, "z": 0.9}]}
PHONE = "my-phone@phy0-ap0"


def level_hub(tmp_path, prints, clock=lambda: 10.0, layout=PLAN_PLACES, **kw):
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=clock, fingerprints_file=tmp_path / "fp.json", **kw)
    for place, levels in prints.items():
        for db in levels:
            hub.fingerprints.add(PHONE, place, db)
    return hub


def rows(hub, now, iface="phy0-ap0"):
    """Строки устройств страницы одного диапазона по имени."""
    return {r["name"]: r for r in hub.snapshot(now)["devices"] if r["iface"] == iface}


def test_device_row_shows_places_by_level_with_coordinates_and_a_gone_device_none(tmp_path):
    hub = level_hub(tmp_path, {"стол": (-46.0, -47.0, -45.0), "кровать": (-61.0, -60.0, -62.0)})
    hub.devices.update([ph.rs.Station("p1", "phy0-ap0", -46.0, 100, "My-Phone"), ph.rs.Station("p2", "phy0-ap0", -70.0, 100, "Gone-Phone"),
                        ph.rs.Station("p3", "phy1-ap0", -46.0, 100, "My-Phone")], 10.0)
    other_band = rows(hub, 10.0, iface="phy1-ap0")["My-Phone"]                                        # на другом диапазоне отпечатков нет
    assert other_band["present"] and other_band["where"] == []
    hub.devices.update([ph.rs.Station("p1", "phy0-ap0", -46.0, 100, "My-Phone")], 1000.0)          # второе устройство давно ушло
    got = rows(hub, 1000.0)
    best = got["My-Phone"]["where"][0]
    assert (best["place"], best["x"], best["z"]) == ("стол", 8.0, 4.0) and best["p"] > 0.9            # координаты места идут с ответом
    assert got["Gone-Phone"]["where"] == [] and got["Gone-Phone"]["field"] == []

    near = level_hub(tmp_path, {"стол": (-46.0, -47.0, -45.0), "кровать": (-50.0, -49.0, -51.0)})
    near.devices.update([ph.rs.Station("p1", "phy0-ap0", -48.0, 100, "My-Phone")], 10.0)
    field = rows(near, 10.0)["My-Phone"]["field"]                                                      # всё поле мест с координатами
    assert {f["place"] for f in field} == {"стол", "кровать"} and sum(f["p"] for f in field) == pytest.approx(1.0, abs=0.02)
    assert all("x" in f and "z" in f for f in field)

    measured = level_hub(tmp_path, {"кровать в спальне": (-61.0, -60.0, -62.0)}, layout={**ph.pr.EMPTY_LAYOUT, "places": []},
                         controls={"Кровать в спальне": {"x": 4.8, "z": 0.9}})
    measured.devices.update([ph.rs.Station("p1", "phy0-ap0", -61.0, 100, "My-Phone")], 10.0)
    row = rows(measured, 10.0)["My-Phone"]                                                            # места нет в плане: координаты из замера
    assert (row["where"][0]["x"], row["where"][0]["z"]) == (4.8, 0.9) and row["at"] == {"place": "кровать в спальне", "x": 4.8, "z": 0.9}


def test_level_estimate_updates_once_per_sample_and_a_named_place_sets_it(tmp_path):
    now = [10.0]
    hub = level_hub(tmp_path, {"стол": (-46.0, -47.0, -45.0), "кровать": (-50.0, -51.0, -49.0)}, clock=lambda: now[0])
    hub.devices.update([ph.rs.Station("p1", "phy0-ap0", -48.5, 100, "My-Phone")], 10.0)
    first = rows(hub, 10.0)["My-Phone"]["field"]
    assert rows(hub, 10.0)["My-Phone"]["field"] == first                    # тот же отсчёт повторно: оценка не усиливается
    now[0] = 11.0
    hub.devices.update([ph.rs.Station("p1", "phy0-ap0", -48.5, 100, "My-Phone")], 11.0)
    second = rows(hub, 11.0)["My-Phone"]["field"]                           # новый отсчёт: прошлая оценка сработала как априор
    assert second != first and sum(f["p"] for f in second) == pytest.approx(1.0, abs=0.02)

    named = level_hub(tmp_path, {"кровать": (-60.0,)})
    named.feed_stations([ph.rs.Station("p1", "phy0-ap0", -46.0, 100, "My-Phone")], now=10.0)
    named.note_message("told", "phone", B, place="Стол")                    # названное место ставит оценку этого устройства и диапазона
    field = {f["place"]: f["p"] for f in rows(named, 10.0)["My-Phone"]["field"]}
    assert field["стол"] >= 0.9 and set(field) <= {"стол", "кровать"}


def test_level_places_become_person_evidence_only_when_switched_on(tmp_path):
    layout = {**ph.pr.EMPTY_LAYOUT, "people": {B: {}}, "places": [{"label": "Стол", "x": 8.0, "z": 4.0}]}
    for switch in (False, True):
        hub = level_hub(tmp_path, {"стол": (-46.0, -47.0, -45.0)}, layout=layout, level_evidence=switch, owners={"phone": B})
        hub.devices.update([ph.rs.Station("p1", "phy0-ap0", -46.0, 100, "My-Phone")], 10.0)
        placed = [e for e in hub.channels[0].evidence(10.0) if e.x is not None]
        assert bool(placed) is switch
    coordless = level_hub(tmp_path, {"стол": (-46.0, -47.0, -45.0), "лишнее без координат": (-46.0, -46.5, -45.5), "ещё лишнее": (-46.0, -46.5, -45.5)},
                          layout=layout, level_evidence=True, owners={"phone": B})
    coordless.devices.update([ph.rs.Station("p1", "phy0-ap0", -46.0, 100, "My-Phone")], 10.0)
    assert [e for e in coordless.channels[0].evidence(10.0) if e.x is not None] == []    # две трети вероятности на местах без координат: точки нет
