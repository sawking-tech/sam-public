"""Реестр голосов: имя голоса → движок, пресет или образец с расшифровкой, слепок на диске.


Один источник для клиента синтеза, серверов Silero и CosyVoice и session.py: раньше
один образец для CosyVoice искали по диску, другой был зашит в лаборатории.
Файл - data/voice/voices/registry.json (SAM_VOICE_REGISTRY переопределяет):

  {"defaults": {"silero": "eugene", "cosy": "cam"},
   "voices": {"xenia": {"engine": "silero", "about": "...",
                        "silero": {"speaker": "xenia"},
                        "cosy": {"ref": "E:/.../xenia.wav", "text": "E:/.../xenia.txt", "imprint": "cosy-xenia.pt"}}}}

У голоса раздел на каждый движок, которым он звучит; engine - движок голоса, если session.json не выбрал другой.
Относительные пути считаются от папки реестра. Здесь только разбор и выбор, без звука и модели: проверяется
тестом без видеокарты (tests/test_layer_voice_registry.py).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .config import VOICE_REGISTRY

REGISTRY = VOICE_REGISTRY
ENGINE_KEY = "engine"   # движок голоса по умолчанию
ABOUT_KEY = "about"     # описание для человека: JSON без комментариев


class RegistryError(ValueError):
    """Реестр не разобран или в нём нет нужного голоса; текст - что не так и где."""


@dataclass(frozen=True)
class Voice:
    """Голос на одном движке."""
    name: str
    engine: str
    speaker: str | None = None    # пресет движка (Silero)
    ref: Path | None = None       # образец wav для клонирования (CosyVoice)
    text: Path | None = None      # расшифровка образца
    imprint: Path | None = None   # слепок голоса на диске: снимается с образца один раз


class Registry:
    def __init__(self, voices: dict[str, dict[str, Voice]], preferred: dict[str, str], defaults: dict[str, str]):
        self._voices = voices          # имя → движок → голос
        self._preferred = preferred    # имя → движок голоса
        self._defaults = defaults      # движок → имя голоса, если в запросе чужой или пустой

    def names(self, engine: str) -> list[str]:
        """Голоса, которые звучат на движке."""
        return [name for name, engines in self._voices.items() if engine in engines]

    def get(self, name: str | None, engine: str) -> Voice | None:
        return self._voices.get(name or "", {}).get(engine)

    def choose(self, name: str | None, engine: str) -> Voice:
        """Голос из запроса; его нет на движке - голос движка по умолчанию."""
        voice = self.get(name, engine) or self.get(self._defaults.get(engine), engine)
        if voice is None:
            raise RegistryError(f"реестр голосов: у движка {engine} нет голоса по умолчанию (defaults)")
        return voice

    def engine_for(self, name: str | None, requested: str | None) -> str | None:
        """Движок голоса: выбранный в session.json, если голос на нём есть, иначе свой; голоса нет в реестре - None."""
        engines = self._voices.get(name or "")
        if not engines:
            return None
        return requested if requested in engines else self._preferred[name]

    def check(self, name: str, engine: str | None = None) -> None:
        """Ошибка с понятным текстом, если голоса нет вообще или нет на указанном движке."""
        if name not in self._voices:
            raise RegistryError(f"голоса {name} нет в реестре; есть: {', '.join(self._voices)}")
        if engine and engine not in self._voices[name]:
            raise RegistryError(f"голос {name} не звучит на {engine}; есть на: {', '.join(self._voices[name])}")


def _path(value: str | None, base: Path) -> Path | None:
    """Путь из реестра: абсолютный остаётся как есть, относительный считается от папки реестра.

    Абсолютность проверяется и по Windows: реестр пишут на одной машине, читают на другой, и `C:/voice/x.wav`
    на Linux не абсолютный для Path - он приклеился бы к папке реестра."""
    if not value:
        return None
    p = Path(value)
    if p.is_absolute() or PureWindowsPath(value).is_absolute():
        return p
    return base / p


def parse(data: dict, base: Path) -> Registry:
    """Реестр из разобранного JSON; base - папка реестра для относительных путей."""
    voices: dict[str, dict[str, Voice]] = {}
    preferred: dict[str, str] = {}
    for name, entry in (data.get("voices") or {}).items():
        engines = {key: Voice(name, key, sec.get("speaker"), _path(sec.get("ref"), base),
                              _path(sec.get("text"), base), _path(sec.get("imprint"), base))
                   for key, sec in entry.items() if isinstance(sec, dict)}
        engine = entry.get(ENGINE_KEY)
        if engine not in engines:
            raise RegistryError(f"реестр голосов: у {name} engine={engine}, а разделы есть только {', '.join(engines)}")
        voices[name], preferred[name] = engines, engine
    defaults = dict(data.get("defaults") or {})
    for engine, name in defaults.items():
        if engine not in voices.get(name, {}):
            raise RegistryError(f"реестр голосов: голос по умолчанию {name} не звучит на {engine}")
    return Registry(voices, preferred, defaults)


def load(path: Path = REGISTRY) -> Registry:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"реестр голосов {path} не прочитан: {exc}") from exc
    return parse(data, path.parent)


class Live:
    """Реестр, который перечитывается при правке файла: голос меняется без перезапуска серверов.
    Сломанный при правке файл не роняет синтез: остаётся последний разобранный, в лог одна строка."""

    def __init__(self, path: Path = REGISTRY, label: str = "[синтез]"):
        self.path, self.label = path, label
        self._stamp: tuple[int, int] | None = None
        self._registry: Registry | None = None

    def get(self) -> Registry:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp or self._registry is None:
            self._stamp = stamp
            try:
                self._registry = load(self.path)
            except RegistryError as exc:
                if self._registry is None:
                    raise
                print(f"{self.label} {exc}; остаюсь на прежнем реестре", file=sys.stderr, flush=True)
        return self._registry
