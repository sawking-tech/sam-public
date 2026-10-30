"""Мост голосового слоя к /voice/turn (SAM-09, ADR-012): фраза уходит ходом, ответ - предложениями в say-out,
судьба - отчётом spoken. Сокет шлюза - tests/fakes_voice.FakeGatewaySocket, ждёт по событию отправки; сроки моста -
нулевые или заведомо не наступающие, без sleep."""
import asyncio
import json

import pytest

from voice.layer import bridge
from voice.layer import ticker as tk
from voice.layer.bus import SAY_OUT
from voice.layer.stress import STRESS
from tests.fakes_voice import FakeGatewaySocket
# Прежнее имя сокета: его берёт tests/test_rough_edges.py
from tests.fakes_voice import FakeGatewaySocket as _FakeSocket  # noqa: F401

AT = "2026-10-09T02:00:00+06:00"


def _say_out(d):
    return [json.loads(line) for line in (d / SAY_OUT).read_text(encoding="utf-8").splitlines()]


def _queue(*items) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    for item in items:
        q.put_nowait(item)
    return q


@pytest.mark.parametrize("fates, report", [
    ([{"text": "Привет!", "status": "played"}, {"text": "Как дела?", "status": "cut"}],
     [{"text": "Привет!", "status": "played"}, {"text": "Как дела?", "status": "cut"}]),
    # say_out пишет судьбу текстом с ударениями: сверка их не видит
    ([{"text": f"Приве{STRESS}т!", "status": "played"}, {"text": "чужая реплика", "status": "played"},
      {"text": f"Как дел{STRESS}а?", "status": "dropped"}],
     [{"text": "Привет!", "status": "played"}, {"text": "Как дела?", "status": "dropped"}]),
    ([], [{"text": "Привет!", "status": "unknown"}, {"text": "Как дела?", "status": "unknown"}]),   # судьбы нет
], ids=["fates", "stress-marks", "no-fate"])
async def test_bridge_turn_phrase_to_say_out_and_fate_report(tmp_path, fates, report):
    ws = FakeGatewaySocket([{"type": "delta", "text": "Привет! Как"}, {"type": "delta", "text": " дела?"},
                            {"type": "done", "voice": "Привет! Как дела?", "tools": []}], answer_after=1, close_after=2)
    # нет судьбы - срок ожидания нулевой: отчёт сразу с unknown; есть - срок не наступит раньше судеб
    await bridge._serve(ws, tmp_path, _queue({"ts": AT, "text": "привет"}), "v1", fates=_queue(*fates),
                        **({} if fates else {"fate_timeout_s": 0}))
    assert ws.sent == [{"type": "turn", "session": "v1", "text": "привет", "at": AT},
                       {"type": "spoken", "session": "v1", "sentences": report}]
    assert [(line["text"], line["reply_to"]) for line in _say_out(tmp_path)] == [("Привет!", AT), ("Как дела?", AT)]


@pytest.mark.parametrize("deltas, done_voice, streamed, spoken", [
    (["Открываю бра", "узер. Сейчас найду ", "погоду"], "Открываю браузер. Сейчас найду погоду",
     ["Открываю браузер."], ["Открываю браузер.", "Сейчас найду погоду"]),        # первое - до конца ответа
    (["Температура 3.5 градуса, т. е. холодно. Оденься. "], "", ["Температура 3.5 градуса, т. е. холодно.", "Оденься."],
     ["Температура 3.5 градуса, т. е. холодно.", "Оденься."]),                   # число и сокращение не режут
    (["Готово, без точки"], "Готово, без точки", [], ["Готово, без точки"]),      # хвост - в конце
    (["Пять. [судь", "ба неизве", "стна] Шесть. ", "Конец."], "Пять. Шесть. Конец.", ["Пять.", "Шесть."],
     ["Пять.", "Шесть.", "Конец."]),                                              # метка по кускам не звучит
    (["Привет."], "Привет.", [], ["Привет."]),                                    # итог не повторяется после потока
    (["Если x < y, то"], "Если x < y, то", [], ["Если x < y, то"]),
    ([], "итог хода", [], ["итог хода"]),                                         # поток пуст - звучит итог
])
def test_sentences_stream_before_answer_ends(deltas, done_voice, streamed, spoken):
    turn = bridge.TurnSpeech()
    lines = [line for d in deltas for line in turn.on_message({"type": "delta", "text": d})]
    assert [line["text"] for line in lines] == streamed
    lines += turn.on_message({"type": "done", "voice": done_voice, "tools": []})
    assert [line["text"] for line in lines] == spoken


