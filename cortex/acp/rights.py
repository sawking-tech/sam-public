"""Права сессии Cortex от создателя (ADR-022, «Агенты в Cortex», SAM-133 этап 1).

Создатель агента передаёт в `_meta` новой сессии ACP: кто создатель (`creator`), какие инструменты выданы (`tools`)
метку сложности (`level`: smart, medium, weak), собеседников (`peers`) и сколько поколений детей агент может
создать (`depth`, 0 - ни одного), и сколько детей ещё можно создать (`children`). Выдачи нет - прокси-режим: все инструменты MCP сессии, как у
главного хода. Метка выбирает модель из конфига (`SAM_AGENT_LLM_<МЕТКА>`, gateway/tools/agents.level_source).
Модуль чистый: разбор словаря и фильтр имён.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

CREATOR_KEY, TOOLS_KEY, LEVEL_KEY, PEERS_KEY, DEPTH_KEY = "creator", "tools", "level", "peers", "depth"
CHILDREN_KEY = "children"
PEER_NOTES_KEY = "peer_notes"   # {id собеседника: кто он и чем занят}
PEER_PREFIX = "agent:"   # право писать агенту выдаётся как инструмент agent:<id задачи> (ADR-022, «Кому писать»)
LEVELS = ("smart", "medium", "weak")   # как gateway/tools/agents.LEVELS


@dataclass(frozen=True)
class Rights:
    creator: str = ""
    tools: tuple[str, ...] | None = None   # None - выдачи нет: все инструменты сессии
    level: str = ""
    peers: tuple[str, ...] = ()            # агенты, которым можно писать: список вызывающего и выданные права
    depth: int = 0                         # поколений детей, которых агент может создать (ADR-022, «Агент создаёт агента»)
    children: int = 0                      # сколько детей ещё можно создать: ширина дерева (ADR-022)
    peer_notes: tuple[tuple[str, str], ...] = ()   # кто собеседник и чем занят: агент решает, писать ли ему


def from_meta(meta: dict | None) -> Rights:
    """`_meta` новой сессии -> права; мусор в поле - поле не задано, а не сбой сессии."""
    meta = meta if isinstance(meta, dict) else {}
    tools = meta.get(TOOLS_KEY)
    granted = tuple(str(t) for t in tools) if isinstance(tools, list) else None
    level = str(meta.get(LEVEL_KEY) or "").strip().lower()
    peers = meta.get(PEERS_KEY)
    depth, children = meta.get(DEPTH_KEY), meta.get(CHILDREN_KEY)
    return Rights(creator=str(meta.get(CREATOR_KEY) or ""), tools=granted, level=level if level in LEVELS else "",
                  peers=tuple(str(p) for p in peers) if isinstance(peers, list) else (),
                  depth=_count(depth), children=_count(children), peer_notes=_notes(meta.get(PEER_NOTES_KEY)))


def _notes(raw) -> tuple[tuple[str, str], ...]:
    return tuple((str(k), str(v)) for k, v in raw.items()) if isinstance(raw, dict) else ()


def with_peer(rights: Rights, peer: str, note: str = "") -> Rights:
    """Права с новым собеседником и строкой о нём."""
    notes = dict(rights.peer_notes)
    if note:
        notes[peer] = note
    return replace(rights, peers=tuple(dict.fromkeys([*rights.peers, peer])), peer_notes=tuple(notes.items()))


def _count(value) -> int:
    """Неотрицательное целое из `_meta`; мусор - 0, то есть права нет."""
    return max(value, 0) if isinstance(value, int) and not isinstance(value, bool) else 0


def allowed(names: Iterable[str], rights: Rights) -> list[str]:
    """Имена инструментов сессии в пределах выдачи; выдачи нет - все."""
    return list(names) if rights.tools is None else [n for n in names if n in rights.tools]


def beyond(names: Iterable[str], rights: Rights) -> list[str]:
    """Что из имён шире выдачи (права ребёнка не шире прав родителя); выдачи нет - ничего."""
    return [] if rights.tools is None else [n for n in names if n not in rights.tools]
