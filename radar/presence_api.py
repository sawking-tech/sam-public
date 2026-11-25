"""Веб-сервис радара присутствия (SAM-105): страница, API и сокет кадров над `PresenceHub`.

Радар живёт отдельным процессом: шлюз его не хостит и не падает вместе с ним.
Шлюз зовёт функции радара `/sam/tools/*` (`presence_sam_api.py`, клиент шлюза - `gateway/radar_client.py`);
`POST /sam/presence/message` принимает сообщения страницы и скриптов (`scripts/radar_ping.py`). Хаб передаётся
в `create_app`, поэтому тесты подставляют свой, а маршруты не читают глобальное состояние.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gateway.access import AccessMiddleware
from gateway.env import env_bool

from . import presence_answers as pa
from . import presence_flow_history as pfh
from . import presence_names as pnames
from . import presence_tuning as tune
from .presence_coverage import coverage_payload
from .presence_flow_live import ALL_SECONDS, DEFAULT_RADIO_MODEL_FILE, RADIO_MODEL_ENV
from .presence_hub import PresenceHub, ZoneLocked
from .presence_radio_map import WallModel
from .presence_sam_api import add_sam_tools

STATIC_DIR = Path(__file__).parent / "static"          # presence.html, модули страницы /static/presence/*.js и three.js
# Каркас страниц SAM (шрифты, стили, навигация) общий у монитора, панели и радара: берётся из шлюза как есть
SHELL_DIR = Path(__file__).resolve().parents[1] / "gateway" / "static" / "shell"
SHELL_URL = "/static/shell"
GATEWAY_MARK = "__GATEWAY__"        # в presence.html: адрес шлюза, с которого страница берёт каркас SAM
GATEWAY_ENV, DEFAULT_GATEWAY_PORT = "SAM_SERVER", 8000
FIGURE_JOKE_ENV = "SAM_RADAR_FIGURE_JOKE"
FIGURE_JOKE_DEFAULT = False


def gateway_port() -> int:
    return urlsplit(os.getenv(GATEWAY_ENV) or "").port or DEFAULT_GATEWAY_PORT
OFF_DETAIL = pa.OFF_DETAIL
TRACK_MAX_POINTS = 20000
CLIENT_LOG_PER_MINUTE = 30     # ошибок страницы в лог не больше этого за минуту: сломанный кадр не должен забить файл
CLIENT_LOG_MAX = 600
SHOT_DIR = Path("data/screenshots")
RADIO_MODEL_FILE = Path(os.environ.get(RADIO_MODEL_ENV, DEFAULT_RADIO_MODEL_FILE))
TOLD, TOLD_OFF_DETAIL = pa.TOLD, pa.TOLD_OFF_DETAIL
NO_MODEL_DETAIL ="нет модели уровня: запусти python scripts/radar_radio_fit.py"
SHOT_MAX_BYTES = 8_000_000      # снимок страницы до восьми мегабайт
SHOT_KEEP = 30                 # хранятся последние тридцать: старые стираются
log = logging.getLogger("sam.radar")


class ZoneBody(BaseModel):
    label: str
    locked: bool = False
    x: float
    z: float
    w: float
    d: float


class PresenceMessage(BaseModel):
    channel: str = "chat"           # chat или voice
    device: str | None = None
    who: str | None = None
    level: float | None = None      # громкость фразы при речи (rms слуха)
    place: str | None = None        # для channel told: ключ места со слов человека (кровать, стол)
    pose: str | None = None         # для channel told: поза со слов (stand, sit, lie); нет - поза места


class ClientLog(BaseModel):
    kind: str = "error"
    message: str = ""
    extra: str = ""


class LockBody(BaseModel):
    locked: bool


class DebugBody(BaseModel):
    on: bool


class RealBody(BaseModel):
    on: bool


class ShotBody(BaseModel):
    image: str                      # data URL PNG из canvas страницы
    state: dict = {}                # что страница знает о себе: люди, камера, режим


class AutopilotBody(BaseModel):
    on: bool
    period_s: float | None = None
    mode: str | None = None                  # routine - быт, random - случайные места


class PersonBody(BaseModel):
    person: str
    confidence: float
    place: str | None = None        # ключ точки плана, куда поставить; пусто - где радар уже видит человека


class PlaceBody(BaseModel):
    name: str
    place: str = ""


def create_app(hub: PresenceHub, clock: Callable[[], float] = time.time) -> FastAPI:
    """clock - часы ограничителя ошибок страницы; тесты подают свои."""
    app = FastAPI(title="SAM radar")
    app.add_middleware(AccessMiddleware)

    def require_enabled() -> None:
        if not hub.enabled:
            raise HTTPException(status_code=409, detail=OFF_DETAIL)

    @app.on_event("startup")
    async def _start() -> None:
        if hub.enabled:
            asyncio.create_task(hub.run(), name="presence")
            print(f"[Presence] радар включён: {hub.mode}, планировка {hub.layout.get('name')}")

    @app.get("/health")
    async def health():
        return {"status": "ok", "enabled": hub.enabled, "mode": hub.mode}

    @app.get("/")
    @app.get("/presence")
    async def page(request: Request):
        # каркас страниц SAM - со шлюза: хост тот, с которого открыт радар, порт - шлюза (SAM_SERVER)
        html = (STATIC_DIR / "presence.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(GATEWAY_MARK, f"//{request.url.hostname}:{gateway_port()}"))

    @app.get("/sam/presence")
    async def snapshot():
        return {"layout": hub.layout if hub.enabled else None, **hub.snapshot()}

    @app.post("/sam/presence/zone")
    async def zone_add(body: ZoneBody):
        require_enabled()
        try:
            return hub.add_zone(body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    @app.delete("/sam/presence/zone/{zone_id}")
    async def zone_remove(zone_id: str):
        try:
            removed = hub.remove_zone(zone_id)
        except ZoneLocked:
            raise HTTPException(status_code=423, detail="зона логическая, стирать нельзя: сначала сними замок")
        if not removed:
            raise HTTPException(status_code=404, detail=f"зоны {zone_id} нет")
        return {"ok": True}

    @app.post("/sam/presence/zone/{zone_id}/lock")
    async def zone_lock(zone_id: str, body: LockBody):
        if not hub.set_zone_lock(zone_id, body.locked):
            raise HTTPException(status_code=404, detail=f"зоны {zone_id} нет")
        return {"ok": True, "locked": body.locked}

    @app.get("/sam/presence/zones")
    async def zones_list():
        return {"zones": hub.zones}

    @app.get("/sam/presence/track")
    async def track(name: str, minutes: float = 30.0, limit: int = 2000):
        """Ряд сигнала устройства по части имени за последние минуты: как его носили по квартире."""
        require_enabled()
        since = time.time() - max(0.0, minutes) * 60
        rows = hub.tracks.read(name, since, max(1, min(limit, TRACK_MAX_POINTS)))
        return {"name": name, "count": len(rows), "points": rows}

    @app.post("/sam/presence/message")
    async def message(body: PresenceMessage):
        """Сообщение с именем и устройством для радара: чат, голос, хук сессии Claude Code на компьютере человека."""
        require_enabled()
        who, place = body.who, body.place
        if body.channel == TOLD and not body.device:                  # слова SAM: место и человек должны быть в разметке, иначе пропали бы молча; с устройством - отметка отпечатка (radar_mark_place), имя места свободное
            try:
                who, place = pa.resolve_told(body.who or "", body.place or "", body.pose, hub.place_points(),
                                             pnames.people_index(hub.layout.get("people") or {}))
            except pa.Rejected as e:
                raise HTTPException(status_code=422, detail=str(e))
        applied = hub.note_message(body.channel, body.device, who, body.level, place=place, pose=body.pose)
        return {"ok": True, "applied": applied, **({} if applied else {"detail": TOLD_OFF_DETAIL})}

    @app.get("/sam/presence/places")
    async def places():
        """Разметка словами для SAM: комнаты, места плана с комнатой и позой (имя места - ключ для слов «со слов»), люди."""
        require_enabled()
        return pa.places(hub.layout, hub.place_points(), hub.enabled_channels)

    @app.get("/sam/presence/walks")
    async def walks():
        """Размеченные прогулки для показа трека на полу (точки маршрута со временем)."""
        return {"walks": hub.walks() if hub.enabled else []}

    @app.post("/sam/presence/place")
    async def place(body: PlaceBody):
        require_enabled()
        try:
            hub.set_place(body.name, body.place)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "places": hub.devices.places}

    @app.post("/sam/presence/person")
    async def person_set(body: PersonBody):
        """Отладка: вероятность человека ползунком; 0 убирает человека, место необязательно."""
        require_enabled()
        try:
            hub.set_person(body.person, body.confidence, body.place)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "overrides": hub.overrides}

    @app.delete("/sam/presence/person/{person}")
    async def person_clear(person: str):
        if not hub.clear_person(person):
            raise HTTPException(status_code=404, detail=f"ручной вероятности для {person} нет")
        return {"ok": True}

    recent_client_logs: list[float] = []

    @app.post("/sam/presence/client-log")
    async def client_log(body: ClientLog):
        """Ошибка в странице радара (исключение, отказ запроса): одной строкой в лог сервиса, с ограничением частоты."""
        now = clock()
        recent_client_logs[:] = [t for t in recent_client_logs if now - t < 60]
        if len(recent_client_logs) >= CLIENT_LOG_PER_MINUTE:
            return {"ok": False}
        recent_client_logs.append(now)
        log.warning("страница %s: %s | %s", body.kind[:20], body.message[:CLIENT_LOG_MAX], body.extra[:CLIENT_LOG_MAX])
        return {"ok": True}

    @app.get("/sam/presence/autopilot")
    async def autopilot_get():
        return {"on": hub.autopilot_on, "period_s": hub.autopilot_period_s, "mode": hub.autopilot_mode}

    @app.post("/sam/presence/autopilot")
    async def autopilot_set(body: AutopilotBody):
        """Отладка: автопилот случайных маршрутов для всех, чтобы подбирать параметры анимации."""
        require_enabled()
        return hub.set_autopilot(body.on, body.period_s, body.mode)

    @app.get("/sam/presence/figure")
    async def figure():
        """Настройка фигуры для страницы: joke - показывать ли шутку (radar/static/presence/figure.js)."""
        return {"joke": env_bool(FIGURE_JOKE_ENV, FIGURE_JOKE_DEFAULT)}

    @app.get("/sam/presence/debug")
    async def debug_get():
        return {"on": hub.debug}

    @app.post("/sam/presence/debug")
    async def debug_set(body: DebugBody):
        """Режим отладки: подробные строки состояния людей в лог радара."""
        return {"on": hub.set_debug(body.on)}

    @app.delete("/sam/presence/heat")
    async def heat_clear():
        """Очистка накопленной карты посещений (кнопка в панели отладки)."""
        require_enabled()
        return {"cleared": hub.clear_heat()}

    @app.get("/sam/presence/coverage")
    async def coverage(model: str | None = None):
        """Карта покрытия сети: ожидаемый уровень роутера по клеткам плана для выбранной пары «устройство и диапазон» и места плана по убыванию уровня.

        Диапазонов два (2,4 и 5 ГГц), и у каждого устройства своя модель; в ответе список моделей и кто сейчас на каком интерфейсе."""
        require_enabled()
        if not RADIO_MODEL_FILE.exists():
            raise HTTPException(status_code=404, detail=NO_MODEL_DETAIL)
        models = json.loads(RADIO_MODEL_FILE.read_text(encoding="utf8"))["models"]
        if not models:
            raise HTTPException(status_code=404, detail=NO_MODEL_DETAIL)
        key = model if model in models else next(iter(models))
        fitted = models[key]                                          # модель диапазона: ключ - интерфейс роутера
        wall_model = WallModel(fitted["l0_db"], fitted["n"], fitted["wall_db_per_m"], fitted.get("near_gain_db", 0.0))
        return {**coverage_payload(wall_model, hub.layout), "model": key, "band": fitted["band"], "iface": fitted["iface"],
                "models": [{"key": k, "label": m["band"], "iface": m["iface"]} for k, m in models.items()],
                "connected": hub.connected_devices()}

    @app.get("/sam/presence/walk_fields")
    async def walk_fields(model: str | None = None):
        """Поля шага к роутеру: из каждой клетки потока k секунд по полу к роутеру, куда пришёл и на сколько изменился ожидаемый уровень, k от 1 до окна."""
        require_enabled()
        if hub.flow is None:
            raise HTTPException(status_code=404, detail="поток выключен: SAM_PRESENCE_FLOW=1")
        if not RADIO_MODEL_FILE.exists():
            raise HTTPException(status_code=404, detail=NO_MODEL_DETAIL)
        models = json.loads(RADIO_MODEL_FILE.read_text(encoding="utf8"))["models"]
        if not models:
            raise HTTPException(status_code=404, detail=NO_MODEL_DETAIL)
        key = model if model in models else next(iter(models))
        return {**await asyncio.to_thread(hub.flow.walk_fields, models[key]), "model": key}

    @app.get("/sam/presence/stay")
    async def stay():
        """Сколько каждый человек уже на одном месте и пора ли размяться (больше часа): отсюда SAM берёт «прогуляйся»."""
        return {"people": hub.stay_report() if hub.enabled else []}

    @app.get("/sam/presence/flow")
    async def flow(detail: bool = False, device: str | None = None, second: str | None = None, at: float | None = None):
        """Поток положения: клетки мира, у каждого устройства распределение, список мест с вероятностями и пятна; выключен флагом - enabled: false.
        detail=true добавляет срезы окна с вкладом факторов, пути, точку и прошлую точку для слоя объяснения, device=id|диапазон - только этому устройству.
        at=секунда unix - перемотка: состояние, пересчитанное из треков за минуту до этой секунды, живой поток не трогается."""
        require_enabled()
        if second is not None and second != "all" and not second.isdigit():
            raise HTTPException(status_code=422, detail="second: номер секунды окна с нуля или all")
        wanted = None if second is None else (ALL_SECONDS if second == "all" else int(second))
        if at is not None:
            return hub.rewind.state(at, detail, device, wanted) if hub.rewind is not None else {"enabled": False, "at": int(at)}
        return hub.flow.state(detail, device, wanted) if hub.flow is not None else {"enabled": False}

    @app.get("/sam/presence/flow/history")
    async def flow_history(since: float, until: float, subject: str | None = None, min_sure: float = 0.0):
        """Куда поток ставил субъектов по секундам за отрезок unix-времени: точка, места с вероятностями, уверенность; subject - часть ключа или имени, min_sure - порог уверенности верхнего места."""
        require_enabled()
        return {"rows": hub.flow_history.read_range(since, until, subject, min_sure)}

    @app.get("/sam/presence/flow/heat")
    async def flow_heat(since: float, until: float, subject: str | None = None, min_sure: float = pfh.SURE_SHARE):
        """Тепловая карта за отрезок: секунды по клеткам пола («столбец,ряд» в клетках cell_m) из уверенных строк истории потока; для SAM и обучения приоритетов."""
        require_enabled()
        return {"cell_m": pfh.HEAT_CELL_M, "cells": pfh.heat(hub.flow_history.read_range(since, until, subject), min_sure=min_sure)}

    @app.get("/sam/presence/rewind/span")
    async def rewind_span():
        """Границы записи треков для ползунка перемотки: первая и последняя секунда; записи или перемотки нет - enabled: false."""
        require_enabled()
        span = hub.rewind.span() if hub.rewind is not None else None
        return {"enabled": False} if span is None else {"enabled": True, "from": span[0], "to": span[1]}

    @app.get("/sam/presence/fingerprints")
    async def fingerprints():
        """Накопленные отпечатки: у каждого устройства по названным местам средний уровень сигнала и разброс."""
        return {"fingerprints": hub.fingerprints.summary()}

    @app.delete("/sam/presence/fingerprints")
    async def fingerprints_forget(place: str, device: str | None = None):
        """Стереть ошибочно названное место (у одного устройства по части имени или у всех)."""
        require_enabled()
        return {"removed": hub.forget_fingerprint(place, device)}

    @app.get("/sam/presence/real")
    async def real_get():
        return {"on": hub.real, "channels": sorted(hub.enabled_channels)}

    @app.post("/sam/presence/real")
    async def real_set(body: RealBody):
        """Тест реального времени: все каналы, без автопилота и ручных вероятностей, подробный журнал состояния людей."""
        require_enabled()
        return {"on": hub.set_real(body.on), "channels": sorted(hub.enabled_channels)}

    @app.post("/sam/presence/screenshot")
    async def screenshot(body: ShotBody):
        """Снимок страницы с состоянием: PNG и JSON рядом в data/screenshots, чтобы по ним можно было разобрать, что видит человек."""
        head, _, data = body.image.partition(",")
        if not head.startswith("data:image/png"):
            raise HTTPException(status_code=422, detail="нужен PNG в виде data URL")
        raw = base64.b64decode(data)
        if len(raw) > SHOT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="снимок больше восьми мегабайт")
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        png = SHOT_DIR / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        png.write_bytes(raw)
        png.with_suffix(".json").write_text(json.dumps({"state": body.state, "debug": hub.debug, "identities": hub.identities(time.time())}, ensure_ascii=False, indent=1), encoding="utf-8")
        for stale in sorted(SHOT_DIR.glob("shot-*.png"))[:-SHOT_KEEP]:
            stale.unlink(missing_ok=True)
            stale.with_suffix(".json").unlink(missing_ok=True)
        log.info("снимок страницы сохранён: %s", png)
        return {"ok": True, "path": str(png)}

    @app.get("/sam/presence/where")
    async def where(person: str = ""):
        """Где кто словами: комната, подписанная зона, мебель рядом, поза и уверенность."""
        require_enabled()
        return {"people": hub.where(person)}

    @app.get("/sam/presence/tuning")
    async def tuning_get():
        return {"tuning": hub.tuning.to_dict(), "knobs": tune.knobs_payload()}

    @app.post("/sam/presence/tuning")
    async def tuning_set(body: dict[str, float]):
        """Пороги детектора с ползунков панели отладки; действуют сразу."""
        require_enabled()
        try:
            return {"tuning": hub.set_tuning(body)}
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    @app.delete("/sam/presence/tuning")
    async def tuning_reset():
        require_enabled()
        return {"tuning": hub.reset_tuning()}

    @app.get("/sam/presence/animation")
    async def animation_get():
        return {"values": hub.animation}

    @app.post("/sam/presence/animation")
    async def animation_set(body: dict[str, float]):
        """Параметры анимации роя с ползунков страницы: сохраняются сразу, изменения - в журнал."""
        require_enabled()
        try:
            return {"values": hub.set_animation(body)}
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    @app.delete("/sam/presence/animation")
    async def animation_reset():
        require_enabled()
        return {"values": hub.reset_animation()}

    @app.websocket("/ws/presence")
    async def frames(ws: WebSocket):
        await ws.accept()
        await ws.send_text(json.dumps(hub.hello(), ensure_ascii=False))
        if not hub.enabled:
            await ws.close()
            return
        q = hub.add_client()
        try:
            while True:
                await ws.send_text(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.remove_client(q)

    add_sam_tools(app, hub, lambda: datetime.fromtimestamp(clock()))     # ручки SAM: app_call(radar, ...)
    app.mount(SHELL_URL, StaticFiles(directory=SHELL_DIR), name="shell")         # раньше /static: монтирования сверяются по порядку
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")      # после маршрутов: монтирование ловит всё под /static
    return app
