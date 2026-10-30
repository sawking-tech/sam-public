"""Фальшивки и выдуманные данные тестов радара: внешняя граница (роутер, сеть, часы) и синтетические планы вместо личных данных стенда."""
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest


# --- общее: node (часть B) ---

STATIC_PRESENCE = Path(__file__).resolve().parents[1] / "radar" / "static" / "presence"
NODE_TIMEOUT_S = 60          # один процесс на файл тестов со всеми сценариями; с запасом на холодный старт Windows


def require_node() -> None:
    """Без node тест пропускается; в CI (CI=1) это ошибка: зелёный прогон без node ничего бы не проверил."""
    if shutil.which("node") is None:
        if os.environ.get("CI") == "1":
            pytest.fail("нужен node: в CI тесты страницы радара не пропускаются")
        pytest.skip("нужен node")


def run_node_scenarios(module: str, scenarios: dict[str, str], extra: dict[str, str] | None = None, prelude: str = "") -> dict:
    """Один запуск node: модуль `module` из radar/static/presence импортируется как m, каждое выражение сценария вычисляется, ответы - JSON по именам.
    Выражение может быть телом стрелочной функции в фигурных скобках с return. extra - ещё модули (имя -> файл), prelude - общий код перед сценариями."""
    require_node()
    uri = (STATIC_PRESENCE / module).as_uri()
    imports = "".join(f"import * as {alias} from {json.dumps((STATIC_PRESENCE / file).as_uri())};\n" for alias, file in (extra or {}).items())
    body = ",\n".join(f"{json.dumps(name)}: (() => {expr if expr.lstrip().startswith('{') else '(' + expr + ')'})()" for name, expr in scenarios.items())
    code = f"import * as m from {json.dumps(uri)};\n{imports}{prelude}\nconsole.log(JSON.stringify({{\n{body}\n}}));"
    out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- часть B ---
# План живого потока: квартира 6 x 4 м, внутренняя стена x = 3 с дверью, роутер в левом углу, мебели и подписанных мест нет;
# одно выдуманное устройство с моделью уровня на 5 ГГц и два места потока слева и справа.

FLOW_NAME, FLOW_IFACE = "Test-Phone", "phy1-ap0"
FLOW_PLACES = {"left": [1.0, 1.0], "right": [5.0, 3.0]}
FLOW_MODEL = {f"{FLOW_NAME.lower()}@{FLOW_IFACE}": {"l0_db": -40.0, "n": 2.0, "wall_db_per_m": 20.0}}
FLOW_START = 1_000_000.0
FLOW_TEST_WINDOW = 7          # окно тестового потока: боевое окно длиннее, а тесты проверяют правила на коротком


def flow_layout() -> dict:
    door = {"id": "d", "title": "Дверная рама", "kind": "opening", "x": 2.6, "z": 1.95, "w": 0.8, "d": 0.1, "h": 2.0, "angle": math.pi / 2}
    outer = [(0, 0, 6, 0), (6, 0, 6, 4), (6, 4, 0, 4), (0, 4, 0, 0)]
    walls = [{"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": 0.2, "h": 2.5} for x1, z1, x2, z2 in outer] + [{"x1": 3, "z1": 0, "x2": 3, "z2": 4, "t": 0.1, "h": 2.5}]
    return {"walls": walls, "objects": [door], "router": {"x": 0.5, "z": 0.5, "h": 2.4}, "places": []}


class LoopDone(Exception):
    """Фальшивая пауза исчерпала заданное число тиков: цикл хаба остановлен."""


class TickSleep:
    """Пауза цикла хаба (параметр sleep) без настенного времени: каждый вызов - один тик; перед тиком зовёт before(n), после ticks тиков бросает LoopDone."""

    def __init__(self, ticks: int, before=None):
        self.ticks, self.before, self.calls = ticks, before, 0

    async def __call__(self, seconds: float) -> None:
        if self.calls >= self.ticks:
            raise LoopDone
        if self.before is not None:
            self.before(self.calls)
        self.calls += 1


# --- часть A ---


def isolated_hub_env(tmp_path: Path, **extra: str) -> dict:
    """Окружение для presence_hub.build, где каждый файл и каталог данных радара лежит в tmp_path: data/ стенда в тест не попадает."""
    from radar import presence as pr
    from radar import presence_flow_history as pfh
    from radar import presence_hub as ph
    from radar import presence_pc as ppc
    names = [v for k, v in vars(ph).items() if k.endswith(("_FILE_ENV", "_DIR_ENV")) and isinstance(v, str)]
    env = {name: str(tmp_path / name.lower()) for name in names + [pfh.HISTORY_DIR_ENV, ppc.PC_FILE_ENV]}
    env[pr.LAYOUT_ENV] = str(tmp_path / "no-layout.json")
    return {**env, **extra}


