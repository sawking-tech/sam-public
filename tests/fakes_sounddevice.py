"""sounddevice без звукового устройства: заглушка для машин, где PortAudio нет.

`import sounddevice` сам поднимает PortAudio и падает, если звукового сервера нет (CI-раннер: PulseAudio
не запущен). Модули голосового слоя импортируют его на уровне модуля, поэтому без заглушки не собирается
ни один тест, который их тянет.

Тесты устройств и так всё подменяют: здесь - только имена, которые модули и тесты берут у sounddevice
(исключение, устройства), и ни одного настоящего вызова. Модуль подставляется в sys.modules **до** первого
импорта голосового слоя; на машине с рабочим PortAudio он тоже ставится: ни один тест не должен трогать
настоящие карты.
"""
from __future__ import annotations

import sys
import types

SD = "sounddevice"


class PortAudioError(Exception):
    """Ошибка PortAudio: тесты и код ловят её по имени."""


class _Stream:
    """Поток на границе: start/stop/close есть, звука нет."""

    active = False

    def __init__(self, *args, **kwargs) -> None:
        self.device = kwargs.get("device")
        self.started = False

    def start(self) -> None:
        self.started = True
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False

    def __enter__(self) -> "_Stream":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Default:
    device = (None, None)


def _no_device(*args, **kwargs):
    """Устройств нет: пустой список, как на машине без звуковых карт.

    Запрос одного устройства (индекс или имя) отвечает отказом, как PortAudio: код ловит его и говорит
    «системного нет»; код, которому нужен список, получает пустой и решает сам.
    """
    if args and (isinstance(args[0], int) or len(args) > 1):
        raise PortAudioError("Error querying device -1")
    return []


def install() -> None:
    """Подставить заглушку в sys.modules, если настоящего sounddevice ещё не импортировали."""
    if SD in sys.modules:
        return
    mod = types.ModuleType(SD)
    mod.PortAudioError = PortAudioError
    mod.InputStream = _Stream
    mod.OutputStream = _Stream
    mod.RawInputStream = _Stream
    mod.RawOutputStream = _Stream
    mod.Stream = _Stream
    mod.query_devices = _no_device
    mod.query_hostapis = lambda *a, **k: []
    mod.default = _Default()
    mod.play = lambda *a, **k: None
    mod.rec = lambda *a, **k: None
    mod.wait = lambda *a, **k: None
    mod.stop = lambda *a, **k: None
    mod.sleep = lambda *a, **k: None
    mod.get_stream = lambda *a, **k: None
    mod._initialize = lambda: None
    mod._terminate = lambda: None
    mod.__version__ = "stub"
    sys.modules[SD] = mod
