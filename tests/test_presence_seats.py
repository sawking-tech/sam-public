"""Места на опорах (SAM-105): на диване сидят в ряд, на кровати двое лежат, третий садится на угол; стул, ванна и унитаз - по одному.
Ритм «иногда» (прилечь, лечь в ванну, сесть, лечь порознь) подаётся функцией: обе ветки проверяются явно, ритм по умолчанию - отдельно."""
import math

import pytest

from radar import presence_seats as seats

SOFA = {"title": "Диван", "kind": "furniture", "x": 4.0, "z": 1.0, "w": 2.0, "d": 0.9, "angle": 0.0}          # центр (5.0; 1.45), длинная сторона вдоль x
BED = {"title": "Кровать", "kind": "furniture", "x": 0.0, "z": 0.0, "w": 1.6, "d": 2.0, "angle": 0.0}          # центр (0.8; 1.0), изголовье на -z, ноги на +z
TUB = {"title": "Ванна (примерно)", "kind": "furniture", "x": 0.0, "z": 0.0, "w": 0.7, "d": 1.5, "angle": 0.0}
TOILET = {"title": "Унитаз (примерно)", "kind": "furniture", "x": 0.0, "z": 0.0, "w": 0.4, "d": 0.65, "angle": 0.0}      # центр (0.2; 0.325), бачок на севере
CHAIR = {"title": "Стул вращающийся", "kind": "furniture", "x": 1.0, "z": 1.0, "w": 0.5, "d": 0.5, "angle": 0.0}
ROOM = {"id": "r", "title": "кабинет", "x": 3.0, "z": 0.0, "w": 5.0, "d": 3.0}                 # диван SOFA в этой комнате
P1, P2, P3, P4 = "p1", "p2", "p3", "p4"                                                    # нейтральные ключи людей
SOFA_AT, BED_AT, TUB_AT, TOILET_AT = (5.0, 1.45), (0.8, 1.0), (0.4, 0.8), (0.3, 0.5)
NOW = 1.0
ALWAYS, NEVER = (lambda *a: True), (lambda *a: False)                                     # ритм: этот период выпал / не выпал


def _rows(*points, pose="stand"):
    return [{"person": p, "x": x, "z": z, "pose": pose} for p, (x, z) in zip((P1, P2, P3, P4), points)]


def _by_person(rows):
    return {r["person"]: r for r in rows}


@pytest.mark.parametrize("count, width", [(3, 2.0), (4, 3.0)])
def test_sofa_seats_people_in_a_row_by_its_width(count, width):
    """Трое и больше сидят в ряд по ширине дивана, лицо - от функции front."""
    got = seats.settle(_rows(*[SOFA_AT] * count), [dict(SOFA, w=width)], front=lambda obj: 1.25, now=NOW)
    assert [r["pose"] for r in got] == ["sit"] * count and all(r["face"] == 1.25 for r in got)
    xs = sorted(r["x"] for r in got)
    assert all(b - a >= 0.6 - 1e-9 for a, b in zip(xs, xs[1:])) and all(4.0 <= x <= 4.0 + width for x in xs)


def test_sofa_sitter_with_someone_takes_the_seat_next_to_them():
    """Садящийся «с кем-то» садится рядом с ним, третий - с краю; без предпочтения третий оказался бы между ними."""
    rows = _rows(SOFA_AT, SOFA_AT, SOFA_AT)
    got = {r["person"]: r["x"] for r in seats.settle(rows, [SOFA], sits_with={P1: P3})}
    assert abs(got[P1] - got[P3]) < abs(got[P1] - got[P2]) and abs(got[P1] - got[P3]) <= 0.7
    plain = {r["person"]: r["x"] for r in seats.settle(rows, [SOFA])}
    assert abs(plain[P1] - plain[P2]) < abs(plain[P1] - plain[P3])


