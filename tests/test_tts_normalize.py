"""Страховка речи перед синтезом (ADR-003): в движок уходят только слова."""
import re

import pytest

from voice import tts_normalize as tn

pytest.importorskip("num2words")

# В речи для синтеза нет цифр и знаков, которые движок читает по символу
SIGNS = re.compile(r"[\d%=*°\\]")


@pytest.mark.parametrize("text, spoken", [
    ("2 + 2 = 4", "два плюс два равно четыре"),
    ("10 / 4", "десять разделить на четыре"),
    ("3 * 4", "три умножить на четыре"),
    ("Скидка 15%, было 21%.", "Скидка пятнадцать процентов, было двадцать один процент."),
    ("Скидка 1,5%", "Скидка одна целая пять десятых процента"),
    ("На улице -5°C.", "На улице минус пять градусов."),
    ("№ 5", "номер пять"),
    ("Лежит в C:\\Users\\anton\\web.", "Лежит в путь."),
    ("Смотри https://example.com/a?b=1, там всё.", "Смотри ссылка, там всё."),
    ("Открыл gateway/static/monitor.html", "Открыл путь"),
    ("~/dev/sam готов", "путь готов"),
    ("Встреча в 14:11.", "Встреча в четырнадцать часов одиннадцать минут."),
    ("Вышла 08.10.2026.", "Вышла восьмого октября две тысячи двадцать шестого года."),
    ("Версия 3.11.2", "Версия три точка одиннадцать точка два"),
    ("Было 2,5 часа", "Было две целых пять десятых часа"),
    ("Осталось 5 минут", "Осталось пять минут"),
    ("В 14:11 2 + 2 = 4, 50% из 10 / 2, -3°, версия 1.2.3, путь C:\\web",
     "В четырнадцать часов одиннадцать минут два плюс два равно четыре, пятьдесят процентов из десять разделить на "
     "два, минус три градуса, версия один точка два точка три, путь путь"),
    ("Зам+ок на двери", "Зам+ок на двери"),          # плюс перед гласной - ударение, остаётся
    ("и/или", "и/или"),                              # косая между словами - не путь
    ("Запусти Docker", "Запусти Docker"),            # латиницу кириллицей пишет модель: по буквам звучит неверно
])
def test_only_words_reach_synthesis(text, spoken):
    out = tn.normalize_for_tts(text)
    assert out == spoken and not SIGNS.search(out)


def test_synthesis_request_carries_words(monkeypatch):
    # Страховка стоит в tts_client.stream: через него идёт каждая реплика к любому движку
    from voice.layer import tts_client
    sent = []
    monkeypatch.setattr(tts_client, "_stream_at", lambda address, text, voice: sent.append(text) or iter(()))
    monkeypatch.setattr(tts_client, "tts_backend", lambda voice=None: "silero")
    list(tts_client.stream("Стек на 2 + 2 кадра в C:\\web"))
    assert sent == ["Стэк на два плюс два кадра в путь"]


def test_silent_main_engine_hands_the_line_to_the_fallback_voice(monkeypatch):
    """stand 14, speech-out 30: основной движок (CosyVoice) не отвечает - реплика звучит запасным SAM_TTS_FALLBACK
    (silero) голосом SAM_TTS_FALLBACK_VOICE, а не пропадает. Настоящие клиент и сервер Wyoming на локальном порту;
    подменена модель синтеза."""
    import socket
    import threading
    import time

    import numpy as np

    from voice.layer import tts_client, wyoming_server
    heard = []

    def synth(text, voice):
        heard.append((text, voice))
        return np.zeros(480, dtype=np.float32), 48000
    with socket.socket() as probe:
        probe.bind((wyoming_server.HOST, 0))
        port = probe.getsockname()[1]
    threading.Thread(target=wyoming_server.serve, args=("silero", port, synth, ["xenia"]), daemon=True).start()
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection((wyoming_server.HOST, port), timeout=1).close()
            break
        except OSError:
            assert time.monotonic() < deadline, "запасной сервер не поднялся"
            time.sleep(0.05)
    monkeypatch.setattr(tts_client, "TTS_BACKENDS", {"cosy": "127.0.0.1:1", "silero": f"{wyoming_server.HOST}:{port}"})
    monkeypatch.setattr(tts_client, "tts_backend", lambda voice=None: "cosy")

    chunks = list(tts_client.stream("Привет, я тут", "cosy-voice"))

    assert chunks and chunks[0][1] == 48000 and len(chunks[0][0]) == 480
    assert heard == [("Привет, я тут", tts_client.FALLBACK_VOICE)]
    assert (tts_client.FALLBACK_TTS, tts_client.FALLBACK_VOICE) == ("silero", "xenia")
