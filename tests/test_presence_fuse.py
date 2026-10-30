"""Слияние свидетельств и канал сообщений радара (SAM-105): вес, затухание, независимость каналов, место и «что рядом»."""
import pytest

from radar import presence_fuse as fz
from radar import presence_messages as pm

NOW = 10_000.0


def ev(source="messages", person="p1", weight=0.6, age=0.0, **kw):
    return fz.Evidence(source=source, person=person, weight=weight, t=NOW - age, **kw)


@pytest.mark.parametrize("evidence, confidence", [
    ([ev(weight=0.6)], 0.6),                                                         # одно свидетельство - его вес
    ([ev(source="messages", weight=0.6), ev(source="wifi", weight=0.5)], 1 - 0.4 * 0.5),   # независимые каналы складываются
    ([ev(weight=0.6), ev(weight=0.6, age=30), ev(weight=0.5, age=60)], 0.6),           # повтор одного канала не копится
    ([ev(weight=0.6, age=fz.HALF_LIFE_S)], 0.3),                                       # за полураспад вес вдвое
    ([ev(weight=0.6, age=20 * 60)], None),                                             # 20 минут: призрак ещё есть
    ([ev(weight=0.6, age=40 * 60)], 0.0),                                              # 40 минут: затухло
], ids=["one", "channels-add", "repeat", "half-life", "ghost-stays", "ghost-gone"])
def test_fusion_confidence_from_independent_channels_decays_with_time(evidence, confidence):
    estimates = fz.fuse(evidence, NOW)
    if confidence == 0.0:
        assert estimates == []
        return
    [est] = estimates
    assert est.person == "p1" and set(est.sources) == {e.source for e in evidence}
    if confidence is not None:
        assert est.confidence == pytest.approx(confidence)


@pytest.mark.parametrize("evidence, x, z, pose", [
    # одно место в пределах CLUSTER_M усредняется по весу, поза от сильнейшего
    ([ev(source="messages", weight=0.6, x=0.0, z=0.0, pose="sit"), ev(source="wifi", weight=0.2, x=1.0, z=0.5, pose="stand")], 0.25, 0.125, "sit"),
    # разные места не смешиваются, побеждает тяжелейшее
    ([ev(source="messages", weight=0.6, x=0.0, z=0.0, pose="sit"), ev(source="wifi", weight=0.2, x=4.0, z=2.0, pose="stand")], 0.0, 0.0, "sit"),
    # поза от выбранного места: у кровати два свидетельства тяжелее одного сильного у стола
    ([ev("wifi", weight=0.5, x=4.1, z=0.9, pose="lie"), ev("messages", weight=0.5, x=4.2, z=1.0), ev("pc", weight=0.7, x=8.35, z=4.0, pose="sit")],
     4.15, 0.95, "lie"),
    # у выбранного места позы нет - её решат опоры, а не стол
    ([ev("wifi", weight=0.5, x=4.1, z=0.9), ev("messages", weight=0.4, x=4.2, z=1.0), ev("pc", weight=0.7, x=8.35, z=4.0, pose="sit")],
     4.15, 0.95, None),
    # человек известен, но не размещён
    ([ev(weight=0.4)], None, None, None),
], ids=["one-place", "two-places", "pose-of-chosen", "no-pose-at-chosen", "unplaced"])
def test_fusion_places_person_at_heaviest_place_with_its_pose(evidence, x, z, pose):
    [est] = fz.fuse(evidence, NOW)
    if x is None:
        assert est.x is None and est.z is None
    else:
        assert (est.x, est.z) == (pytest.approx(x, abs=0.01), pytest.approx(z, abs=0.01))
    assert est.pose == pose


def test_fusion_drops_anonymous_and_sorts_by_confidence():
    assert fz.fuse([ev(person=None, x=1.0, z=1.0)], NOW) == []
    est = fz.fuse([ev(person="p2", weight=0.3), ev(person="p1", weight=0.7)], NOW)
    assert [e.person for e in est] == ["p1", "p2"]


SPOTS = {"desk-pc": {"x": 7.9, "z": 4.5, "pose": "sit", "owner": "p1"}, "mic-kitchen": {"x": 10.5, "z": 7.0}}
OBJECTS = [{"title": "стол", "x": 7.5, "z": 4.2, "w": 1.2, "d": 0.6}, {"title": "шкаф", "x": 0.0, "z": 0.0, "w": 1.0, "d": 1.0}]


@pytest.mark.parametrize("message, people, expected", [
    # чат с компьютера хозяина: хозяин там, с позой и тем, что рядом
    (pm.Message(NOW, "chat", "desk-pc"), None, {"person": "p1", "x": 7.9, "z": 4.5, "pose": "sit", "weight": pm.CHAT_WEIGHT}),
    # голос узнан - человек с весом узнанного голоса
    (pm.Message(NOW, "voice", "mic-kitchen", who="Speaker"), {"speaker": "p2"}, {"person": "p2", "weight": pm.VOICE_KNOWN_WEIGHT}),
    # голос не узнан - без человека и с меньшим весом
    (pm.Message(NOW, "voice", "mic-kitchen"), {}, {"person": None, "weight": pm.VOICE_UNKNOWN_WEIGHT}),
    # устройство без известного места - свидетельства нет
    (pm.Message(NOW, "voice", "headset-x"), {}, None),
], ids=["chat-owner", "voice-known", "voice-unknown", "no-place"])
def test_message_from_device_becomes_owner_evidence(message, people, expected):
    kwargs = {"objects": OBJECTS} if people is None else {"people": people}        # people=None - чат, рядом проверяются предметы
    e = pm.evidence_from(message, SPOTS, **kwargs)
    if expected is None:
        assert e is None
        return
    for name, value in expected.items():
        assert getattr(e, name) == value, name
    if people is None:
        assert "стол" in e.note and "шкаф" not in e.note                      # в заметке - что рядом с устройством, далёкий шкаф нет


def test_message_channel_feeds_fusion():
    ch = pm.MessageChannel(SPOTS, objects=OBJECTS)
    ch.feed(pm.Message(NOW - 10, "chat", "DESK-PC mic"))
    ch.feed(pm.Message(NOW - 5, "voice", "headset-x"))
    [est] = fz.fuse(ch.evidence(NOW), NOW)
    assert est.person == "p1" and est.pose == "sit"