@pytest.mark.parametrize("kwargs, extra, expected", [
    (dict(sofa_pose=lambda person, title, now: "lie", rhythm=NEVER), [], "lie"),              # доли поз решают: лечь
    (dict(sofa_pose=lambda person, title, now: "sit", rhythm=ALWAYS, lie_alone={P1}), [], "sit"),   # доли поз решают: сидеть, ритм и привычка не спорят
    (dict(sofa_pose=lambda person, title, now: None, rhythm=ALWAYS), [], "lie"),             # долей на это время нет: решает ритм
    (dict(rhythm=ALWAYS), [], "lie"),                                                          # долей нет: ритм выпал
    (dict(rhythm=NEVER), [], "sit"),                                                           # ритм не выпал
    (dict(rhythm=ALWAYS, sit_only={P1}), [], "lie"),                                           # только сидит на кровати, а на диване может лечь
    (dict(rhythm=NEVER, lie_alone={P1}), [], "lie"),                                           # любит лежать один: в комнате никого
    (dict(rhythm=NEVER, lie_alone={P1}), [{"person": P3, "x": 7.0, "z": 2.0, "pose": "sit"}], "sit"),     # в комнате кто-то сидит
    (dict(rhythm=NEVER, lie_alone={P1}), [{"person": P3, "x": 20.0, "z": 20.0, "pose": "sit"}], "lie"),   # другой - в другой комнате
], ids=["shares-lie", "shares-sit", "no-shares-now", "rhythm-lie", "rhythm-sit", "sit-only-bed", "lie-alone", "company-in-room", "company-elsewhere"])
def test_lone_person_on_a_sofa_lies_by_shares_or_rhythm_and_sits_with_company(kwargs, extra, expected):
    got = _by_person(seats.settle(_rows(SOFA_AT) + extra, [SOFA], now=NOW, rooms=[ROOM], **kwargs))[P1]
    assert got["pose"] == expected
    if expected == "lie":
        assert (got["x"], got["z"]) == SOFA_AT and abs(math.cos(got["face"])) > 0.99           # вдоль длинной стороны
    duo = seats.settle(_rows(SOFA_AT, SOFA_AT), [SOFA], now=NOW, rhythm=ALWAYS)
    assert [r["pose"] for r in duo] == ["sit", "sit"]                                          # вдвоём всегда сидят


def test_bed_two_lie_along_and_the_third_sits_at_a_corner():
    """Двое лежат рядом (первый в очереди у стены, лицом к изголовью), третий или sit_only сидит на углу в ногах лицом к двери."""
    got = seats.settle(_rows(BED_AT, BED_AT, BED_AT), [BED])
    lying, sitting = [r for r in got if r["pose"] == "lie"], [r for r in got if r["pose"] == "sit"]
    assert len(lying) == 2 and len(sitting) == 1
    assert abs(sitting[0]["x"] - 0.8) > 0.39 and abs(sitting[0]["z"] - 1.0) > 0.7               # на углу, не посередине
    assert abs(lying[0]["x"] - lying[1]["x"]) >= 0.7                                            # рядом по ширине кровати

    rows = _rows(BED_AT, BED_AT, BED_AT)                                                         # порядок строк не совпадает с очередью жильцов
    got = _by_person(seats.settle(rows, [BED], front=lambda obj: 0.0, now=NOW, genders={P3: "male", P2: "female", P1: "male"},
                                  order={P3: 0, P2: 1, P1: 2}, door_at=lambda x, z: 1.234))      # свободно в +x: стена с -x
    assert got[P3]["pose"] == got[P2]["pose"] == "lie" and got[P1]["pose"] == "sit"
    assert got[P3]["x"] < got[P2]["x"] - 0.7                                                     # первый в очереди у стены (-x)
    assert abs(got[P2]["z"] - 1.0) < 1e-9 and abs(got[P3]["z"] - 1.0) < 1e-9                     # лежат вдоль
    assert got[P1]["z"] > 1.0 + 0.7 and abs(got[P1]["x"] - got[P2]["x"]) < 1e-9                  # на краю в ногах у соседа, что не у стены
    assert got[P1]["face"] == 1.234 and abs(math.cos(got[P3]["face"])) < 1e-9 and got[P3]["face"] < 0     # к двери; лицом к изголовью

    pair = _rows(BED_AT, BED_AT)
    only_sits = _by_person(seats.settle(pair, [BED], now=NOW, order={P1: 0, P2: 1}, sit_only={P1}, door_at=lambda x, z: 0.5))
    assert only_sits[P2]["pose"] == "lie" and only_sits[P1]["pose"] == "sit"                     # первый в очереди, но только сидит
    alone = seats.settle(pair[:1], [BED], now=NOW, order={P1: 0}, sit_only={P1}, door_at=lambda x, z: 0.5)[0]
    assert alone["pose"] == "sit" and abs(alone["z"] - 1.8) < 1e-9 and abs(alone["x"] - 0.8) > 0.5     # один тоже сидит: на углу в ногах


