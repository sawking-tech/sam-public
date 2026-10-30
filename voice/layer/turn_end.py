"""Конец хода моделью: на короткой паузе Smart Turn v3 решает, закончена ли фраза.

Пункт плана voice-turn-end, обоснование -, раздел «Выбор»:
фиксированная тишина 1,5 с (hearing.SILENCE_SHORT) съедает почти весь бюджет ответа в 1-2 с. Модель
работает поверх паузы и ничего не отнимает: на паузе PAUSE секунд звук фразы уходит в модель; вероятность
конца хода не ниже THRESHOLD - фраза закрывается сразу, ниже - ждём нынешнюю тишину, как без модели.

Smart Turn v3 (pipecat-ai/smart-turn, BSD-2) - энкодер Whisper Tiny и линейная голова, ONNX 8 МБ, на CPU:
видеокарту не занимает. На вход - последние 8 с звука 16 кГц, признаки WhisperFeatureExtractor(chunk_length=8),
как в inference.py репозитория модели. В карточке v3.2: русский 93,5% на их тестовом наборе (1468 фраз).

  TurnEnd    решение без звука и модели: когда спрашивать модель и закрывает ли её ответ фразу
  SmartTurn  модель: звук int16 -> вероятность конца хода (нужны onnxruntime и transformers)

В слух (hearing.py) не встроено: замер на 1559 записанных фразах (turn_end_eval.py, журнал эпика,
метка voice-turn-end) - модель почти не отличает конец фразы от внутренней паузы (AUC 0,60), на паузе 0,4 с
и пороге 0,5 она закрыла бы раньше 37% фраз и разрезала бы 56%. Настройки замера: SAM_TURN_END_MODEL
(путь к .onnx), SAM_TURN_END_PAUSE, SAM_TURN_END_THRESHOLD.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import TURN_END_MODEL
from gateway.env import env_float

SAMPLE_RATE = 16_000
# Вход модели - последние 8 с звука; короче - дополняется тишиной спереди (audio_utils.py модели)
WINDOW_SECS = 8
INT16_SCALE = 32768.0
# Пауза, на которой спрашиваем модель. Замер на записях - журнал эпика, метка voice-turn-end
PAUSE = env_float("SAM_TURN_END_PAUSE", 0.4)
# Вероятность конца хода, с которой фраза закрывается. 0,5 - порог из inference.py модели; выбор - по замеру
THRESHOLD = env_float("SAM_TURN_END_THRESHOLD", 0.5)


@dataclass(frozen=True)
class TurnEnd:
    """Когда спрашивать модель и что делать с её ответом. Пауза, тишина и звук - снаружи."""

    pause: float = PAUSE
    threshold: float = THRESHOLD

    def should_ask(self, was_quiet: float, quiet: float) -> bool:
        """Тишина только что дошла до паузы: один вопрос на паузу, новая речь обнуляет тишину."""
        return was_quiet < self.pause <= quiet

    def closes(self, probability: float) -> bool:
        return probability >= self.threshold


def last_window(audio: np.ndarray, secs: int = WINDOW_SECS) -> np.ndarray:
    """Последние secs секунд звука; короче - тишина спереди, как audio_utils.truncate_audio_to_last_n_seconds."""
    size = secs * SAMPLE_RATE
    if len(audio) >= size:
        return audio[-size:]
    return np.pad(audio, (size - len(audio), 0))


class SmartTurn:
    """Модель Smart Turn v3 на CPU: звук фразы int16 16 кГц -> вероятность того, что ход закончен."""

    def __init__(self, path: Path) -> None:
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        options = ort.SessionOptions()
        # Настройки сессии из inference.py модели: один поток, последовательно - рядом Whisper и синтез
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(path), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.features = WhisperFeatureExtractor(chunk_length=WINDOW_SECS)

    def probability(self, audio: np.ndarray) -> float:
        window = last_window(audio.astype(np.float32) / INT16_SCALE)
        inputs = self.features(window, sampling_rate=SAMPLE_RATE, return_tensors="np", padding="max_length",
                               max_length=WINDOW_SECS * SAMPLE_RATE, truncation=True, do_normalize=True)
        features = inputs.input_features.astype(np.float32)
        return float(self.session.run(None, {"input_features": features})[0].reshape(-1)[0])

