"""Карта покрытия сети (SAM-118): сетка уровней по плану, полосы качества, места по убыванию уровня и маршрут радара."""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from radar import presence_api
from radar import presence_coverage as pcov
from radar import presence_hub as ph

LAYOUT = {"name": "t", "router": {"x": 1.0, "z": 1.0},
          "walls": [{"x1": 0.0, "z1": 0.0, "x2": 6.0, "z2": 0.0, "t": 0.2}, {"x1": 6.0, "z1": 0.0, "x2": 6.0, "z2": 4.0, "t": 0.2},
                    {"x1": 6.0, "z1": 4.0, "x2": 0.0, "z2": 4.0, "t": 0.2}, {"x1": 0.0, "z1": 4.0, "x2": 0.0, "z2": 0.0, "t": 0.2},
                    {"x1": 3.0, "z1": 0.0, "x2": 3.0, "z2": 4.0, "t": 0.1}],
          "places": [{"label": "рядом", "x": 2.0, "z": 1.0}, {"label": "за стеной", "x": 5.0, "z": 1.0}]}
MODELS = {"phy1-ap0": {"iface": "phy1-ap0", "band": "5 ГГц", "l0_db": -45.0, "n": 2.0, "wall_db_per_m": 50.0},
          "phy0-ap0": {"iface": "phy0-ap0", "band": "2,4 ГГц", "l0_db": -35.0, "n": 2.0, "wall_db_per_m": 30.0}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(presence_api, "RADIO_MODEL_FILE", tmp_path / "model.json")
    return TestClient(presence_api.create_app(ph.PresenceHub(ph.MODE_RSSI, LAYOUT, clock=lambda: 10.0))), tmp_path / "model.json"


def test_coverage_route_serves_a_level_grid_and_places_per_band(client):
    client, model_file = client
    assert client.get("/sam/presence/coverage").status_code == 404                  # модели нет
    model_file.write_text(json.dumps({"models": MODELS}), encoding="utf8")
    first = client.get("/sam/presence/coverage").json()
    assert (first["model"], first["band"]) == ("phy1-ap0", "5 ГГц")
    assert (first["cell_m"], first["w"], first["h"]) == (0.25, 24, 16) and len(first["levels"]) == 24 * 16    # сетка по плану с шагом клетки
    levels = np.array(first["levels"]).reshape(first["h"], first["w"])
    assert levels[4, 4] > levels[4, 20] + 5.0                                       # (1,1) м у роутера, (5,1) м за стеной
    places = first["places"]                                                         # места по убыванию уровня с полосой
    assert [p["label"] for p in places] == ["рядом", "за стеной"] and places[0]["dbm"] > places[1]["dbm"]
    assert {p["band"] for p in places} <= {b["name"] for b in first["bands"]}
    assert [m["key"] for m in first["models"]] == ["phy1-ap0", "phy0-ap0"] and [m["label"] for m in first["models"]] == ["5 ГГц", "2,4 ГГц"]
    assert first["connected"] == []
    second = client.get("/sam/presence/coverage", params={"model": "phy0-ap0"}).json()
    assert second["band"] == "2,4 ГГц" and second["places"][0]["dbm"] > places[0]["dbm"]          # на 2,4 ГГц те же места сильнее
    off = TestClient(presence_api.create_app(ph.PresenceHub(ph.MODE_OFF, LAYOUT)))
    assert off.get("/sam/presence/coverage").status_code == 409


@pytest.mark.parametrize("dbm, band", [(-50.0, "отлично"), (-60.0, "отлично"), (-66.9, "хорошо"), (-70.0, "средне"), (-80.0, "слабо"), (-95.0, "почти нет")])
def test_coverage_bands_follow_the_thresholds_and_the_lowest_takes_everything_below(dbm, band):
    assert pcov.band_of(dbm) == band