def test_person_off_furniture_or_walking_is_left_alone():
    """Вдали от мебели и на ходу поза и место не трогаются, один на кровати лежит."""
    far = seats.settle(_rows((9.0, 9.0)), [SOFA, BED])
    assert far[0]["pose"] == "stand" and far[0]["x"] == 9.0
    walking = [{"person": P1, "x": 5.0, "z": 1.45, "pose": "stand", "walking": True}]
    assert seats.settle(walking, [SOFA])[0]["pose"] == "stand"
    assert seats.settle(_rows(BED_AT), [BED])[0]["pose"] == "lie"


def test_bed_pair_hugs_by_rhythm_and_never_with_a_third():
    """Пара иногда сближается, обнимающий на стороне партнёра; hug_always - для настройки; с третьим никто не обнимается."""
    def settle(rows, rhythm, **kw):
        return _by_person(seats.settle(rows, [BED], now=NOW, order={P1: 0, P2: 1, P3: 2}, hugs={P1: P2}, rhythm=rhythm, **kw))
    pair = _rows(BED_AT, BED_AT)
    apart, close = settle(pair, ALWAYS), settle(pair, NEVER)                                     # ритм выпал - порознь
    gap = lambda rs: math.hypot(rs[P1]["x"] - rs[P2]["x"], rs[P1]["z"] - rs[P2]["z"])
    assert "hug" not in apart[P1] and abs(gap(apart) - gap(close) - seats.HUG_CLOSER_M) < 1e-6 and gap(close) >= seats.HUG_MIN_GAP_M
    assert (apart[P2]["x"], apart[P2]["z"]) == (close[P2]["x"], close[P2]["z"])                  # партнёр не сдвинулся
    assert close[P1]["hug"] == P2 and close[P1]["hug_side"] in (1, -1) and "hug" not in close[P2]
    assert [r["pose"] for r in close.values()] == ["lie", "lie"]
    assert settle(pair, ALWAYS, hug_always=True)[P1]["hug"] == P2
    assert not any("hug" in r for r in settle(_rows(BED_AT, BED_AT, BED_AT), NEVER, hug_always=True).values())


@pytest.mark.parametrize("door, face", [(math.pi / 2, math.pi / 2), (-math.pi / 2, -math.pi / 2)], ids=["door-south", "door-north"])
def test_lone_person_lies_in_the_tub_head_to_the_door(door, face):
    """Один у ванны, когда ритм выпал, лежит в центре вдоль, головой к двери; ритм не выпал - стоит; двое стоят."""
    got = seats.settle(_rows(TUB_AT), [TUB], now=NOW, door_at=lambda x, z: door, rhythm=ALWAYS)[0]
    assert (got["pose"], got["x"], got["z"]) == ("lie", 0.35, 0.75) and abs(got["face"] - face) < 1e-9
    assert seats.settle(_rows(TUB_AT), [TUB], now=NOW, rhythm=NEVER)[0]["pose"] == "stand"
    assert [r["pose"] for r in seats.settle(_rows(TUB_AT, (0.6, 1.0)), [TUB], now=NOW, rhythm=ALWAYS)] == ["stand", "stand"]


