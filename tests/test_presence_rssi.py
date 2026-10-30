"""RSSI-источник радара (SAM-105): разбор station dump и детектор дрожания сигнала. MAC в данных выдуманные, шум - с фиксированным сидом."""
import random

import pytest

from radar import presence_rssi as rs

DUMP = """Station aa:bb:cc:00:00:01 (on phy1-ap0)
\tinactive time:\t200 ms
\tsignal:  \t-72 [-79, -73] dBm
\tsignal avg:\t-71 [-79, -72] dBm
\ttx bitrate:\t720.6 MBit/s 80MHz HE-MCS 7 HE-NSS 2
Station aa:bb:cc:00:00:02 (on phy1-ap0)
\tinactive time:\t21570 ms
\tsignal:  \t-74 [-78, -75] dBm
Station aa:bb:cc:00:00:03 (on phy1-ap0)
\tinactive time:\t100 ms
\tsignal:  \t-5 dBm
Station aa:bb:cc:00:00:04 (on phy1-ap0)
\tsignal:  \t-60 dBm
"""
MACS = ("aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02")


@pytest.mark.parametrize("text, expected", [
    (DUMP, [(-76.0, 200), (-76.5, 21570)]),          # средний по цепям: (-79-73)/2 и (-78-75)/2; уровень вне диапазона и строка без inactive отброшены
    ("", []),
    ("command failed", []),
], ids=["dump", "empty", "garbage"])
def test_station_dump_parses_to_anonymous_stations(text, expected):
    """Средний по цепям уровень, плохие строки и мусор отброшены, id скрывает MAC и стабилен."""
    got = rs.parse_station_dump(text, "phy1-ap0")
    assert [(s.signal_db, s.inactive_ms) for s in got] == expected
    assert all(s.iface == "phy1-ap0" for s in got)
    assert [s.sid for s in got] == [rs.station_id(mac) for mac in MACS[:len(got)]]
    sid = rs.station_id(MACS[0].upper())
    assert sid == rs.station_id(MACS[0]) and len(sid) == 8 and ":" not in sid and sid != rs.station_id(MACS[1])


def _station(sid="s1", level=-60.0, inactive=100) -> rs.Station:
    return rs.Station(sid, "phy1-ap0", level, inactive)


def _feed(detector, n, lines, rnd):
    """n опросов; lines - {sid: (уровень, разброс)}; возвращает последнее чтение."""
    reading = None
    for _ in range(n):
        reading = detector.update([_station(sid, level + rnd.gauss(0, sd)) for sid, (level, sd) in lines.items()])
    return reading


def test_motion_detector_reports_activity_per_active_station():
    """Ровный сигнал тихий, дрожание после покоя поднимает активность, по линиям раздельно, молчащая станция не считается, база спускается за спокойной комнатой."""
    rnd = random.Random(11)
    steady = rs.MotionDetector()
    assert steady.update([_station()]).stations_active == 0                                    # окно ещё короткое
    reading = _feed(steady, 30, {"s1": (-60.0, 0.0)}, rnd)
    assert reading.activity == 0.0 and reading.stations_active == 1

    lines = rs.MotionDetector()
    _feed(lines, 60, {"calm": (-50.0, 0.2), "shaky": (-70.0, 0.2)}, rnd)                         # спокойная комната
    reading = _feed(lines, 25, {"calm": (-50.0, 0.2), "shaky": (-70.0, 3.5)}, rnd)               # человек ходит через линию shaky
    assert reading.per_station["shaky"] > 0.5 and reading.per_station["calm"] < 0.2
    assert reading.activity == max(reading.per_station.values())

    silent = rs.MotionDetector()
    for i in range(30):
        reading = silent.update([_station(level=-60 + (i % 2) * 8, inactive=rs.ACTIVE_MS + 1)])
    assert reading.activity == 0.0 and reading.stations_active == 0 and reading.stations_total == 1

    calming = rs.MotionDetector()
    noisy_base = _feed(calming, 40, {"s1": (-60.0, 2.0)}, rnd).quiet_db                             # шумное начало: база высокая
    assert _feed(calming, 200, {"s1": (-60.0, 0.1)}, rnd).quiet_db < noisy_base


@pytest.mark.parametrize("seen_before_leaving", [1, rs.MIN_SAMPLES + 4], ids=["before-baseline", "with-baseline"])
def test_detector_forgets_a_departed_station(seen_before_leaving):
    """Ушедшая станция теряет окно и базу, в том числе до накопления базы; вернувшись, копит окно заново, детектор не падает."""
    detector = rs.MotionDetector()
    for _ in range(seen_before_leaving):
        detector.update([_station()])
    gone = detector.update([])
    assert (gone.stations_total, gone.stations_active, gone.quiet_db, gone.per_station) == (0, 0, 0.0, {})
    assert detector.update([_station()]).stations_active == 0                                   # окно с нуля: снова короткое