# --- часть C ---


class CorridorNavigation:
    """Навигация выдуманного прямого коридора: клетки в ряд по оси x с шагом сетки, стен нет."""

    def __init__(self, cells: int):
        self.cells = cells

    def stand_nodes(self, step_m):
        return [(x * step_m, 0.0) for x in range(self.cells)]

    def segment_free(self, a, b):
        return True

    def snap(self, x, z):
        return (x, z)


ROUTINE_PEOPLE = ("a", "b", "guest")                    # ключи людей встроенных сцен быта
ROUTINE_PLACES = [{"label": "pc-a", "x": 1.4, "z": 0.6}, {"label": "pc-b", "x": 2.4, "z": 0.9}, {"label": "Газовая плита", "x": 5.5, "z": 1.2},
                  {"label": "Кухня", "x": 4.2, "z": 1.6}, {"label": "Туалет", "x": 0.6, "z": 1.6}, {"label": "Ванная", "x": 2.4, "z": 3.4}]


def routine_layout() -> dict:
    """Выдуманный план (two_room_layout) с местами встроенных сцен быта и вылазок: компьютеры, кухня, плита, туалет, ванная; жильцы a, b и гость."""
    layout = two_room_layout({person: {} for person in ROUTINE_PEOPLE})
    layout["places"] = layout["places"] + [dict(p) for p in ROUTINE_PLACES]
    return layout


# --- часть D ---
# Общий выдуманный план вместо личного data/home_layout.json: квартира 6 x 4 м, внутренняя стена x = 3 с дверью на z = 2,
# слева спальня с кроватью, справа зал с диваном и шкафом; места подписаны, вход у правой стены.


def _wall(x1, z1, x2, z2, t=0.2, h=2.5):
    return {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": t, "h": h}


def two_room_layout(people: dict | None = None) -> dict:
    """Новый словарь плана на каждый вызов: тест может его менять. people - жильцы плана (ключ -> свойства)."""
    door = {"id": "door", "title": "Дверная рама", "kind": "opening", "x": 2.6, "z": 1.95, "w": 0.8, "d": 0.1, "h": 2.0, "angle": math.pi / 2}
    bed = {"id": "bed", "title": "Кровать", "kind": "furniture", "x": 0.3, "z": 2.2, "w": 1.6, "d": 1.6, "h": 0.5, "angle": 0.0}
    sofa = {"id": "sofa", "title": "Диван", "kind": "furniture", "x": 3.8, "z": 3.0, "w": 2.0, "d": 0.9, "h": 0.87, "angle": 0.0}
    wardrobe = {"id": "wardrobe", "title": "Шкаф", "kind": "furniture", "x": 4.8, "z": 0.2, "w": 1.0, "d": 0.6, "h": 2.1, "angle": 0.0}
    return {
        "name": "synthetic",
        "walls": [_wall(0, 0, 6, 0), _wall(6, 0, 6, 4), _wall(6, 4, 0, 4), _wall(0, 4, 0, 0), _wall(3, 0, 3, 4, t=0.1)],
        "rooms": [{"id": "bedroom", "title": "спальня", "x": 0, "z": 0, "w": 3, "d": 4}, {"id": "hall", "title": "зал", "x": 3, "z": 0, "w": 3, "d": 4}],
        "objects": [door, bed, sofa, wardrobe],
        "places": [{"label": "Угол спальни", "x": 0.8, "z": 0.8}, {"label": "Угол зала", "x": 5.0, "z": 1.5}, {"label": "Вход", "x": 5.6, "z": 2.0}],
        "router": {"x": 0.5, "z": 0.5, "h": 2.4},
        "people": dict(people or {}),
    }


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    """Скрипт scripts/<name>.py как модуль по пути. Папка scripts на пути импорта и сам модуль в sys.modules (его ищут dataclass) -
    только на время загрузки: скрипты берут соседей вроде radar_replay, а после загрузки ничего не остаётся на весь прогон."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as mp:
        mp.syspath_prepend(str(SCRIPTS_DIR))
        mp.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module


def write_layout(tmp_path: Path, layout: dict | None = None, name: str = "layout.json") -> Path:
    """План в файл под tmp_path (по умолчанию two_room_layout) - для build() и скриптов, читающих план с диска."""
    path = tmp_path / name
    path.write_text(json.dumps(layout or two_room_layout(), ensure_ascii=False), encoding="utf-8")
    return path