def _toilet_face(obj, along_long=False):
    return math.pi / 2 if along_long else 0.0                                                    # лицом на юг, ко входу


@pytest.mark.parametrize("gender, rhythm, pose", [("female", NEVER, "sit"), ("male", ALWAYS, "sit"), ("male", NEVER, "stand")])
def test_toilet_pose_by_gender_and_capacity_one(gender, rhythm, pose):
    """Девочка сидит лицом ко входу, мальчик сидит, когда ритм выпал, иначе стоит перед чашей лицом к стене; двое - оба стоят."""
    got = seats.settle(_rows(TOILET_AT), [TOILET], front=_toilet_face, now=NOW, genders={P1: gender}, rhythm=rhythm)[0]
    assert got["pose"] == pose
    if pose == "sit":
        assert (round(got["x"], 2), round(got["z"], 2)) == (0.2, 0.33) and abs(got["face"] - math.pi / 2) < 1e-9
    else:
        assert abs(got["face"] - (math.pi / 2 + math.pi)) < 1e-9 and got["z"] > 0.33
    two = seats.settle(_rows(TOILET_AT, (0.1, 0.4)), [TOILET], front=_toilet_face, now=NOW, genders={P1: gender}, rhythm=rhythm)
    assert [r["pose"] for r in two] == ["stand", "stand"]


def test_chair_seats_one_owner_first_others_stand():
    """На стуле один (хозяин рабочего места, иначе первый в очереди), остальные стоят; сидящий без опоры встаёт, на допуске края сидит."""
    rows = _rows((1.25, 1.25), (1.25, 1.25), (1.25, 1.25), pose="sit")
    got = {r["person"]: r["pose"] for r in seats.settle(rows, [CHAIR], now=NOW, order={P3: 0, P1: 1, P2: 2})}
    assert got == {P3: "sit", P1: "stand", P2: "stand"}
    far = rows[:1] + [{"person": P4, "x": 5.0, "z": 5.0, "pose": "sit"}]
    assert [r["pose"] for r in seats.settle(far, [CHAIR], now=NOW)] == ["sit", "sit"]             # вдали стул никого не отнимает
    owned = {r["person"]: r["pose"] for r in seats.settle(rows[:2], [CHAIR], order={P1: 0, P2: 1}, chair_owners=[(1.25, 1.25, P2)])}
    assert owned == {P1: "stand", P2: "sit"}                                                      # без хозяина сел бы первый по очереди
    pushed = [{"person": P1, "x": 1.25, "z": 1.25, "pose": "sit"}, {"person": P2, "x": 6.0, "z": 0.6, "pose": "sit"}, {"person": P3, "x": 6.0, "z": 0.6, "pose": "lie"}]
    assert {r["person"]: r["pose"] for r in seats.ground_poses(pushed, [CHAIR, dict(SOFA, x=20.0)])} == {P1: "sit", P2: "stand", P3: "stand"}
    assert seats.ground_poses([{"person": P4, "x": 1.7, "z": 1.25, "pose": "sit"}], [CHAIR])[0]["pose"] == "sit"


@pytest.mark.parametrize("objects, at, kwargs, period, every, branch", [
    ([SOFA], SOFA_AT, {}, seats.SOFA_LIE_PERIOD_S, seats.SOFA_LIE_EVERY, "lie"),
    ([TUB], TUB_AT, {}, seats.TUB_PERIOD_S, seats.TUB_EVERY, "lie"),
    ([TOILET], TOILET_AT, dict(front=_toilet_face, genders={P1: "male"}), seats.TOILET_SIT_PERIOD_S, seats.TOILET_SIT_EVERY, "sit"),
], ids=["sofa", "tub", "toilet"])
def test_default_rhythm_picks_one_period_in_every(objects, at, kwargs, period, every, branch):
    """Ритм по умолчанию: из every периодов подряд ровно один выпадает, остальные - обычная поза."""
    poses = [seats.settle(_rows(at), objects, now=k * period, **kwargs)[0]["pose"] for k in range(every)]
    assert poses.count(branch) == 1
