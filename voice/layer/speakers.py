"""Кто говорит: отпечатки голосов (ECAPA, SpeechBrain) и опознание фразы.


  python -m voice.layer.speakers enroll <имя> <файл.wav> [<файл.wav> ...]   профиль из записей
  python -m voice.layer.speakers enroll-claude                              профиль своего голоса из синтеза голосом по умолчанию
  python -m voice.layer.speakers who <файл.wav>                             кто на записи
  python -m voice.layer.speakers list

Профиль - средний нормированный эмбеддинг, data/voice/voices/<имя>.npy. Профиль claude особый:
фраза с моим голосом - эхо моей озвучки из колонок.
"""

from __future__ import annotations


import sys
from pathlib import Path

import numpy as np

from .config import SELF_PROFILE, VOICES_DIR
from gateway.env import env_float

VOICES = VOICES_DIR
MODEL = "speechbrain/spkrec-ecapa-voxceleb"
SAMPLE_RATE = 16_000
# ниже - «неизвестно». 8 окт: одного домашнего принимало за другого на 0,36, верный профиль на кусках речи
# 0,42-0,45, на целых фразах 0,52-0,72 - порог 0,4; VOICE_MIN_SCORE.
# Замер 9 окт (speaker_eval.py, 1392 фразы одного говорящего 8 окт): на 0,4 «неизвестно» 276 фраз, из них
# от 6 с только 21; чужой профиль побеждает 2. На 0,35 «неизвестно» 207, чужой - 5: порог оставлен
MIN_SCORE = env_float("VOICE_MIN_SCORE", 0.4)
CLAUDE = SELF_PROFILE  # профиль своего голоса: фраза им - эхо своей озвучки

_encoder = None


def encoder():
    global _encoder
    if _encoder is None:
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy
        # COPY: на Windows без прав администратора симлинки по умолчанию падают (WinError 1314)
        _encoder = EncoderClassifier.from_hparams(
            source=MODEL, savedir=str(VOICES / ".model"), run_opts={"device": "cpu"},
            local_strategy=LocalStrategy.COPY)
    return _encoder


def embed(audio: np.ndarray) -> np.ndarray:
    """int16 16 кГц → нормированный эмбеддинг."""
    import torch
    wav = torch.from_numpy(audio.astype(np.float32) / 32768.0).unsqueeze(0)
    e = encoder().encode_batch(wav).squeeze().numpy()
    return e / np.linalg.norm(e)


_reported: set[str] = set()   # о битом профиле - одна строка за запуск, а не на каждую фразу


def _skip(name: str, why: str) -> None:
    if name not in _reported:
        _reported.add(name)
        print(f"[голоса] профиль {name} пропущен: {why}; пересоздайте его командой enroll", flush=True)


def profiles() -> dict[str, np.ndarray]:
    """Профили из VOICES; недописанный или битый файл пропускается, опознание остальных идёт."""
    out = {}
    for path in VOICES.glob("*.npy"):
        try:
            vec = np.load(path)
        except (OSError, ValueError) as e:
            _skip(path.stem, str(e).splitlines()[0] if str(e) else type(e).__name__)
            continue
        if vec.ndim != 1 or not np.all(np.isfinite(vec)):
            _skip(path.stem, f"не вектор эмбеддинга (форма {vec.shape})")
            continue
        out[path.stem] = vec
    return out


def identify(audio: np.ndarray, known: dict[str, np.ndarray] | None = None
             ) -> tuple[str | None, float]:
    """Имя и косинусная близость; None, если ни один профиль не выше MIN_SCORE."""
    known = profiles() if known is None else known
    if not known or len(audio) < SAMPLE_RATE // 2:
        return None, 0.0
    e = embed(audio)
    scores = []
    for n, v in known.items():
        if v.shape != e.shape:   # профиль другой модели эмбеддингов
            _skip(n, f"размер {v.shape[0] if v.ndim else 0}, а у модели {e.shape[0]}")
            continue
        scores.append((n, float(e @ v)))
    if not scores:
        return None, 0.0
    name, score = max(scores, key=lambda x: x[1])
    return (name if score >= MIN_SCORE else None), score


def save_profile(name: str, embeddings: list[np.ndarray]) -> None:
    VOICES.mkdir(exist_ok=True)
    m = np.mean(embeddings, axis=0)
    np.save(VOICES / f"{name}.npy", m / np.linalg.norm(m))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from .hearing import read_wav
    cmd, *args = sys.argv[1:] or ["list"]
    if cmd == "enroll":
        name, files = args[0], args[1:]
        save_profile(name, [embed(read_wav(f)) for f in files])
        print(f"профиль {name}: {len(files)} записей")
    elif cmd == "enroll-claude":
        from .tts_client import synthesize  # синтез переносит SAM-09
        lines = ["Привет, это Сэм, я тебя слышу.",
                 "Свет в комнате выключила, музыку сделала тише.",
                 "Задача готова: собрала отчёт и положила его в папку проекта.",
                 "Завтра в девять утра напомню про встречу, а вечером про ужин.",
                 "Погоди, сейчас проверю, что там с сервером, и сразу скажу."]
        embs = []
        for line in lines:
            audio, rate = synthesize(line)
            n = int(len(audio) * SAMPLE_RATE / rate)
            audio = np.interp(np.linspace(0, len(audio), n, endpoint=False),
                              np.arange(len(audio)), audio).astype(np.int16)
            embs.append(embed(audio))
        save_profile(CLAUDE, embs)
        print(f"профиль {CLAUDE}: {len(lines)} синтезированных фраз")
    elif cmd == "who":
        name, score = identify(read_wav(args[0]))
        print(f"{name or 'неизвестно'} ({score:.2f})")
    else:
        for n in sorted(profiles()):
            print(n)


if __name__ == "__main__":
    main()
