"""Фильтр по времени для места устройства: достижимость, забывание, один отсчёт один раз (SAM-118)."""
import math

from radar import presence_fingerprint as pf
from radar import presence_transition as pt

LINE = {"кухня": 0.0, "коридор": 3.0, "кабинет": 6.0, "спальня": 14.0}      # места на прямой, метры
AT_KITCHEN = {"кухня": 1.0, "коридор": 0.0, "кабинет": 0.0, "спальня": 0.0}


def dist(a: str, b: str) -> float:
    return abs(LINE[a] - LINE[b])


def fingerprints(levels: dict[str, tuple[float, ...]]) -> pf.Fingerprints:
    fp = pf.Fingerprints()
    for place, dbs in levels.items():
        for db in dbs:
            fp.add("phone", place, db)
    return fp


def test_prediction_spreads_to_reachable_places_and_never_closes_any():
    """Сумма остаётся единицей, за секунду масса почти вся на месте, за три ближние места получают больше дальних, ни одно не закрыто."""
    second = pt.predict(AT_KITCHEN, dist, 1.0)
    assert abs(sum(second.values()) - 1.0) < 1e-9 and second["кухня"] > 0.9
    assert min(second.values()) >= pt.FLOOR_SHARE / len(second) - 1e-12
    later = pt.predict(AT_KITCHEN, dist, 3.0)
    assert later["коридор"] > later["кабинет"] > later["спальня"]


def test_place_tracker_prior_lifecycle():
    """Первый отсчёт без прошлого, следующий с прошлым как априором; тот же отсчёт считается один раз; давнее забыто."""
    tracker, priors = pt.PlaceTracker(), []

    def likelihood(prior, power):
        priors.append(prior)
        return {"кухня": 0.9, "коридор": 0.1, "кабинет": 0.0, "спальня": 0.0}

    first = tracker.update(10.0, dist, likelihood)
    assert tracker.update(10.0, dist, lambda prior, power: {"спальня": 1.0}) is first and len(priors) == 1
    tracker.update(11.0, dist, likelihood)
    assert priors[0] is None and priors[1] is not None and priors[1]["кухня"] > priors[1]["спальня"]
    tracker.update(11.0 + pt.FORGET_AFTER_S + 1.0, dist, likelihood)
    assert priors[2] is None
    assert math.isinf(pt.PlaceTracker().observed_at)


def test_tracker_with_fingerprints_slight_edge_stays_uncertain_and_told_place_leads():
    """С настоящими отпечатками небольшое преимущество, повторённое каждую секунду, не растёт в уверенность;
    место, названное человеком, ведёт оценку против двусмысленного уровня."""
    fp = fingerprints({"вход": (-59.0, -60.0, -61.0, -59.5), "стол": (-62.0, -63.0, -61.5, -62.5)})
    tracker, probs = pt.PlaceTracker(), {}
    for second in range(30):
        probs = tracker.update(float(second), lambda a, b: 3.0, lambda prior, power: fp.place_probabilities("phone", -60.5, prior, power=power))
    assert probs["вход"] < 0.9 and probs["стол"] > 0.1                          # без ослабления тридцать отсчётов давали около 99%

    fp = fingerprints({"вход": (-59.0, -60.0, -61.0), "стол": (-60.0, -61.0, -62.0)})
    tracker = pt.PlaceTracker()
    tracker.set_known("стол", ["вход", "стол"], 10.0)
    probs = tracker.update(11.0, lambda a, b: 3.0, lambda prior, power: fp.place_probabilities("phone", -59.5, prior, power=power))   # уровень чуть ближе к входу
    assert max(probs, key=probs.get) == "стол" and probs["стол"] > 0.8
