"""Нарезка проверочных путей радиуса: обезличивание, концы путей, стоянки (SAM-118). Данные выдуманные, треки и план - в tmp_path."""
import json
from pathlib import Path

import pytest

from tests.fakes_presence import load_script

mod = load_script("radar_walk_dataset")
DAY = "2026-01-02"
T0 = mod.clock_to_ts(DAY, "10:00:00")


def write_tracks(path: Path, levels: dict[str, list[float]]) -> None:
    rows = []
    for name, series in levels.items():
        rows += [json.dumps({"t": T0 + i + 0.2, "name": name, "db": db, "if": "phy1-ap0"}) for i, db in enumerate(series)]
    path.write_text("\n".join(rows), encoding="utf8")


def timeline(tracks: Path) -> dict:
    return {"date": DAY, "from": "10:00:00", "to": "10:01:19", "tracks_file": str(tracks), "target": "phone-a", "reference": "phone-b", "iface": "phy1-ap0",
            "band": "5GHz", "router": {"x": 1.0, "z": 1.0},
            "places": {"Кухня": {"x": 5.0, "z": 5.0}, "Спальня": {"x": 0.0, "z": 0.0}},
            "stands": [{"place": "Кухня", "from": "10:00:00", "to": "10:00:30"}, {"place": "Спальня", "from": "10:01:00", "to": "10:01:19"}],
            "legs": [{"from": "Кухня", "to": "Спальня", "start": "10:00:35", "end": "10:00:55", "pace": "slow", "router_pass": True}]}


@pytest.fixture
def walk(tmp_path):
    """Один проход кухня -> спальня через роутер: подъём уровня к пику -30 на секунде 45 и спад; спальня с комнатами из плана."""
    rise = [-70.0 + 4.0 * i for i in range(1, 11)]
    fall = [-30.0 - 5.0 * i for i in range(1, 11)]
    write_tracks(tmp_path / "t.jsonl", {"phone-a": [-70.0] * 35 + rise + fall + [-80.0] * 25, "phone-b": [-65.0] * 80})
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps({"rooms": [{"title": "кухня", "x": 4.0, "z": 4.0, "w": 3.0, "d": 3.0}, {"title": "спальня", "x": -1.0, "z": -1.0, "w": 3.0, "d": 3.0}]}), encoding="utf8")
    return mod.build({**timeline(tmp_path / "t.jsonl"), "layout": str(layout)})


def test_walk_dataset_is_anonymous_and_labels_every_second(walk):
    """Места - P01, P02, комнаты - R01, R02 из плана, время относительное, имён, устройств и даты нет;
    каждая секунда помечена стоянкой с местом или ходом, остальные пусты."""
    text = json.dumps(walk, ensure_ascii=False)
    assert set(walk["places"]) == {"P01", "P02"} and (walk["places"]["P01"]["room"], walk["places"]["P02"]["room"]) == ("R01", "R02")
    assert all(word not in text for word in ("Кухня", "Спальня", "кухня", "спальня", "phone-a", DAY))
    assert walk["meta"]["seconds"] == 80 and len(walk["series"]["target"]) == 80
    labels = walk["labels"]
    assert len(labels["kind"]) == 80 and (labels["kind"][10], labels["place"][10]) == ("stand", "P01")
    assert (labels["kind"][40], labels["place"][40], labels["leg"][40]) == ("walk", None, 0)
    assert labels["kind"][32] is None                                                        # между стоянкой и ходом - без метки


