"""Многостенная модель уровня (SAM-118): пересечения стен, подгонка и предсказание."""
import numpy as np
import pytest

from radar import presence_radio_map as rm

ROUTER = (0.0, 0.0)
WALL_X3 = {"x1": 3.0, "z1": -5.0, "x2": 3.0, "z2": 5.0, "t": 0.2}


def test_signal_path_crosses_walls_and_passes_through_doorways():
    """Толщина стен на луче от роутера, проём пропускает, край тени мягкий."""
    points = np.array([[2.0, 0.0], [4.0, 0.0], [4.0, 20.0], [-4.0, 0.0]])
    assert rm.wall_thickness_on_path([WALL_X3], ROUTER, points).tolist() == [0.0, 0.2, 0.0, 0.0]
    two = rm.wall_thickness_on_path([WALL_X3, {**WALL_X3, "x1": 6.0, "x2": 6.0, "t": 0.1}], ROUTER, np.array([[8.0, 0.0]]))
    assert two[0] == pytest.approx(0.3)
    door = {"x": 2.5, "z": -0.05, "w": 1.0, "d": 0.1, "angle": 1.5707964}      # проём с центром (3; 0) в стене x=3, ширина вдоль неё
    points = np.array([[6.0, 0.0], [6.0, 3.0], [6.0, 4.9]])
    blocked = rm.wall_thickness_on_path([WALL_X3], ROUTER, points, soft_m=0.0)
    opened = rm.wall_thickness_on_path([WALL_X3], ROUTER, points, [door], soft_m=0.0)
    assert blocked.tolist() == [0.2, 0.2, 0.2] and opened[0] == 0.0 and opened[1] == 0.2
    soft = rm.wall_thickness_on_path([WALL_X3], ROUTER, np.array([[6.0, 10.0]]))[0]       # луч к (6; 10) пересекает стену на z=5: на самом краю
    assert 0.0 < soft < 0.2


def test_wall_model_fit_recovers_parameters_and_predicts_levels():
    """Подгонка возвращает параметры модели, за стеной уровень падает на потерю стены, пики у роутера поднимают ближнее поле, мало точек - ValueError."""
    truth = rm.WallModel(-45.0, 2.0, 50.0)
    xy = np.array([[2.0, 1.0], [4.0, 0.5], [6.0, -1.0], [8.0, 2.0], [3.5, -3.0]])
    dist = rm.distance3d(xy, ROUTER)
    thickness = rm.wall_thickness_on_path([WALL_X3], ROUTER, xy)
    fitted = rm.fit_wall_model(dist, thickness, truth.predict(dist, thickness))
    assert fitted.l0_db == pytest.approx(-45.0, abs=1e-6) and fitted.wall_db_per_m == pytest.approx(50.0, abs=1e-6)
    nodes = np.array([[2.9, 0.0], [3.1, 0.0]])
    before, after = rm.expected_levels(truth, nodes, [WALL_X3], ROUTER)
    d_before, d_after = rm.distance3d(nodes, ROUTER)
    assert before - after == pytest.approx(50.0 * 0.2 + 20.0 * np.log10(d_after / d_before), abs=1e-6)
    with pytest.raises(ValueError):
        rm.fit_wall_model(np.array([1.0, 2.0]), np.array([0.0, 0.1]), np.array([-50.0, -60.0]))
    levels = truth.predict(dist, thickness)
    plain = rm.fit_wall_model(dist, thickness, levels)
    boosted = rm.fit_wall_model(dist, thickness, levels, peak_levels_db=[-40.0, -42.0, -44.0])
    near, far = np.array([rm.NEAR_REF_M]), np.array([rm.NEAR_BREAK_M + 1.0])
    assert boosted.predict(near, np.zeros(1))[0] == pytest.approx(-42.0, abs=1e-6)
    assert boosted.predict(far, np.zeros(1))[0] == pytest.approx(plain.predict(far, np.zeros(1))[0])
