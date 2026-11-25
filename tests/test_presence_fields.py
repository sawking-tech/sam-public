"""Слои вероятности (SAM-118): пятна из строк кадра, их прозрачность и описания телефона, человека и потока; все сценарии одним запуском node."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

ROW = {"name": "Beta-Max-13", "iface": "phy1-ap0", "signal_db": -66.5, "at": {"place": "диван"},
       "field": [{"place": "диван", "p": 0.62, "x": 9.8, "z": 5.3}, {"place": "туалет", "p": 0.2, "x": 7.7, "z": 7.5}, {"place": "кухня", "p": 0.01, "x": None, "z": None}]}
IDENT = {"confidence": 0.77, "evidence": [{"x": 9.8, "z": 5.3, "w": 0.77, "source": "messages", "note": "со слов"}, {"x": None, "z": None, "w": 0.3, "source": "wifi"},
                                           {"x": 1.0, "z": 1.0, "w": 0.01, "source": "wifi"}]}
FLOW_DEVICE = {"name": "Beta-Max-13", "iface": "phy0-ap0", "point": {"x": 9.8, "z": 5.3}, "places": [{"place": "диван", "p": 0.62}, {"place": "кухня", "p": 0.2}]}
GROWTH = [{"growth": {"peak": 0.012, "mass_near": 0.2, "area_m2": 12.5}}, {"growth": {"peak": 0.06, "mass_near": 0.62, "area_m2": 4.59}}, {}]
SCENARIOS = {
    "phone_blobs": f"m.phoneBlobs({json.dumps(ROW)}).map(b => b.label)",
    "person_blobs": f"m.personBlobs({json.dumps(IDENT)}).map(b => [b.label, b.p])",
    "blob_alpha": ("{ const stops = []; const g = { createRadialGradient() { return { addColorStop: (k, c) => stops.push(c) }; }, set fillStyle(v) {}, fillRect() {} }; "
                   "m.paintBlobs(g, [{x: 10, z: 6, p: 0.5}], [127, 227, 255], { minX: 8, minZ: 4 }, 40, 1); return stops; }"),
    "phone": f"m.describePhone({json.dumps(ROW)})",
    "person": f"m.describePerson({json.dumps(IDENT)})",
    "flow": f"flow.describeFlow({json.dumps(FLOW_DEVICE)})",
    "flow_none": "flow.describeFlow(null)",
    "growth": f"flow.describeGrowth({json.dumps(GROWTH)}, 2)",
    "growth_empty": "flow.describeGrowth([{}], 1)",
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("fields.js", SCENARIOS, extra={"flow": "flow.js"})


def test_probability_blobs_come_from_placed_phone_places_and_person_evidence(js):
    assert js["phone_blobs"] == ["диван", "туалет"]                                   # место без координат пятна не даёт
    assert js["person_blobs"] == [["messages", 0.77]]                                  # свидетельство без места и слабое - без пятна
    assert js["blob_alpha"] == ["rgba(127,227,255,0.425)", "rgba(127,227,255,0)"]      # прозрачность в центре пропорциональна вероятности


@pytest.mark.parametrize("scenario, text", [
    ("phone", "Beta, 5 ГГц, -66.5 дБ, выбрано: диван: диван 62%, туалет 20%"),
    ("person", "уверенность 77%; свидетельства: messages 77%"),
    ("flow", "Beta, 2,4 ГГц, точка (9.8; 5.3): диван 62%, кухня 20%"),
    ("flow_none", ""),
    ("growth", ["  1 с: пик 1.2%, у точки 20.0%, площадь 90% 12.5 м2", "> 2 с: пик 6.0%, у точки 62.0%, площадь 90% 4.6 м2"]),
    ("growth_empty", ["> 1 с: нет данных"]),
])
def test_layer_texts_describe_phone_person_flow_and_growth(js, scenario, text):
    assert js[scenario] == text
