"""Где рисовать устройство (SAM-118): на своём месте, пока лежит; по своей оценке, когда взято; у хозяина без оценки; нигде без обоих.
Все сценарии devices.js одним запуском node."""
import json

import pytest

from tests.fakes_presence import run_node_scenarios

OWNER = {"cx": 9.8, "cz": 5.3, "conf": 0.9}
AT_DESK = {"place": "стол", "x": 8.0, "z": 4.0}
TOILET = [{"place": "туалет", "p": 0.66, "x": 7.7, "z": 7.5}]
TARGETS = {                                                          # устройство, носитель, где рисовать
    "flow_point": ({"at": AT_DESK, "where": [{"place": "диван", "p": 0.9, "x": 9.8, "z": 5.3}]}, OWNER, {"x": 8.0, "y": 0.9, "z": 4.0}),
    "own_estimate": ({"where": TOILET}, OWNER, {"x": 7.7, "y": 0.9, "z": 7.5}),
    "weak_estimate_follows_owner": ({"where": [{"place": "кухня", "p": 0.2, "x": 11.0, "z": 7.0}]}, OWNER, {"x": 9.8, "y": 0.9, "z": 5.3}),
    "no_owner_nowhere": ({"where": []}, None, None),
    "faint_owner_nowhere": ({"where": []}, {"cx": 1, "cz": 1, "conf": 0.01}, None),
    "sampled_place_wins": ({"at": AT_DESK, "where": TOILET}, OWNER, {"x": 8.0, "y": 0.9, "z": 4.0}),
}
PUSHES = {                                                           # точка телефона, фигуры, куда она уходит
    "inside_to_edge": ({"x": 9.9, "z": 5.3}, [OWNER], {"x": 10.4, "z": 5.3}),       # на 0,6 м от центра по линии от него
    "far_figure": ({"x": 12.0, "z": 5.3}, [OWNER], {"x": 12.0, "z": 5.3}),
    "faint_figure": ({"x": 9.9, "z": 5.3}, [{**OWNER, "conf": 0.01}], {"x": 9.9, "z": 5.3}),
    "exact_centre_to_plus_x": ({"x": 9.8, "z": 5.3}, [OWNER], {"x": 10.4, "z": 5.3}),
}
LABELS = {
    "present": ("'Redmi-Note-13-Pro-5G', -66.5, 'Owner', true, false", "Owner: Redmi · -66.5 дБ"),
    "left": ("'Redmi-Note-13-Pro-5G', -66.5, 'Owner', false, true", "Owner: Redmi · вышел из сети"),
    "no_owner": ("'Redmi', -66.5, null, false, false", ""),
}
SCENARIOS = {
    **{f"target:{k}": f"m.deviceTarget({json.dumps(d)}, {json.dumps(c)})" for k, (d, c, _) in TARGETS.items()},
    **{f"push:{k}": f"m.pushOut({json.dumps(p)}, {json.dumps(f)})" for k, (p, f, _) in PUSHES.items()},
    **{f"label:{k}": f"m.deviceLabel({args})" for k, (args, _) in LABELS.items()},
}


@pytest.fixture(scope="module")
def js():
    return run_node_scenarios("devices.js", SCENARIOS)


@pytest.mark.parametrize("case", list(TARGETS))
def test_device_is_drawn_at_its_point_its_estimate_its_owner_or_nowhere(js, case):
    assert js[f"target:{case}"] == TARGETS[case][2]


@pytest.mark.parametrize("case", list(PUSHES))
def test_phone_inside_a_figure_is_pushed_to_its_edge(js, case):
    got, expected = js[f"push:{case}"], PUSHES[case][2]
    assert got["x"] == pytest.approx(expected["x"]) and got["z"] == pytest.approx(expected["z"])


@pytest.mark.parametrize("case", list(LABELS))
def test_device_label_names_owner_device_and_level_or_left(js, case):
    assert js[f"label:{case}"] == LABELS[case][1]
