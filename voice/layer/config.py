"""Пути голосового слоя: всё, что зависит от машины, - из переменных окружения.

Каталог данных (профили голосов, калибровка колонок, модель ECAPA) и шина (voice-in.jsonl,
say-out.jsonl, флаги) живут вне git: в них записи голоса и личные профили домашних.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Настройки голосового слоя - в .env репозитория, как у gateway; переменная окружения процесса главнее файла.
# python-dotenv есть не в каждом окружении (сервер CosyVoice идёт в venv лаборатории): тогда только окружение
def load_env(path: Path) -> bool:
    """Подтянуть переменные из файла, не перебивая окружение процесса; False - нет python-dotenv или файла."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(path, override=False)


load_env(_REPO / ".env")

# Каталог данных голосового слоя; по умолчанию data/voice в репозитории, он в .gitignore
DATA_DIR = Path(os.getenv("SAM_VOICE_DATA_DIR", str(_REPO / "data" / "voice")))
# Профили голосов <имя>.npy (SAM-30) и кэш модели ECAPA
VOICES_DIR = Path(os.getenv("SAM_VOICES_DIR", str(DATA_DIR / "voices")))
# Задержка эха по имени выхода (калибровка колонок AudioCore)
CALIBRATION_FILE = Path(os.getenv("SAM_SPEAKER_CALIBRATION", str(VOICES_DIR / "speaker-calibration.json")))
# Шина голосового слоя: папка потоков и флагов (bus.py)
BUS_DIR = Path(os.getenv("SAM_VOICE_BUS_DIR", str(DATA_DIR / "bus")))
# Словарь для initial_prompt Whisper
TERMS_FILE = Path(os.getenv("SAM_VOICE_TERMS", str(Path(__file__).resolve().parent / "terms.txt")))
# Профиль своего голоса: фраза этим голосом - эхо своей озвучки
SELF_PROFILE = os.getenv("SAM_SELF_PROFILE", "claude")
# Реестр голосов синтеза: имя голоса -> движок и образец (voice_registry.py)
VOICE_REGISTRY = Path(os.getenv("SAM_VOICE_REGISTRY", str(VOICES_DIR / "registry.json")))
# Словарь произношения: слово -> написание для синтеза («стек» -> «стэк»), lexicon.py
LEXICON_FILE = Path(os.getenv("SAM_VOICE_LEXICON", str(Path(__file__).resolve().parent / "lexicon.txt")))
# Модель конца хода Smart Turn v3 (turn_end.py): pipecat-ai/smart-turn-v3 на Hugging Face, вариант для CPU
TURN_END_MODEL = Path(os.getenv("SAM_TURN_END_MODEL", str(DATA_DIR / "smart-turn" / "smart-turn-v3.2-cpu.onnx")))
