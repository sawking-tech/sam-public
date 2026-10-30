"""Обезличенная выгрузка сырых треков: псевдонимы, относительное время, без имён и хэшей (SAM-118). Данные выдуманные."""
import json

import pytest

from tests.fakes_presence import load_script

mod = load_script("radar_tracks_export")
T0 = 1_000_000.0


def line(t, name, db, **extra):
    return json.dumps({"t": T0 + t, "id": "a1b2c3d4", "name": name, "db": db, "if": "phy1-ap0", **extra}, ensure_ascii=False)


ALIASED = [line(1, "Owner-Phone-Ultra", -60.0), line(2, "Guest-PC", -80.0), line(3, "Guest-PC", -81.0), line(4, "owner-phone-ultra", -61.0), line(5, None, -90.0)]
FILTERED = [line(-5, "x", -60.0), "не json", line(2, "x", -60.0, ln={"avg": -58.0}), line(20, "x", -60.0)]


@pytest.mark.parametrize("lines, aliases, expected", [
    (ALIASED, mod.parse_aliases(["OWNER=phone-a"]),
     [{"d": "phone-a", "t": 1.0}, {"d": "other-1", "t": 2.0}, {"d": "other-1", "t": 3.0}, {"d": "phone-a", "t": 4.0}, {"d": "other-2", "t": 5.0}]),
    (FILTERED, {}, [{"d": "other-1", "t": 2.0, "ln": {"avg": -58.0}}]),        # вне интервала и битые строки мимо, признаки линка остаются
], ids=["aliases", "interval_and_broken"])
def test_export_anonymises_relativises_and_filters(lines, aliases, expected):
    """Имена - псевдонимы (разбор пар без учёта регистра), неизвестные устройства - номера, время от начала интервала, id и дат нет."""
    rows = mod.export_rows(lines, T0, T0 + 10, aliases)
    assert [{k: r[k] for k in ("d", "t", "ln") if k in r} for r in rows] == expected
    text = json.dumps(rows, ensure_ascii=False)
    assert "Owner" not in text and "Guest" not in text and "a1b2c3d4" not in text and all("id" not in r for r in rows)