@pytest.mark.parametrize("messages, lines", [
    ([{"type": "source", "source": "fast"}, {"type": "delta", "text": "Раз. Два "}],
     [{"text": "Раз.", "from": bridge.FAST, "reply_to": AT}]),
    ([{"type": "source", "source": "main"}, {"type": "delta", "text": "Раз. Два "}],
     [{"text": "Раз.", "from": bridge.MAIN, "reply_to": AT}]),
    # 9 окт: мост метил haiku любой ответ, и поправка ведущего отзывала ответы основной
    ([{"type": "source", "source": "??"}, {"type": "delta", "text": "Раз. Два "}],
     [{"text": "Раз.", "from": bridge.MAIN, "reply_to": AT}]),
    ([{"type": "delta", "text": "Раз. Два"}, {"type": "done", "voice": "Раз. Два", "tools": []}],
     [{"text": "Раз.", "from": bridge.MAIN, "reply_to": AT}, {"text": "Два", "from": bridge.MAIN, "reply_to": AT}]),
    # поставщик сменился: начатое обрывается следующим предложением, отвечает основная
    ([{"type": "source", "source": "fast"}, {"type": "delta", "text": "Нача"}, {"type": "reset"},
      {"type": "source", "source": "main"}, {"type": "delta", "text": "Ответ основной. Дальше "}],
     [{"text": "Ответ основной.", "from": bridge.MAIN, "reply_to": AT, "interrupt": True}]),
], ids=["fast", "main", "unknown", "done", "reset"])
def test_lines_carry_who_answered_and_reset_interrupts(messages, lines):
    turn = bridge.TurnSpeech(reply_to=AT)
    assert [line for msg in messages for line in turn.on_message(msg)] == lines


@pytest.mark.parametrize("rec, goes_on", [
    ({"text": "открой браузер", "who": "owner"}, True),
    ({"text": "моя озвучка", "echo": True}, False),
    ({"text": "Сэм, при", "partial": True}, False),
    ({"text": "   "}, False),
])
def test_only_finished_human_phrases_go_on(rec, goes_on):
    """Один фильтр у моста (в шлюз) и тикера (ведущему)."""
    assert bridge.is_phrase(rec) is goes_on
    assert bool(tk.voice_ticks([rec])) is goes_on


@pytest.mark.parametrize("fate, taken, status", [
    ("played", False, "played"),
    (None, False, "stale"),          # не взята в озвучку до срока очереди: в историю не идёт
    (None, True, "unknown"),         # взята, но судьба не пришла
])
async def test_own_reply_fate_played_stale_or_unknown(tmp_path, monkeypatch, fate, taken, status):
    """SAM-85: своя реплика say отчитывается spoken с её id; судьба приходит, когда реплика встала в очередь паузы."""
    fates: asyncio.Queue = asyncio.Queue()

    def add(d, text, **kw):
        if fate:
            fates.put_nowait({"text": str(bridge.agenda.wav_path(d, "i1")), "status": fate})
        return "i1"
    monkeypatch.setattr(bridge.agenda, "add", add)
    monkeypatch.setattr(bridge.agenda, "is_done", lambda d, iid: taken)
    deadline = {} if fate else {"own_ttl_s": 0, "fate_timeout_s": 0}   # нет судьбы - срок вышел сразу
    if not fate:
        monkeypatch.setattr(bridge, "OWN_CHECK_S", 0)
    ws = FakeGatewaySocket([{"type": "say", "text": "Тесты зелёные.", "id": "k1"}], close_after=1)
    await bridge._serve(ws, tmp_path, asyncio.Queue(), "v1", fates=fates, **deadline)
    assert ws.sent == [{"type": "spoken", "id": "k1", "session": "v1",
                        "sentences": [{"text": "Тесты зелёные.", "status": status}]}]


async def test_bridge_reconnects_every_two_seconds_while_the_gateway_is_down(tmp_path, monkeypatch):
    """stand 19: шлюз лежит - мост пишет строку и пробует снова через SAM_BRIDGE_RECONNECT_S (2 с), не падает.
    Подменены подключение websockets (граница сети) и сон цикла."""
    import websockets

    class _Stop(BaseException):
        """Третья попытка: шлюз поднялся - тесту дальше не нужно."""
    tries, slept = [], []

    def connect(url, **kwargs):
        tries.append(url)
        if len(tries) == 3:
            raise _Stop
        raise OSError("connection refused")

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)

    with pytest.raises(_Stop):
        await bridge.run_bridge(tmp_path, url="ws://gateway/voice/turn")
    assert tries == ["ws://gateway/voice/turn"] * 3 and slept == [2.0, 2.0]
