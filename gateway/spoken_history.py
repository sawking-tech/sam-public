"""В историю только прозвучавшее (ADR-013, SAM-55).

Голосовой ход не пишет ответ в историю по концу генерации. Ответ ждёт отчёта моста голосового слоя
о судьбе каждого предложения (said.jsonl): прозвучавшее идёт в историю, оборванное - с меткой,
непрозвучавшее и отозванное - нет. Отчёта нет за срок - ответ пишется с полем судьбы «неизвестна».

Судьба - поле записи (fate), не текст реплики: метку в тексте модель видела в контексте
и копировала в речь. Старые записи с меткой в тексте не мигрируются: метку вырезает чтение для контекста
(strip_fate_mark в runner.build_messages и compression._to_llm_msg).

Своя реплика SAM (итог воркера, heartbeat; SAM-85) идёт тем же путём: сообщение say несёт id, мост
отчитывается о её судьбе с тем же id, потому что звучит она в паузе, не по порядку ответов.

Ответ, ждущий отчёта, в истории ещё нет, но он уже звучит. Следующий ход видит его в контексте вызова
(with_in_flight): без него фраза человека стоит в истории без ответа, и быстрая модель отвечала на неё
заново («Пять. Всё нормально, у тебя как?», сверка 9 окт 15:2x). В историю он по-прежнему идёт только
по отчёту моста.

compose_spoken и with_in_flight - чистые решения; SpokenLedger держит ожидающие ответы и пишет
событие assistant_spoken в шину (история пишется только через emit).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable
from .env import env_float

IGNORE_FIELD = "ignore"   # поле скрытой записи истории (repos/session_repo.IGNORE): контекст её не видит

PLAYED, CUT = "played", "cut"
UNKNOWN = "unknown"           # мост не узнал судьбу предложения
CUT_MARK = "[прервано]"
# Метка судьбы в тексте записей до 9 окт 19:30; теперь судьба - поле FATE, метку вырезает чтение
LEGACY_UNKNOWN_MARK = "[судьба неизвестна]"
_LEGACY_MARK = re.compile(r"\s*" + re.escape(LEGACY_UNKNOWN_MARK))
FATE = "fate"                 # поле события и записи истории: PLAYED или UNKNOWN
EVENT = "assistant_spoken"    # в _PERSIST шины: пишется в историю как реплика ассистента
# Сколько ждать отчёта моста после конца хода: реплика длиной в минуту ещё звучит. Не замерено.
REPORT_TIMEOUT_S = env_float("SAM_SPOKEN_REPORT_TIMEOUT_S", 120.0)
# Своя реплика ждёт паузы в очереди своих реплик до её срока (voice/layer/agenda.py DEFAULT_TTL, 300 с),
# потом мост ждёт судьбу (SAM_BRIDGE_FATE_TIMEOUT_S, 90 с); срок gateway - больше их суммы. Не замерено.
OWN_REPORT_TIMEOUT_S = env_float("SAM_SPOKEN_OWN_REPORT_TIMEOUT_S", 420.0)


def compose_spoken(sentences: list[dict]) -> str:
    """Предложения с судьбой -> текст реплики для истории. Пусто - в историю ничего."""
    parts = []
    for s in sentences:
        text, status = str(s.get("text") or "").strip(), s.get("status")
        if not text:
            continue
        if status == PLAYED:
            parts.append(text)
        elif status == CUT:
            parts.append(f"{text} {CUT_MARK}")
        elif status == UNKNOWN:
            parts.append(text)    # судьба - поле записи (fate_of), не текст
        # dropped, stale, dup, retracted: человек этого не слышал
    return " ".join(parts)


def fate_of(sentences: list[dict]) -> str:
    """Судьба реплики для поля записи: хоть одно вошедшее предложение без отчёта - неизвестна."""
    unknown = any(s.get("status") == UNKNOWN and str(s.get("text") or "").strip() for s in sentences)
    return UNKNOWN if unknown else PLAYED


def strip_fate_mark(content: str) -> str:
    """Текст записи без метки судьбы старого формата: модель её не видит и не повторяет."""
    return _LEGACY_MARK.sub("", content) if LEGACY_UNKNOWN_MARK in content else content


def replay_fate(doc: dict) -> dict:
    """Запись истории ассистента -> текст и судьба для монитора. Старая запись с меткой в тексте
    показывается без метки, с судьбой «неизвестна»; запись без судьбы (текстовый ход) - без поля."""
    content = doc.get("content") or ""
    fate = doc.get(FATE) or (UNKNOWN if LEGACY_UNKNOWN_MARK in content else None)
    return {"content": strip_fate_mark(content), **({FATE: fate} if fate else {})}


def spoken_event(session_id: str, spoken: str, source: str, fate: str, ignore: str = "") -> dict:
    """Данные события EVENT: реплика для истории и судьба отдельным полем; ignore - причина скрыть
    запись от контекста (поправленный бред, SAM-166)."""
    return {"content": spoken, "voice": spoken, "session": session_id, "source": source, FATE: fate,
            **({IGNORE_FIELD: ignore} if ignore else {})}


def with_in_flight(history: list[dict], in_flight: list[tuple[str, str]]) -> list[dict]:
    """История для контекста хода: каждый ответ в пути (фраза, речь) встаёт сразу после своей фразы.
    Ответы в пути отвечают на последние фразы, поэтому фраза ищется с конца; не нашлась (сжата,
    за пределом окна) - ответ встаёт в конец. Без меток: метку модели копируют в речь."""
    out = list(history)
    limit = len(out)
    for phrase, voice in reversed(in_flight):
        at = _last_user(out, phrase, limit)
        reply = {"role": "assistant", "content": voice}
        if at is None:
            out.append(reply)
            continue
        out.insert(at + 1, reply)
        limit = at
    return out


def _last_user(history: list[dict], phrase: str, limit: int) -> int | None:
    for i in range(limit - 1, -1, -1):
        h = history[i]
        if h.get("role") == "user" and h.get("content") == phrase:
            return i
    return None


@dataclass
class _Pending:
    voice: str
    key: str | None = None        # id своей реплики say; у ответа хода нет, он идёт по порядку
    timer: asyncio.Task | None = None
    phrase: str | None = None     # фраза человека, на которую это ответ хода
    ignore: str = ""              # поправка основной: записать скрытой от контекста (SAM-166)


class SpokenLedger:
    """Ответы голосовых ходов, ждущие отчёта о судьбе, очередью на сессию. Мост отчитывается
    по ответам по порядку (bridge._report_fates), поэтому отчёт относится к самому старому
    ожидающему: новый ход, кончившийся раньше отчёта о прежнем (перебивание), его не трогает.
    Своя реплика с ключом ждёт отчёта со своим ключом и в очередь ответов не встаёт."""

    def __init__(self, emit: Callable[[str, dict], Any], timeout_s: float = REPORT_TIMEOUT_S) -> None:
        self._emit = emit
        self._timeout_s = timeout_s
        self._pending: dict[str, list[_Pending]] = {}

    def expect(self, session_id: str, voice: str, key: str | None = None,
               timeout_s: float | None = None, phrase: str | None = None) -> None:
        if not voice.strip():
            return
        entry = _Pending(voice, key, phrase=phrase)
        self._pending.setdefault(session_id, []).append(entry)
        wait_s = self._timeout_s if timeout_s is None else timeout_s
        entry.timer = asyncio.get_running_loop().create_task(self._expire(session_id, entry, wait_s))

    def settle(self, session_id: str, sentences: list[dict], key: str | None = None) -> bool:
        """Отчёт моста о реплике с ключом или о самом старом ответе хода. False - такого не ждали."""
        entry = self._pop(session_id, self._find(session_id, key))
        if entry is None:
            return False
        if entry.timer:
            entry.timer.cancel()
        self._write(session_id, compose_spoken(sentences), "bridge", fate_of(sentences), entry.ignore)
        return True

    def forget(self, session_id: str, voice: str, reason: str) -> bool:
        """Ответ хода, ещё ждущий отчёта, запишется скрытым от контекста (поправка основной). False - такого нет."""
        entry = next((e for e in self._pending.get(session_id) or []
                      if e.key is None and e.voice.strip() == voice.strip()), None)
        if entry is None:
            return False
        entry.ignore = reason
        return True

    def pending(self, session_id: str) -> bool:
        return bool(self._pending.get(session_id))

    def in_flight(self, session_id: str) -> list[tuple[str, str]]:
        """Ответы ходов, ждущие отчёта, по порядку: (фраза, речь). Свои реплики say сюда не входят:
        они звучат в паузе и ни на какую фразу не отвечают."""
        return [(e.phrase, e.voice) for e in self._pending.get(session_id) or []
                if e.key is None and e.phrase is not None]

    def _find(self, session_id: str, key: str | None) -> _Pending | None:
        return next((e for e in self._pending.get(session_id) or [] if e.key == key), None)

    def _pop(self, session_id: str, entry: _Pending | None) -> _Pending | None:
        queue = self._pending.get(session_id) or []
        if entry is None or entry not in queue:
            return None
        queue.remove(entry)
        if not queue:
            self._pending.pop(session_id, None)
        return entry

    async def _expire(self, session_id: str, entry: _Pending, wait_s: float) -> None:
        await asyncio.sleep(wait_s)
        if self._pop(session_id, entry) is not None:
            print(f"[Spoken] {session_id}: отчёта о судьбе нет {wait_s:g} с, пишу с судьбой «неизвестна»")
            self._write(session_id, entry.voice, "timeout", UNKNOWN, entry.ignore)

    def _write(self, session_id: str, spoken: str, source: str, fate: str, ignore: str = "") -> None:
        if not spoken:
            return
        self._emit(EVENT, spoken_event(session_id, spoken, source, fate, ignore))