def test_walk_dataset_cuts_legs_peaks_stands_landmarks(walk, tmp_path):
    """Ход с проходом роутера режется целиком и по пику уровня; стоянки - неподвижные куски заданных длин; id уникальны и по порядку;
    ориентир режет ход по отметке минус запаздывание и помечен приближённым."""
    leg = [c for c in walk["cuts"] if c["moving"] and not c.get("chain")]
    whole = max(leg, key=lambda c: c["length_s"])
    assert (whole["start"]["place"], whole["end"]["place"], whole["length_s"]) == ("P01", "P02", 20)
    peak = [c for c in leg if c["end"]["kind"] == "router_peak"]
    assert len(peak) == 1 and peak[0]["t1"] == 44 and peak[0]["end"]["x"] == 1.0 and peak[0]["end"]["sd_m"] == mod.PEAK_SD_M
    still = [c for c in walk["cuts"] if not c["moving"]]
    assert still and all(c["start"] == c["end"] and c["t1"] - c["t0"] in mod.STAND_CUT_LENGTHS_S for c in still)
    ids = [c["id"] for c in walk["cuts"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)

    tl = {**timeline(tmp_path / "flat.jsonl"), "mark_lag_s": 4}
    tl["legs"] = [{**tl["legs"][0], "landmarks": [{"clock": "10:00:46", "x": 2.0, "z": 3.0, "sd_m": 1.5}]}]
    write_tracks(tmp_path / "flat.jsonl", {"phone-a": [-70.0] * 80, "phone-b": [-70.0] * 80})
    data = mod.build(tl)
    approx = [c for c in data["cuts"] if not c["exact"]]
    assert {(c["t0"], c["t1"]) for c in approx} == {(35, 42), (42, 55)}                  # отметка 46 минус запаздывание 4
    assert next(c for c in approx if c["t0"] == 35)["end"] == {"place": None, "kind": "landmark", "x": 2.0, "z": 3.0, "sd_m": 1.5, "t_sd_s": mod.MARK_TIME_SD_S}
    assert all(c["exact"] for c in data["cuts"] if "landmark" not in (c["end"]["kind"], c["start"]["kind"]))


def _splice_timeline(tmp_path):
    tl = {**timeline(tmp_path / "t3.jsonl"), "to": "10:03:00"}
    tl["stands"] = [{"place": "Кухня", "from": "10:00:00", "to": "10:00:30"}, {"place": "Спальня", "from": "10:01:00", "to": "10:01:20"},
                    {"place": "Кухня", "from": "10:02:00", "to": "10:02:20"}]
    tl["legs"] = [{"from": "Кухня", "to": "Спальня", "start": "10:00:35", "end": "10:00:55", "pace": "slow"},
                  {"from": "Спальня", "to": "Кухня", "start": "10:01:25", "end": "10:01:55", "pace": "slow"},
                  {"from": "Кухня", "to": "Спальня", "start": "10:02:25", "end": "10:02:45", "pace": "slow"}]
    series = [-70.0] * 60 + [-75.0] * 60 + [-72.0] * 60
    write_tracks(tmp_path / "t3.jsonl", {"phone-a": series, "phone-b": series})
    return mod.build(tl)


def _pad(walk, tmp_path):
    padded = [a for a in walk["augmented"] if a["augment"] == "pad"]
    assert padded and all(a["exact"] and a["start"]["kind"] == "stand" and a["parent"].startswith("C") for a in padded)
    assert all(a["t0"] >= 0 and a["t1"] <= 79 and a["length_s"] == a["t1"] - a["t0"] for a in padded)


def _offset(walk, tmp_path):
    assert {a["offset_db"] for a in walk["augmented"] if a["augment"] == "offset"} == set(mod.OFFSETS_DB)


def _reverse(walk, tmp_path):
    back = [a for a in walk["augmented"] if a["augment"] == "reverse"]
    assert back and all(a["hypothesis"] for a in back) and (back[0]["start"]["place"], back[0]["end"]["place"]) == ("P02", "P01")


def _splice(walk, tmp_path):
    joins = [a for a in _splice_timeline(tmp_path)["augmented"] if a["augment"] == "splice"]
    assert [(j["legs"], j["start"]["place"], j["end"]["place"]) for j in joins] == [([1, 0], "P02", "P02")]       # ход 2 не склеивается: после него нет стоянки
    assert joins[0]["parts"][1]["offset_db"] == -2.0                                                                  # кухня в конце тише на 2 дБ


@pytest.mark.parametrize("augment", [_pad, _offset, _reverse, _splice], ids=["pad", "offset", "reverse", "splice"])
def test_walk_dataset_augments(walk, tmp_path, augment):
    """Расширения: отступ в соседние стоянки с концами и родителем, сдвиг уровня, обратный ход с переставленными концами, склейка двух ходов через одно место."""
    augment(walk, tmp_path)
