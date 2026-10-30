"""Слепки голосов, загруженные в движок: каждый голос ставится один раз, дальше берётся из памяти.


Один сервер CosyVoice держит несколько голосов и переключается по полю voice запроса без перезапуска.
Как поставить голос в модель (слепок с диска или снять с образца) передаётся функцией install: банк не знает
ни torch, ни CosyVoice и проверяется тестом с подставной функцией (tests/test_layer_voice_registry.py).
"""

from __future__ import annotations

from typing import Callable

from .voice_registry import Voice

Install = Callable[[Voice], str]   # ставит голос в модель под его именем, возвращает откуда он взят - для лога
Log = Callable[[str], None]


class VoiceBank:
    def __init__(self, install: Install, log: Log, label: str):
        self._install, self._log, self._label = install, log, label
        self._loaded: dict[str, Voice] = {}   # имя → запись реестра, с которой голос поставлен
        self._current: str | None = None

    def ensure(self, voice: Voice) -> str:
        """Имя голоса в модели; ставит его, если ещё не стоял или запись в реестре поменялась."""
        if self._loaded.get(voice.name) != voice:
            source = self._install(voice)
            self._loaded[voice.name] = voice
            self._log(f"{self._label} голос {voice.name} {source}")
        return voice.name

    def use(self, voice: Voice) -> str:
        """ensure и строка в лог при смене голоса между репликами."""
        name = self.ensure(voice)
        if name != self._current:
            if self._current is not None:
                self._log(f"{self._label} говорю голосом {name}")
            self._current = name
        return name
