"""Цепочка субъектов потока положения (SAM-118): порядок шага. Телефон считается первым (у него первичный признак), человек от телефона, кот от человека.

Цепочка только вызывает субъектов по порядку; кто на кого опирается, задают их свидетельства (связь с другим субъектом берёт его итог последнего шага).
Модуль чистый."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .presence_subject import Estimate, Subject


class Chain:
    def __init__(self, subjects: Sequence[Subject]) -> None:
        names = [subject.name for subject in subjects]
        if len(set(names)) != len(names):
            raise ValueError(f"в цепочке повторяются субъекты: {names}")
        self._subjects = tuple(subjects)

    def tick(self, now: datetime) -> dict[str, Estimate]:
        """Один шаг всех субъектов по порядку цепочки."""
        return {subject.name: subject.step(now) for subject in self._subjects}
