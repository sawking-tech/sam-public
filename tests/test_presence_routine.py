"""Автопилот «быт» (SAM-105): сцены по кругу из правдоподобных продолжений, вылазки в туалет, уход и приход гостя. Случайность с сидом, план выдуманный."""
import asyncio
import json
import random

import pytest

from radar import presence_hub as ph
from radar import presence_nav as pnav
from radar import presence_routine as pr
from radar.presence_random import Randomness
from tests.fakes_presence import routine_layout

TRIPS = {place for place, _ in pr.TRIP_PLACES}


def _run(seed=1, seconds=3000, step=2.0, **kwargs):
    """Журнал смен (время, сцена, назначения) за seconds секунд опроса раз в step."""
    routine = pr.Routine(rng=random.Random(seed), **kwargs)
    log, now = [], 0.0
    while now < seconds:
        change = routine.step(now)
        if change:
            log.append((now, routine.scene, change))
        now += step
    return routine, log


def test_routine_starts_asleep_and_cycles_through_scenes():
    """Со сна проходит много сцен и снова засыпает; со случайным стартом день начинается в разных сценах без гостя."""
    _, log = _run()
    assert log[0][1] == "сон" and log[0][2] == {"a": "кровать", "b": "кровать", "guest": None}
    assert len({entry[1] for entry in log}) >= 7                                       # за 50 минут почти все сцены: цикл, а не одна дорожка
    assert sum(1 for e in log if e[1] == "сон") >= 2                                    # и снова ложатся спать: круг замкнут
    firsts = set()
    for seed in range(40):
        r = pr.Routine(rng=random.Random(seed), random_start=True)
        r.step(0.0)
        firsts.add(r.scene)
    assert len(firsts) >= 3 and firsts <= set(pr.START_SCENES)


def test_default_scenes_are_consistent():
    """Переходы ведут в существующие сцены, dwell упорядочен, на чужом компьютере никто не сидит, разбор чужих мест работает."""
    for name, scene in pr.DEFAULT_SCENES.items():
        assert scene["dwell_s"][0] <= scene["dwell_s"][1]
        for target, weight in scene["next"]:
            assert target in pr.DEFAULT_SCENES and weight > 0, (name, target)
    assert pr.foreign_seats(pr.DEFAULT_SCENES) == []
    assert pr.foreign_seats({"x": {"assign": {"b": "pc-a", "guest": "pc-b"}}}) == [("x", "b", "pc-a"), ("x", "guest", "pc-b")]
    assert pr.DEFAULT_SCENES["вдвоём за одним компьютером"]["assign"] == {"a": "pc-b", "b": "pc-b"}


@pytest.mark.parametrize("seed, seconds", [(3, 4000), (7, 20000)])
def test_guest_arrives_and_leaves_only_through_the_entrance(seed, seconds):
    """Гость появляется только в сцене прихода и уходит от входа или из комнаты; у входа никто не тусуется."""
    _, log = _run(seed=seed, seconds=seconds)
    guest = [(scene, change["guest"]) for _, scene, change in log if "guest" in change]
    assert "вход" in [place for _, place in guest] and None in [place for _, place in guest]
    present, last = False, None
    for scene, place in guest:
        if place is not None and not present:
            assert scene == "гость пришёл", scene                                      # от входа его ведёт ходьба
        if place is None and present:
            assert last in ("вход", "диван", "кровать")                                  # ушёл от входа или из комнаты, не из воздуха
        present, last = place is not None, place
    assert pr.DEFAULT_SCENES["гость уходит"]["dwell_s"][1] <= 20                        # у входа стоит секунды: дошёл и пропал
    others = {p for n, sc in pr.DEFAULT_SCENES.items() if n != "гость уходит" for p in sc["assign"].values()}
    assert "вход" not in others


def test_trips_to_the_toilet_return_to_the_scene_and_sleepers_stay(tmp_path):
    """Вылазки в туалет случаются и возвращают на место, спящих не шлют, битый файл сцен - сцены по умолчанию."""
    _, log = _run(seed=5, seconds=6000)
    trips = [(t, person) for t, _, change in log for person, place in change.items() if place in TRIPS]
    assert trips
    for t, person in trips:
        back = next((c[person] for t2, _, c in log if t2 > t and person in c), None)
        assert back not in TRIPS                                                        # возвращается на своё место или сцена сменилась
    in_bed = {}
    for _, scene, change in log:
        sent = [person for person, place in change.items() if place in TRIPS]
        assert not any(in_bed.get(person) == "кровать" and scene == "сон" for person in sent)     # спящего в туалет не шлют
        in_bed.update({p: v for p, v in change.items() if v not in TRIPS})
    bad = tmp_path / "routine.json"
    bad.write_text("не json", encoding="utf-8")
    assert pr.load_scenes(bad) is pr.DEFAULT_SCENES and pr.load_scenes(None) is pr.DEFAULT_SCENES


FRAMES = 1500                 # кадров на прогон: при шаге часов 2 с - 50 минут быта, несколько сцен и вылазок
CLOCK_STEP_S = 2.0
START = 1000.0


async def _identities_over_routine(hub, now):
    """Цикл хаба с одним клиентом: на каждый кадр часы сдвигаются на шаг; тик 0 - кадр приходит сразу, без настенного времени."""
    queue = hub.add_client()
    task = asyncio.create_task(hub.run())
    frames = []
    for _ in range(FRAMES):
        frames.append(json.loads(await queue.get())["identities"])
        now[0] += CLOCK_STEP_S
    task.cancel()
    return frames


@pytest.mark.parametrize("seed", [1, 2])
def test_routine_places_stay_on_free_floor_of_the_plan(monkeypatch, tmp_path, seed):
    """Все места быта есть в плане, и за весь быт никто не стоит внутри мебели или стены."""
    monkeypatch.setenv(pr.ROUTINE_FILE_ENV, str(tmp_path / "no-routine.json"))         # встроенные сцены, не личный быт из data/
    layout = routine_layout()
    now = [START]
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, tick_s=0.0, clock=lambda: now[0], randomness=Randomness(seed))
    used = {place for scene in pr.DEFAULT_SCENES.values() for place in list(scene["assign"].values()) + list((scene.get("then") or {}).values()) if place}
    assert used | TRIPS <= set(hub.place_points()), (used | TRIPS) - set(hub.place_points())
    hub.set_autopilot(True, mode="routine")
    frames = asyncio.run(_identities_over_routine(hub, now))
    nav = pnav.Nav(layout)
    standing = [(r["x"], r["z"]) for rows in frames for r in rows if r.get("x") is not None and r.get("pose") == "stand"]
    assert standing and len({r["person"] for rows in frames for r in rows}) >= 2
    assert [p for p in standing if not nav.segment_free(p, p)] == []
