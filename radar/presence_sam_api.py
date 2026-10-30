"""Ручки радара для SAM: шесть маршрутов с меткой x-sam-tool, по стандарту живых приложений (`gateway/app_tools.py`).

SAM зовёт их инструментом `app_call(radar, where, {person})`: шлюз читает OpenAPI радара, берёт методы с меткой и
вызывает их на адресе радара с ключом доступа. Имя ручки - operationId, аргументы - строка запроса у GET и JSON-тело
у POST. Ответ - текст для модели: JSON словами или одна строка, что не так (код 409 - радар выключен, 422 - слова не
легли на разметку). MCP-переходник `mcp/radar_server.py` для моделей вне шлюза зовёт эти же маршруты.

Функция `note` (свидетельство каналов SAM: чат и голос дают имя и устройство) метки не имеет: модель её не видит, её
зовёт клиент радара шлюза (`gateway/radar_client.py`). Остальные маршруты радара (`radar/presence_api.py`) служат
странице и интеграциям, метки у них нет.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from gateway.app_tools import TOOL_MARK

from . import presence_answers as pa
from . import presence_names as pnames

if TYPE_CHECKING:
    from .presence_hub import PresenceHub

PREFIX = "/sam/tools"
WHERE, HISTORY, LAYOUT, TELL, DEVICES, FURNITURE = "where", "history", "layout", "tell", "devices", "furniture"
NOTE = "note"                  # без метки: функция каналов SAM, не инструмент модели
NOTE_TOLD_DETAIL = "«со слов» - функция tell: она сверяет человека и место с разметкой"
# Описание ручки - для модели: его видят app_list, app_call и инструменты MCP-переходника
DESCRIPTIONS = {
    WHERE: "Что я вижу сейчас: где люди дома - комната, подписанная зона, мебель рядом, поза и уверенность",
    HISTORY: "Где я видела человека в прошлый момент: место, его вероятность и откуда знаю; записи нет - «нет данных»",
    LAYOUT: "Разметка моей квартиры словами: комнаты, места (имя, комната, поза) и люди; имя места отсюда принимает tell",
    TELL: ("Вношу в свою картину квартиры, где человек со слов: он сам сказал или я услышала в разговоре; только "
           "сказанное, не догадки"),
    DEVICES: "Устройства дома, которые я вижу: на связи ли, уровень сигнала, где лежат и насколько близко к роутеру",
    FURNITURE: "Мебель и предметы плана моей квартиры с координатами и размерами в метрах",
}
PERSON_ANY = "часть имени или ключа человека, пусто - все"
PERSON = "имя или ключ человека из разметки"
AT = f"момент, местное время: {pa.AT_HINT}"
PLACE = "имя места из layout"
POSE = "stand, sit или lie; пусто - поза места"
ROOM = "часть названия комнаты, пусто - вся квартира"
REJECTED, OFF = 422, 409


class TellBody(BaseModel):
    person: str = Field(description=PERSON)
    place: str = Field(description=PLACE)
    pose: str = Field("", description=POSE)


class NoteBody(BaseModel):
    channel: str
    device: str | None = None
    who: str | None = None
    level: float | None = None
    place: str | None = None


def _json(data) -> PlainTextResponse:
    return PlainTextResponse(json.dumps(data, ensure_ascii=False))


def _refuse(text: str, status: int = REJECTED) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=status)


def add_sam_tools(app: FastAPI, hub: PresenceHub, now: Callable[[], datetime]) -> None:
    """Ручки на приложение радара; now - часы разбора «ЧЧ:ММ» в history, тесты подают свои."""

    def tool(method: str, name: str):
        return getattr(app, method)(f"{PREFIX}/{name}", operation_id=name, summary=DESCRIPTIONS[name],
                                    response_class=PlainTextResponse, openapi_extra={TOOL_MARK: True})

    def off() -> PlainTextResponse | None:
        return None if hub.enabled else _refuse(pa.OFF_DETAIL, OFF)

    def people() -> dict[str, str]:
        return pnames.people_index(hub.layout.get("people") or {})

    @tool("get", WHERE)
    async def where(person: str = Query("", description=PERSON_ANY)):
        return off() or _json(hub.where(person))

    @tool("get", HISTORY)
    async def history(person: str = Query(description=PERSON), at: str = Query(description=AT)):
        if refused := off():
            return refused
        moment = pa.parse_at(at, now())
        if moment is None:
            return _refuse(f"время «{at}» не разобрала: {pa.AT_HINT}")
        try:
            key = pa.person_key(person, people())
        except pa.Rejected as e:
            return _refuse(str(e))
        since, until = pa.history_window(moment)
        rows = hub.flow_history.read_range(since, until, pa.PERSON_SUBJECT + key)
        return _json(pa.describe_row(pa.pick_row(rows, key, moment.timestamp()), key, moment))

    @tool("get", LAYOUT)
    async def layout():
        return off() or _json(pa.places(hub.layout, hub.place_points(), hub.enabled_channels))

    @tool("post", TELL)
    async def tell(body: TellBody):
        if refused := off():
            return refused
        pose = body.pose or None
        try:
            who, place = pa.resolve_told(body.person, body.place, pose, hub.place_points(), people())
        except pa.Rejected as e:
            return _refuse(str(e))
        applied = hub.note_message(pa.TOLD, None, who, None, place=place, pose=pose)
        return PlainTextResponse(pa.told_answer(applied, body.person, body.place, pose))

    @app.post(f"{PREFIX}/{NOTE}", operation_id=NOTE, response_class=PlainTextResponse)
    async def note(body: NoteBody):
        """Свидетельство канала SAM: кто и с какого устройства; вес по каналу и громкости решает хаб."""
        if refused := off():
            return refused
        if body.channel == pa.TOLD:
            return _refuse(NOTE_TOLD_DETAIL)
        applied = hub.note_message(body.channel, body.device, body.who, body.level, place=body.place)
        return _json({"applied": applied})

    @tool("get", DEVICES)
    async def devices():
        return off() or _json(hub.snapshot().get("devices", []))

    @tool("get", FURNITURE)
    async def furniture(room: str = Query("", description=ROOM)):
        return off() or _json(pa.furniture(hub.layout, room))
