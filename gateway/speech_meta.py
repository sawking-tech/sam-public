"""Что из ответа модели не звучит: мета-блоки в парных тегах (ADR-003, пересмотр 14 окт).

Речь модели - всегда голос. Любой парный блок <имя>...</имя> не звучит: рассуждение, вызов инструмента, молчание
<silent>, заметка для монитора или workflow. По имени тега его находит тот, кому он адресован, синтезу список имён
не нужен. Блок со своим закрывающим тегом - мета, даже вплотную к слову («вечер<note>зал</note>»). Тегом не
считаются одиночные «<» и «>» и самозакрывающийся тег (<br/>, его снимают отдельно).

Незакрытый блок: в потоке не звучит до конца - конец ещё может прийти. В итоге хода оборванное рассуждение, вызов и
молчание (UNCLOSED_CUT) по-прежнему не звучат. У другого имени снимается только тег («нажми <Enter> и подожди» не
глушит хвост ответа), а сразу после буквы или цифры это текст (List<String>, a<b и c>d).

Переходно: <voice> из прежнего режима тега снимается, а содержимое звучит - так записаны старые ответы в истории.

Чистые функции без ввода-вывода: их берут итог хода шлюза (gateway/text_utils.py) и потоковая озвучка моста
(voice/layer/speech_text.py).
"""
from __future__ import annotations

import re

_NAME = r"[A-Za-z][\w:.-]*"
_ATTRS = r"(?:\s[^<>]*)?"
_OPEN = rf"<({_NAME}){_ATTRS}(?<!/)>"
UNCLOSED_CUT = frozenset({"think", "tool_call", "silent", "quiet"})   # quiet - прежнее имя silent
LEGACY_VOICE_TAG = re.compile(r"</?voice\b[^<>]*>", re.I)
PIPE_THINKING = re.compile(r"<\|thinking\|>.*?(?:<\|/thinking\|>|\Z)", re.S)   # формат рассуждения части моделей
CLOSED_BLOCK = re.compile(rf"{_OPEN}.*?</\1\s*>", re.S | re.I)
OPEN_TAG = re.compile(_OPEN, re.I)
# Хвост после вырезки: закрывающий тег без пары (вложенный одноимённый блок) или самозакрывающийся <br/>
STRAY_TAG = re.compile(rf"(?<!\w)<{_NAME}{_ATTRS}/>|</{_NAME}\s*>", re.I)   # «</x>» в тексте не бывает
# Конец потока, который ещё может стать тегом, и после буквы тоже: «<», «<sil», «<note kind="x"», «<br/», «</no»,
# «<|thin», прежний «<voice=»; \Z, а не $: $ совпадает и перед последним переводом строки
PARTIAL_TAG = re.compile(rf"</?(?:{_NAME}{_ATTRS}/?)?\Z|<\|[\w/]*\|?\Z|</?voice\b[^<>]*\Z", re.I)


def strip_meta(text: str) -> str:
    """Итог хода без мета-блоков и тегов - то, что звучит."""
    return STRAY_TAG.sub("", _unclosed(_without_closed(text), final=True))


def stream_meta(text: str) -> str:
    """Поток: хвост сырого потока, похожий на начало тега, ждёт следующей дельты; незакрытый блок ждёт до конца.
    Хвост берётся до разбора: после вырезки блоков конец текста уже не конец потока (a<b<c>d</c>)."""
    return STRAY_TAG.sub("", _unclosed(_without_closed(PARTIAL_TAG.sub("", text)), final=False))


def _without_closed(text: str) -> str:
    return CLOSED_BLOCK.sub("", PIPE_THINKING.sub("", LEGACY_VOICE_TAG.sub("", text)))


def _unclosed(text: str, final: bool) -> str:
    """Незакрытые блоки: в потоке - всё с первого до конца; в итоге - до конца у UNCLOSED_CUT, вплотную к слову -
    текст, иначе снимается только тег."""
    pos = 0
    while (tag := OPEN_TAG.search(text, pos)) is not None:
        if not final or tag.group(1).lower() in UNCLOSED_CUT:
            return text[:tag.start()]
        if tag.start() > 0 and (text[tag.start() - 1].isalnum() or text[tag.start() - 1] == "_"):
            pos = tag.end()
            continue
        text, pos = text[:tag.start()] + text[tag.end():], tag.start()
    return text
