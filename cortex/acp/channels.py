"""Каналы агента Cortex к создателю (ADR-022, SAM-133 этапы 2, 4 и 5): вопрос, статус и запрос права - при любых правах.

Сессия с создателем (`_meta.creator`) получает свои инструменты `ask_creator`, `report_status`, `request_permission` и
`send_to_agent`, а с правом на детей (`depth` больше 0) - ещё `spawn_agent` и `grant_child`: вызов уходит клиенту ACP
расширением `_sam/agent_event`, клиент SAM обрабатывает его теми же функциями, что у воркеров шлюза. Ответ создателя на
вопрос приходит в идущий ход инъекцией (`_sam/inject`); право создатель выдаёт явно (`_sam/grant`), инструмент виден со
следующего шага. Модуль без сети: схемы, проверки по правам сессии и вызов через переданное соединение.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from .rights import PEER_PREFIX, Rights, beyond

CHANNEL_METHOD = "_sam/agent_event"
ASK, STATUS, PERMISSION, SEND = "ask_creator", "report_status", "request_permission", "send_to_agent"
SPAWN, GRANT_CHILD = "spawn_agent", "grant_child"
BASE_NAMES = (ASK, STATUS, PERMISSION, SEND)
CHILD_NAMES = (SPAWN, GRANT_CHILD)   # только у сессии с правом на детей
NAMES = BASE_NAMES + CHILD_NAMES
KIND = {ASK: "ask", STATUS: "status", PERMISSION: "permission", SEND: "send", SPAWN: "spawn", GRANT_CHILD: "grant_child"}
SPAWNED_KEY = "spawned"   # в ответе клиента на spawn: ребёнок создан
LIST_SEPARATOR = ","   # список инструментов ребёнку - строкой через запятую: параметры каналов строковые
# Параметры каждого канала: (имя, описание); первый обязателен
_PARAMS = {
    ASK: [("question", "Что спросить у создателя, если без ответа не обойтись")],
    STATUS: [("message", "Короткий статус создателю: что делаешь, где застрял; итог - с ОТВЕТ: в начале")],
    PERMISSION: [("tool", "Имя инструмента вне выдачи или agent:<id> - право писать агенту"), ("why", "Зачем он нужен")],
    SEND: [("to", "id задачи агента из твоего списка собеседников"), ("message", "Что передать")],
    SPAWN: [("description", "Что сделать агенту-ребёнку"),
            ("tools", "Инструменты ребёнку через запятую, только из твоей выдачи"),
            ("level", "Метка сложности: smart, medium или weak; пусто - medium")],
    GRANT_CHILD: [("task_id", "id задачи твоего ребёнка"), ("tool", "Инструмент из твоей выдачи или agent:<id> из твоих собеседников")],
}
_DESC = {ASK: "Спросить создателя задачи. Ответ придёт следующим сообщением; пока делай то, что можно без него.",
         STATUS: "Сообщить создателю статус или итог (ОТВЕТ: ...).",
         PERMISSION: "Попросить у создателя право на инструмент вне выдачи; выдаст - инструмент появится со следующего шага.",
         SEND: "Написать агенту из списка собеседников, который дал создатель; другим - через request_permission(agent:<id>).",
         SPAWN: "Создать агента-ребёнка на часть задачи, с правами не шире своих; его итог придёт тебе [TASK_DONE].",
         GRANT_CHILD: "Выдать своему ребёнку право по его запросу; чего у тебя нет - попроси у своего создателя."}
NOT_A_PEER = "Агента {to} нет в твоём списке собеседников: попроси право request_permission(tool=agent:{to})"
WIDER_THAN_OWN = "Ребёнку нельзя шире твоих прав: {names} нет в твоей выдаче; попроси их request_permission"
NO_TOOLS = "Нужны tools: инструменты ребёнку из твоей выдачи"
UNKNOWN = "Инструмента {name} нет"
PEERS_HEADER = " Собеседники: "
NO_NOTE = "без описания"
NOT_OWN = "{tool} нет в твоих правах: попроси его у своего создателя request_permission, потом выдай ребёнку"

Ext = Callable[[str, dict], Awaitable[dict]]


def names(rights: Rights) -> tuple[str, ...]:
    """Каналы сессии: детей создаёт только сессия с правом на них, пока не исчерпала число детей; у сессии без права
    каналов детей нет вовсе (ADR-022: агент не знает, что мог бы больше)."""
    if rights.depth <= 0:
        return BASE_NAMES
    return BASE_NAMES + ((SPAWN,) if rights.children > 0 else ()) + (GRANT_CHILD,)


def _description(name: str, rights: Rights) -> str:
    """У send_to_agent - список собеседников: кто и чем занят, чтобы агент сам решил, кому писать."""
    if name != SEND or not rights.peers:
        return _DESC[name]
    notes = dict(rights.peer_notes)
    return _DESC[name] + PEERS_HEADER + "; ".join(f"{p} - {notes.get(p) or NO_NOTE}" for p in rights.peers)


def schemas(rights: Rights) -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": _description(name, rights),
        "parameters": {"type": "object",
                       "properties": {p: {"type": "string", "description": d} for p, d in _PARAMS[name]},
                       "required": [_PARAMS[name][0][0]]}}} for name in names(rights)]


def registry(rights: Rights) -> dict[str, list[dict]]:
    """Параметры для разбора вызова из текста (tool_text_parse), как McpTools.registry."""
    return {name: [{"name": p, "type": "string", "required": i == 0} for i, (p, _) in enumerate(_PARAMS[name])]
            for name in names(rights)}


def _split(value: str) -> list[str]:
    return [t.strip() for t in value.split(LIST_SEPARATOR) if t.strip()]


def _owns(tool: str, rights: Rights) -> bool:
    """Право есть у самого агента: инструмент в выдаче или собеседник в списке."""
    if tool.startswith(PEER_PREFIX):
        return tool[len(PEER_PREFIX):] in rights.peers
    return not beyond([tool], rights)


def _check(name: str, values: dict, rights: Rights) -> tuple[str, dict]:
    """Проверка вызова по правам сессии: (отказ модели, добавка к событию); отказа нет - пустая строка."""
    if name == SEND and values["to"] not in rights.peers:   # писать - только своим (ADR-022, «Кому писать»)
        return NOT_A_PEER.format(to=values["to"]), {}
    if name == SPAWN:
        tools = _split(values["tools"])
        if not tools:
            return NO_TOOLS, {}
        if wider := beyond(tools, rights):
            return WIDER_THAN_OWN.format(names=", ".join(wider)), {}
        return "", {"tools": tools, "depth": rights.depth - 1}
    if name == GRANT_CHILD and not _owns(values["tool"], rights):
        return NOT_OWN.format(tool=values["tool"]), {}
    return "", {}


def impl(ext: Ext, session_id: str, rights: Callable[[], Rights] = Rights,
         on_children: Callable[[int], None] = lambda delta: None) -> dict[str, Callable[[dict], Awaitable[str]]]:
    """Вызовы каналов: имя -> async (args) -> текст для модели. rights() - текущие права сессии; on_children(d) -
    число детей меняется на d: место занимается до вызова клиента и возвращается, если ребёнок не создан. Каналов
    детей у сессии без права на них нет и среди вызовов."""
    def make(name: str):
        async def call(args: dict) -> str:
            args = args or {}
            values = {p: str(args.get(p) or "").strip() for p, _ in _PARAMS[name]}
            first = _PARAMS[name][0][0]
            if not values[first]:
                return f"Нужен {first}"
            current = rights()
            if name not in names(current):
                return UNKNOWN.format(name=name)   # нейтрально: как у любого несуществующего инструмента
            refusal, extra = _check(name, values, current)
            if refusal:
                return refusal
            if name == SPAWN:   # место ребёнка - до вызова: параллельный второй spawn его уже не найдёт
                on_children(-1)
            try:
                reply = await ext(CHANNEL_METHOD, {"session_id": session_id, "kind": KIND[name],
                                                   "text": values[first], **values, **extra})
            except Exception as e:   # клиент не поддерживает каналы или упал - модель узнаёт, а не висит
                reply = {"result": f"Канал к создателю недоступен: {e}"}
            if name == SPAWN and not (reply or {}).get(SPAWNED_KEY):
                on_children(+1)
            return str((reply or {}).get("result") or "ok")
        return call
    return {name: make(name) for name in (NAMES if rights().depth > 0 else BASE_NAMES)}
