"""REST анкеты дома для монитора: список, запись и удаление людей и питомцев (gateway/household.py).

Записи не секретны, поэтому ключ привилегий не нужен: доступ решает общая проверка шлюза (gateway/access.py).
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import household

PREFIX = "/sam/household"
BAD_REQUEST, NOT_FOUND, UNAVAILABLE = 400, 404, 503
NO_STORE = "хранилище недоступно (Mongo), проверь /health"


class Member(BaseModel):
    kind: str = household.PERSON
    name: str
    role: str = ""
    about: str = ""


def make_router(db: Callable, changed: Callable[[], None] = lambda: None) -> APIRouter:
    """db() - база шлюза в момент запроса, None - Mongo нет; changed() - состав изменился, блок промпта читается заново."""
    router = APIRouter(prefix=PREFIX)

    def store():
        found = db()
        if found is None:
            raise HTTPException(status_code=UNAVAILABLE, detail=NO_STORE)
        return found

    @router.get("")
    def members():
        return {"members": household.members(store())}

    @router.put("")
    def save(member: Member):
        try:
            saved = household.save(store(), member.model_dump())
        except household.HouseholdError as exc:
            raise HTTPException(status_code=BAD_REQUEST, detail=str(exc)) from exc
        changed()
        return saved

    @router.delete("/{kind}/{name}")
    def remove(kind: str, name: str):
        if not household.remove(store(), kind, name):
            raise HTTPException(status_code=NOT_FOUND, detail=f"{name}: нет такой записи")
        changed()
        return {"removed": name}

    return router
