"""Сглаживание уровня сигнала медианой по окну (SAM-118)."""
import math

import pytest

from radar import presence_smooth as ps

WINDOW_S = 5.0


@pytest.mark.parametrize("samples, at, expected", [
    ([(0, -50.0), (1, -51.0), (2, -50.0), (3, -75.0), (4, -50.0)], 4, -50.0),                       # одиночный провал медиану не двигает
    ([(t, -50.0) for t in range(5)] + [(5, -70.0)], 5, -50.0),                                      # начало сдвига ещё не медиана
    ([(t, -50.0) for t in range(5)] + [(5 + i, -70.0) for i in range(5)], 9, -70.0),                # устойчивый сдвиг - после половины окна
    ([(0.0, -40.0), (10.0, -60.0), (10.0, -90.0)], 10.0, -60.0),                                    # старое ушло из окна, тот же момент не добавлен дважды
    ([], 0.0, None),                                                                                # отсчётов нет - nan
], ids=["dip", "shift-start", "lasting-shift", "old-and-repeat", "empty"])
def test_level_median_ignores_a_dip_follows_a_lasting_shift_and_drops_old_samples(samples, at, expected):
    s = ps.LevelSmoother(window_s=WINDOW_S)
    for t, db in samples:
        s.add(t, db)
    got = s.median(at)
    assert math.isnan(got) if expected is None else got == expected
