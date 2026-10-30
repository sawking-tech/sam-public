"""Признаки линка станции: разбор вывода iw и строка для файла треков (SAM-118). Вывод взят с реального роутера, MAC выдуманный."""
import pytest

from radar import presence_link as pl
from radar import presence_rssi as rs
from radar.presence_tracks import TrackLog

DUMP = """Station aa:bb:cc:00:11:22 (on phy1-ap0)
\tinactive time:\t30 ms
\tsignal:  \t-84 [-85, -89] dBm
\tsignal avg:\t-78 [-79, -84] dBm
\ttx bitrate:\t432.3 MBit/s 80MHz HE-MCS 4 HE-NSS 2 HE-GI 0 HE-DCM 0
\trx bitrate:\t216.1 MBit/s 80MHz HE-MCS 4 HE-NSS 1 HE-GI 0 HE-DCM 0
\tlast ack signal:-74 dBm
\tavg ack signal: -71 dBm
"""
COUNTERS = "\trx bytes:\t1000\n\trx packets:\t10\n\ttx bytes:\t2000\n\ttx packets:\t20\n\ttx retries:\t3\n\ttx failed:\t1\n"
DUMP_ROW = {"avg": -78.0, "avgch": [-79.0, -84.0], "ack": [-74.0, -71.0], "tx": [432.3, 4, 2], "rx": [216.1, 4, 1]}


@pytest.mark.parametrize("body, fields, row", [
    # полный дамп
    (DUMP, {"avg_db": -78.0, "avg_chains": (-79.0, -84.0), "ack_last_db": -74.0, "ack_avg_db": -71.0,
            "tx": pl.LinkRate(432.3, 4, 2), "rx": pl.LinkRate(216.1, 4, 1), "counters": (None,) * 6}, DUMP_ROW),
    # недостающие поля - None, legacy MCS без потоков сохраняет mcs
    ("\tsignal: -60 [-60, -61] dBm\n\ttx bitrate: 130.0 MBit/s MCS 15 40MHz short GI\n",
     {"avg_db": None, "ack_last_db": None, "rx": pl.LinkRate(), "tx": pl.LinkRate(130.0, 15, None)}, {"tx": [130.0, 15, None]}),
    # в строке для файла только то, что есть
    ("\tsignal: -60 dBm\n", {}, {}),
    # счётчики трафика в постоянном порядке
    (DUMP + COUNTERS, {"counters": (1000, 2000, 10, 20, 3, 1)}, {**DUMP_ROW, "cnt": [1000, 2000, 10, 20, 3, 1]}),
    ("\ttx bytes: 5\n", {}, {"cnt": [None, 5, None, None, None, None]}),
])
def test_iw_station_body_parsed_into_link_row(body, fields, row):
    link = pl.parse_link(body)
    for name, value in fields.items():
        assert getattr(link, name) == value, name
    assert link.as_row() == row


def test_parsed_station_link_written_to_track_file(tmp_path):
    [station] = rs.parse_station_dump(DUMP, "phy1-ap0")
    assert station.link is not None and station.link.tx.mcs == 4 and station.signal_db == -87.0     # среднее цепочек прежнее
    log = TrackLog(tmp_path)
    assert log.record([station], 100.0) == 1
    line = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf8").strip()
    assert '"ln":{"avg":-78.0' in line and '"db":-87.0' in line
