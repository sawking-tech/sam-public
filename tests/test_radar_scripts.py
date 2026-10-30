"""Подгонка модели уровня по отпечаткам (scripts/radar_radio_fit.py, SAM-118) на малых выдуманных данных."""
import math

import pytest

from tests.fakes_presence import load_script


def test_radio_fit_model_per_band_with_device_offsets():
    """Одна модель на диапазон со сдвигом каждого устройства; у диапазона со слишком малым числом мест модели нет."""
    rf = load_script("radar_radio_fit")
    layout = {"router": {"x": 0.0, "z": 0.0, "h": 2.4}, "walls": [], "objects": []}
    controls = {f"p{i}": {"x": 1.0 + i, "z": 0.5} for i in range(8)}
    base = {f"p{i}": -40.0 - 25.0 * math.log10(1.0 + i) for i in range(8)}
    shifted = {place: [level - 3.0] for place, level in base.items()}                                         # второе устройство слабее на 3 дБ везде
    fingerprints = {"s24@phy1-ap0": {place: [level] for place, level in base.items()}, "redmi@phy1-ap0": shifted, "s24@phy0-ap0": {"p0": [-40.0], "p1": [-50.0]}}
    fitted = rf.fit_all(fingerprints, controls, layout)
    assert list(fitted["models"]) == ["phy1-ap0"]                                                            # у 2,4 ГГц слишком мало мест: модели нет
    model = fitted["models"]["phy1-ap0"]
    assert model["controls"] == 16 and model["n"] in rf.EXPONENTS and model["leave_one_out_rms_db"] < 3.0 and model["band"] == "5 ГГц"
    offsets = fitted["device_offsets_db"]
    assert offsets["s24@phy1-ap0"] == 0.0 and offsets["redmi@phy1-ap0"] == pytest.approx(-3.0, abs=0.3)      # сдвиг L0 разводит устройства при общей модели
