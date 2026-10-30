"""Загрузка модели эмбеддингов: сначала из кэша, в сеть - только когда модели в кэше нет.

Без этого каждый старт эмбеддера ходил на Hugging Face без токена («unauthenticated requests to the HF Hub»), хотя
модель давно лежит в кэше; на новой машине первый старт по-прежнему скачивает её. Отчёт загрузки transformers
(«position_ids UNEXPECTED» - лишний ключ в весах этой модели, на результат не влияет) на время загрузки приглушён
до ошибок.
"""
from __future__ import annotations

from typing import Any, Callable


def _quiet_transformers() -> Callable[[], None]:
    """Приглушить логи transformers до ошибок; вернуть, как вернуть прежний уровень."""
    try:
        from transformers.utils import logging as tf_logging
    except ImportError:
        return lambda: None
    level = tf_logging.get_verbosity()
    tf_logging.set_verbosity_error()
    return lambda: tf_logging.set_verbosity(level)


def load_sentence_model(name: str, factory: Callable[..., Any] | None = None) -> Any:
    """SentenceTransformer по имени: из кэша, при его отсутствии - со скачиванием."""
    if factory is None:
        from sentence_transformers import SentenceTransformer
        factory = SentenceTransformer
    restore = _quiet_transformers()
    try:
        try:
            return factory(name, local_files_only=True)
        except OSError:                  # модели нет в кэше - первый запуск на этой машине
            return factory(name)
    finally:
        restore()
