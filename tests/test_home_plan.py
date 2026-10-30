"""План квартиры из Sweet Home 3D (SAM-105): уровни, сантиметры в метры, угол в нуль, мелочь отброшена. XML выдуманный."""
import io
import json
import zipfile

import pytest

from gateway import home_plan as hp

XML = """<home version="7400" unit="cm">
  <level id="L0" name="Level 0" elevation="0"/>
  <level id="L3" name="Level 3" elevation="786"/>
  <wall level="L0" xStart="0" yStart="0" xEnd="400" yEnd="0" thickness="20" height="250"/>
  <wall level="L3" xStart="1400" yStart="100" xEnd="1800" yEnd="100" thickness="20" height="250"/>
  <wall level="L3" xStart="1800" yStart="100" xEnd="1800" yEnd="500" thickness="20"/>
  <pieceOfFurniture level="L3" name="Sofa" x="1600" y="300" width="200" depth="100" height="85" angle="1.5708"/>
  <pieceOfFurniture level="L3" name="Plate" x="1500" y="200" width="20" depth="20" height="2"/>
  <pieceOfFurniture level="L3" name="Мужчина" x="1500" y="250" width="50" depth="40" height="180"/>
  <pieceOfFurniture level="L3" name="Chair" x="1500" y="260" width="10" depth="10" height="90"/>
  <doorOrWindow level="L3" name="Window" x="1800" y="300" width="120" depth="20" height="150"/>
</home>""".encode("utf-8")


def test_sh3d_level_becomes_radar_layout():
    layout = hp.parse_home(XML, "Level 3")
    assert layout["name"] == "sh3d:Level 3" and len(layout["walls"]) == 2
    first = layout["walls"][0]
    assert (first["x1"], first["z1"], first["x2"], first["z2"], first["t"], first["h"]) == (0.0, 0.0, 4.0, 0.0, 0.2, 2.5)
    room = layout["rooms"][0]
    assert (room["x"], room["z"], room["w"], room["d"]) == (0.0, 0.0, 4.0, 4.0)
    objs = {o["title"]: o for o in layout["objects"]}
    assert list(objs) == ["Sofa", "Window"]                             # тарелка (мелкая), фигурка мужчины и стул 10x10 не ориентиры
    sofa = objs["Sofa"]
    assert (sofa["x"], sofa["z"], sofa["w"], sofa["d"]) == (1.0, 1.5, 2.0, 1.0)     # x: 16 м - 1 м - угол 14 м; z: 3 м - 0,5 м - угол 1 м
    assert sofa["kind"] == "furniture" and objs["Window"]["kind"] == "opening"
    assert sofa["angle"] == pytest.approx(1.5708) and objs["Window"]["angle"] == 0.0
    assert hp.parse_home(XML)["name"] == "sh3d:Level 0"                 # уровень по умолчанию - первый
    assert len(hp.parse_home(XML, "no such level")["walls"]) == 1       # неизвестный уровень - первый


def test_bad_project_says_what_to_fix(tmp_path):
    with pytest.raises(ValueError, match="Empty"):
        hp.parse_home(b'<home><level id="A" name="Empty"/></home>', "Empty")
    good, bad = tmp_path / "good.sh3d", tmp_path / "bad.sh3d"
    for path, files in ((good, {"Home.xml": XML}), (bad, {"Home": b"binary"})):
        with zipfile.ZipFile(path, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
    assert hp.read_sh3d(good, "Level 3")["levels"] == ["Level 0", "Level 3"]
    with pytest.raises(ValueError, match="Home.xml"):
        hp.read_sh3d(bad)


def test_layout_file_loads_in_radar(tmp_path):
    from radar import presence as pr
    out = tmp_path / "sub" / "layout.json"
    hp.write_layout(hp.parse_home(XML, "Level 3"), out)
    assert json.loads(out.read_text(encoding="utf-8"))["walls"]
    assert pr.load_layout(str(out))["name"] == "sh3d:Level 3"


@pytest.mark.parametrize("parse, item, expected", [
    (hp.parse_object, "Диван (примерно)=10.7,5.3,2.0,0.9,0.87,180",
     {"approx": True, "kind": "furniture", "x": 9.7, "z": 4.85, "angle": pytest.approx(3.14159, abs=1e-4)}),
    (hp.parse_place, "кресло Ирины=10.4,1.0", {"label": "кресло Ирины", "x": 10.4, "z": 1.0}),
    (hp.parse_object, "Диван=1,2,3", "--object"),
    (hp.parse_object, "=1,2,3,4,5", "--object"),
    (hp.parse_object, "Диван=a,b,c,d,e", "--object"),
    (hp.parse_object, "Диван=1,2,3,4,5,6,7", "--object"),
    (hp.parse_place, "кухня=1", "--place"),
    (hp.parse_place, "=1,2", "--place"),
    (hp.parse_place, "кухня=a,b", "--place"),
], ids=["object_approx_corner_form", "place_label_at_point", "object_too_few", "object_no_name",
        "object_not_numbers", "object_too_many", "place_too_few", "place_no_name", "place_not_numbers"])
def test_object_and_place_from_words(parse, item, expected):
    if isinstance(expected, str):                                       # битый ввод: сообщение называет ключ
        with pytest.raises(ValueError, match=expected):
            parse(item)
        return
    got = parse(item)
    assert {k: got[k] for k in expected} == expected
