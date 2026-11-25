"""Сторож микрофона: когда входной поток замолчал, какое устройство ждать и когда пробовать открыть.


Только решения, без устройств: часы и список имён входов передаются параметрами, поток-сторож
и переоткрытие живут в audio_core. Проверка: tests/test_layer_mic_watch.py.

USB-камера, чей микрофон служил входом голосового слоя, отвалилась от системы: колбэк PortAudio
просто перестал приходить, без ошибки и без статуса, слух ждал блок вечно, флаг hearing висел.
Поэтому замолчавший поток распознаётся по времени с последнего блока, а не по ошибке.
"""

from __future__ import annotations

import time
from typing import Callable

from gateway.env import env_float

# Колбэк входа приходит каждые 40 мс (audio_core.BLOCK). Самый долгий законный перерыв - переоткрытие
# потоков при смене выхода (_reinit), но оно сбрасывает часы сторожа. 2 с - 50 пропущенных блоков
# подряд: случайной задержкой планировщика это не бывает, а наблюдатель узнаёт о потере быстрее,
# чем озвучка начнёт ждать фразу человека (say_out.HEARING_WAIT_SECS = 30 с).
STALL_SECS = env_float("VOICE_MIC_STALL_SECS", 2.0)
# Опрос списка входов (winmm, дёшево) и попытка переоткрыть, когда нужное устройство появилось.
# Как DEVICE_POLL у колонок: вернувшийся микрофон начинает слышать в пределах 2 с.
POLL_SECS = env_float("VOICE_MIC_POLL_SECS", 2.0)

LIVE = "live"          # блоки идут
WAITING = "waiting"    # микрофона нет или он замолчал: жду устройство


def same_device(a: str, b: str) -> bool:
    """winmm и MME режут имя до 31 знака, PortAudio на других API даёт полное."""
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def find_input(names: list[str] | None, want: str | None) -> str | None:
    """Имя нужного входа в списке names. want - имя прежнего устройства; None - любой вход
    (стартовали без микрофона, годится системный по умолчанию). names=None - список не узнать
    (не Windows): вернуть want или пустую строку, чтобы попытка открыть всё же была."""
    if names is None:
        return want or ""
    for name in names:
        if want is None or same_device(name, want):
            return name
    return None


class MicWatch:
    """Состояние входа: LIVE - блоки идут, WAITING - жду устройство.

    block() зовёт колбэк микрофона на каждом блоке. Остальное зовёт поток-сторож:
    stalled() - один раз на переходе LIVE -> WAITING; resumed() - в WAITING пришёл блок
    после потери (поток ожил или переоткрыт); retry_due() - в WAITING пора опросить список
    и попробовать открыть, не чаще poll_secs."""

    def __init__(self, live: bool = True, stall_secs: float = STALL_SECS,
                 poll_secs: float = POLL_SECS, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.stall_secs, self.poll_secs = stall_secs, poll_secs
        self.state = LIVE if live else WAITING
        self.last_block = clock()      # последний настоящий блок с микрофона
        self.opened_at = self.last_block  # последнее открытие потока: отсюда тоже считается тишина
        self.lost_at = self.last_block if not live else None
        self.last_retry: float | None = None

    @property
    def waiting(self) -> bool:
        return self.state == WAITING

    def block(self) -> None:
        self.last_block = self.clock()

    def restart(self) -> None:
        """Поток входа переоткрыт: отсчёт тишины заново (переоткрытие само по себе не потеря),
        но это ещё не блок - «вернулся» скажет только первый настоящий блок."""
        self.opened_at = self.clock()

    def stalled(self) -> bool:
        if self.state == LIVE and self.clock() - max(self.last_block, self.opened_at) > self.stall_secs:
            self.state, self.lost_at, self.last_retry = WAITING, self.clock(), None
            return True
        return False

    def resumed(self) -> bool:
        if self.state == WAITING and self.lost_at is not None and self.last_block > self.lost_at:
            self.state, self.lost_at = LIVE, None
            return True
        return False

    def retry_due(self) -> bool:
        if self.state != WAITING:
            return False
        now = self.clock()
        if self.last_retry is not None and now - self.last_retry < self.poll_secs:
            return False
        self.last_retry = now
        return True
