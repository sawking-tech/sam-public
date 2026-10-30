"""Фраза «пора размяться» (SAM-117): обоснование по данным карты."""
import pytest

from radar import presence_stay as ps

NAME = "Имя"


@pytest.mark.parametrize("minutes, share, pose, place, has, lacks", [
    (95, 0.93, "sit", "стол", ["95 минут", "сидишь", "(стол)", "93%"], []),
    (200, 0.5, "lie", None, ["3,3 часа", "лежишь"], ["в основном", "("]),          # часы; двигался достаточно - без доли за день; без места - без скобок
    (70, 0.9, None, None, ["на месте"], []),                                       # поза неизвестна
], ids=["minutes-pose-place", "hours-moving", "no-pose"])
def test_stretch_message_names_time_pose_place_and_reason(minutes, share, pose, place, has, lacks):
    got = ps.stretch_message(NAME, minutes, share, pose, place)
    assert all(part in got["reason"] for part in has) and not any(part in got["reason"] for part in lacks)
    assert "разминку" in got["say"] and "суставам" in got["say"].lower() and got["say"].startswith(got["reason"])
