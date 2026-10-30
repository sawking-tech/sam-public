"""Фраза «пора размяться» (SAM-117): сколько человек на месте, как давно в основном сидит или лежит, и обоснование, чтобы SAM могла сказать не просто «прогуляйся»,
а почему. Чистый модуль: имя, минуты, доля неподвижного времени и поза приходят аргументами; когда говорить - решает хаб (STAY_ALERT_S)."""
from __future__ import annotations

POSE_WORDS = {"sit": "сидишь", "lie": "лежишь", "stand": "стоишь"}      # как человек проводит время на месте
MIN_SHARE_FOR_REASON = 0.7                                               # «в основном сидел или лежал» говорится, когда неподвижного времени не меньше этой доли


def stretch_message(name: str, minutes: float, still_share: float, pose: str | None, place: str | None) -> dict:
    """{say, reason}: say - что произнести, reason - обоснование по данным карты."""
    how = POSE_WORDS.get(pose or "", "на месте")
    where = f" ({place})" if place else ""
    hours = minutes / 60.0
    since = f"{round(minutes)} минут" if hours < 2 else f"{hours:.1f} часа".replace(".", ",")
    reason = f"{name}, ты уже {since} подряд {how}{where}"
    if still_share >= MIN_SHARE_FOR_REASON:
        reason += f"; по карте за день ты в основном сидел или лежал, неподвижного времени около {round(still_share * 100)}%"
    say = (f"{reason}. Суставам и спине так тяжело: к старости будут болеть, а скорее всего уже сегодня к вечеру. Давай разминку: встань, потянись, "
           "сделай несколько приседаний и пройдись по квартире.")
    return {"say": say, "reason": reason}
