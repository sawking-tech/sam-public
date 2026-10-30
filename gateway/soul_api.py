"""REST души SAM для монитора и ведущего: список разделов, чтение, история, запись и откат (gateway/soul_service.py).

Душа - характер SAM (gateway/soul_service.py): инструкции SAM.md и личный слой data/SAM.md здесь не отдаются и не
правятся. Список и разделы открыты тем, кого пускает доступ шлюза (gateway/access.py). Запись и откат требуют одно из
двух: ключ привилегий (заголовок X-SAM-Privilege, gateway/privilege_key.py, как /sam/main-model) - его держат ведущий
и MCP sam; или ключ команд (SAM_COMMAND_SECRET) - в поле secret тела записи; его называет человек в
мониторе, как называет голосом или в чате для привилегированного инструмента. Монитор ключа привилегий не держит:
страница его не получает, иначе он ушёл бы любой странице этой машины. Ключ команд сверяется так же, как во фразе
(gateway/secret_mask.py), и нигде не сохраняется. Неверный ключ команд MAX_SECRET_FAILURES раз за SECRET_WINDOW_S -
ответ 429 на SECRET_BLOCK_S и строка в лог: перебором его не подобрать. Решения (authorize, SecretLimiter) отделены
от протокола и проверяются без сети.
"""
from __future__ import annotations

import hmac
import time
from collections.abc import Callable, Mapping

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from . import privilege_key
from .secret_mask import COMMAND_PURPOSE, load_secrets, mask

PREFIX = "/sam/soul"
LEAD_AUTHOR = "lead"     # запись с ключом привилегий: ведущий, MCP sam
OWNER_AUTHOR = "owner"   # запись с ключом команд: человек в мониторе
NEED_KEY = ("нужен ключ: заголовок X-SAM-Privilege (ведущий, MCP sam) или ключ команд в поле secret "
            "(монитор спросит его при сохранении)")
# Пять неверных ключей за десять минут - пауза десять минут: человек, ошибившийся пару раз, не замечает её, а перебор
# шестизначного ключа (миллион вариантов) по 5 попыток в 20 минут растягивается на годы
MAX_SECRET_FAILURES = 5
SECRET_WINDOW_S = 600
SECRET_BLOCK_S = 600
TOO_MANY = "слишком много неверных ключей: запись души закрыта на {minutes} мин"
LOG_TAG = "[Soul]"
FORBIDDEN, NOT_FOUND, CONFLICT, TOO_MANY_REQUESTS, UNAVAILABLE = 403, 404, 409, 429, 503


def lead_key_ok(presented: str | None, key: str) -> bool:
    return bool(presented and key and hmac.compare_digest(presented.encode(), key.encode()))


def owner_secret_ok(secret: str | None, secrets: Mapping[str, str]) -> bool:
    return bool(secret) and COMMAND_PURPOSE in mask(secret, secrets).names


def authorize(presented: str | None, key: str, secret: str | None, secrets: Mapping[str, str]) -> str | None:
    """Автор записи (lead или owner) или None - ключа нет или он не тот."""
    if lead_key_ok(presented, key):
        return LEAD_AUTHOR
    return OWNER_AUTHOR if owner_secret_ok(secret, secrets) else None


class SecretLimiter:
    """Счёт неверных ключей команд: max_failures за window_s - закрыто на block_s."""

    def __init__(self, max_failures: int = MAX_SECRET_FAILURES, window_s: float = SECRET_WINDOW_S,
                 block_s: float = SECRET_BLOCK_S, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print, subject: str = "запись души", tag: str = LOG_TAG):
        self._max, self._window, self._block, self._clock, self._log = max_failures, window_s, block_s, clock, log
        self.subject, self._tag = subject, tag
        self._failures: list[float] = []
        self._until = 0.0

    def blocked(self) -> bool:
        return self._clock() < self._until

    def fail(self) -> None:
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < self._window] + [now]
        if len(self._failures) >= self._max:
            self._until, self._failures = now + self._block, []
            self._log(f"{self._tag} {self._max} неверных ключей за {self._window / 60:.0f} мин: "
                      f"{self.subject} закрыта на {self._block / 60:.0f} мин")


class SoulWrite(BaseModel):
    text: str
    expected_version: int   # версия, которую видел автор: раздел изменился с тех пор - 409
    secret: str = ""


class SoulRollback(BaseModel):
    version: int | None = None
    expected_version: int | None = None
    secret: str = ""


def make_router(service: Callable, key: Callable[[], str] = privilege_key.key,
                secrets: Callable[[], Mapping[str, str]] = load_secrets,
                limiter: SecretLimiter | None = None) -> APIRouter:
    """service() - сервис души шлюза; берётся в момент запроса, ключ и секреты - тоже."""
    router = APIRouter(prefix=PREFIX)
    limiter = limiter or SecretLimiter()

    def ready():
        svc = service()
        if not svc.available:
            raise HTTPException(status_code=UNAVAILABLE, detail="хранилище души недоступно (Mongo)")
        return svc

    def author(presented: str | None, secret: str | None) -> str:
        if lead_key_ok(presented, key()):
            return LEAD_AUTHOR
        if limiter.blocked():
            raise HTTPException(status_code=TOO_MANY_REQUESTS, detail=TOO_MANY.format(minutes=round(SECRET_BLOCK_S / 60)))
        if owner_secret_ok(secret, secrets()):
            return OWNER_AUTHOR
        if secret:
            limiter.fail()
        raise HTTPException(status_code=FORBIDDEN, detail=NEED_KEY)

    def answer(result) -> dict:
        if not result.ok:
            raise HTTPException(status_code=CONFLICT, detail=result.message)
        return {"ok": True, "message": result.message, "version": result.version}

    @router.get("")
    def list_sections():
        return {"sections": ready().entries()}

    @router.get("/{name}")
    def read_section(name: str):
        doc = ready().show(name)
        if not doc:
            raise HTTPException(status_code=NOT_FOUND, detail=f"нет раздела {name}")
        return doc

    @router.get("/{name}/history")
    def section_history(name: str):
        versions = ready().history(name)
        if not versions:
            raise HTTPException(status_code=NOT_FOUND, detail=f"нет раздела {name}")
        return {"name": name, "versions": versions}

    @router.put("/{name}")
    def write_section(name: str, body: SoulWrite,
                      x_sam_privilege: str | None = Header(default=None)):
        who = author(x_sam_privilege, body.secret)
        return answer(ready().write(name, body.text, who, body.expected_version))

    @router.post("/{name}/rollback")
    def rollback_section(name: str, body: SoulRollback,
                         x_sam_privilege: str | None = Header(default=None)):
        who = author(x_sam_privilege, body.secret)
        return answer(ready().rollback(name, body.version, who, body.expected_version))

    return router
