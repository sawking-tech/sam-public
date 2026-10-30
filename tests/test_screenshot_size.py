"""scaled_size и clamp_region из mcp/windows_control_server.py: рамка скриншота и зона."""
import importlib.util
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "mcp" / "windows_control_server.py"


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("windows_control_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("size, max_side, expected", [
    ((3840, 2160), 512, (512, 288)),
    ((1000, 2000), 512, (256, 512)),
    ((300, 200), 512, (300, 200)),
    ((3840, 2160), 0, (3840, 2160)),
], ids=["4k_keeps_aspect", "tall_bound_by_height", "small_not_enlarged", "zero_disables_scaling"])
def test_screenshot_scaled_to_frame(server, size, max_side, expected):
    assert server.scaled_size(*size, max_side) == expected


@pytest.mark.parametrize("zone, expected", [
    ((0, 0, 0, 0), (0, 0, 3840, 2160)),
    ((3800, 2100, 500, 500), (3800, 2100, 3840, 2160)),
    ((-50, 9999, 100, 100), (0, 2159, 100, 2160)),
], ids=["empty_is_whole_screen", "cut_at_edge", "corner_pulled_in"])
def test_zone_clamped_to_screen(server, zone, expected):
    assert server.clamp_region(*zone, 3840, 2160) == expected
