"""Дубли подписей на странице радара (SAM-105): чистый labels.js проверяется в node без браузера, один запуск node на файл."""
import pytest

from tests.fakes_presence import run_node_scenarios

SCENARIOS = {
    # ключ без регистра, лишних пробелов и пометки «(примерно)»
    "key": "['Комод (примерно)', '  комод ', 'Письменный  стол'].map(m.labelKey)",
    # близкие дубли: объект уступил зоне, место плана - комнате
    "close": "[...m.hiddenLabels([{key: 'комод', prio: m.PRIORITY.object, x: 1, z: 2}, {key: 'комод', prio: m.PRIORITY.zone, x: 1.1, z: 2.2}, "
             "{key: 'кухня', prio: m.PRIORITY.room, x: 10, z: 6}, {key: 'кухня', prio: m.PRIORITY.place, x: 10, z: 7}])].sort()",
    # одно имя в разных комнатах остаётся, разные имена не сталкиваются
    "far": "[...m.hiddenLabels([{key: 'стол', prio: 1, x: 0, z: 0}, {key: 'стол', prio: 1, x: 8, z: 0}, {key: 'стул', prio: 1, x: 0, z: 0}])]",
}


@pytest.fixture(scope="module")
def answers():
    return run_node_scenarios("labels.js", SCENARIOS)


@pytest.mark.parametrize("scenario, expected", [
    ("key", ["комод", "комод", "письменный стол"]),
    ("close", [0, 3]),
    ("far", []),
])
def test_labels_hide_close_duplicates_by_priority(answers, scenario, expected):
    assert answers[scenario] == expected
