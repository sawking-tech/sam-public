"""Озвучка реплик (voice/layer/say_out.py) целиком: serve читает say-out.jsonl, Speaker синтезирует и играет.
Перебивание стоп-словом и громкостью, interrupt, отклик back, ожидание тишины, устаревший ответ, хвост speaking,
остаток в resume.txt и «продолжай». Настоящие serve, Speaker, Synthesis и конвейер звука; подменены движок синтеза
(tts_client.stream) и ядро звука (колонки: куски записываются, звучание мгновенное или держится тестом)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

from voice.layer import say_out
from voice.layer.bus import BARGE_FLAG, HEARING_FLAG, SAID, SAY_OUT, SPEAKING_FLAG, VOICE_IN

ENGINE_RATE = 24_000
CHUNK = np.full(ENGINE_RATE // 5, 8_000, np.int16)        # 0,2 с звука движка на отрезок
WAIT_S = 10
LONG = ["Первое предложение достаточно длинное, чтобы стать отдельным отрезком синтеза для движка.",
        "Второе предложение тоже длинное и должно прозвучать только после просьбы продолжить разговор.",
        "Третье предложение завершает реплику и тоже не прозвучало из-за перебивания человеком."]


class _Engine:
    """Движок синтеза: каждый отрезок - один кусок; gate - держать отрезок, пока тест не отпустит."""

    def __init__(self):
        self.texts, self.gate = [], None

    def stream(self, text, voice):
        self.texts.append(text)
        if self.gate is not None:
            self.gate.wait(WAIT_S)
        yield CHUNK, ENGINE_RATE


class _Core:
    """Колонки: куски записываются; held - «звучит», пока тест не снимет; wind_down - сразу договорено."""

    def __init__(self):
        self.played, self.held, self.halts = [], threading.Event(), 0
        self.winding = False

    def play(self, audio, rate):
        self.played.append(audio)

    def busy(self):
        return self.held.is_set()

    def end_phrase(self):
        pass

    def stop(self):
        self.halts += 1
        self.held.clear()

    def wind_down(self, keep, fade, level=0.4):
        self.held.clear()

    def duck(self, level):
        pass


@pytest.fixture
def stand(tmp_path, monkeypatch):
    engine, core = _Engine(), _Core()
    monkeypatch.setattr(say_out, "stream", engine.stream)
    monkeypatch.setattr(say_out, "tts_backend", lambda voice=None: "silero")
    for name in (SAY_OUT, VOICE_IN):
        (tmp_path / name).touch()
    made = []
    real = say_out.Speaker

    def speaker(d, core_, recorder=None):
        made.append(real(d, core_, recorder))
        return made[-1]
    monkeypatch.setattr(say_out, "Speaker", speaker)
    threading.Thread(target=say_out.serve, args=(tmp_path, core), daemon=True).start()
    _until(lambda: made)
    return tmp_path, engine, core, made[0]


def _until(condition, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "озвучка не дошла до ожидаемого"
        time.sleep(0.02)


def _iso(dt=None):
    return (dt or datetime.now().astimezone()).isoformat()


def _say(d, text, **fields):
    rec = {"ts": _iso(), "text": text, **fields}
    with (d / SAY_OUT).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _heard(d, text, ts=None):
    with (d / VOICE_IN).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts or _iso(), "text": text, "echo": False}, ensure_ascii=False) + "\n")


def _fates(d):
    path = d / SAID
    return [(r["text"], r["status"]) for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())] \
        if path.exists() else []


def test_stop_word_cuts_now_and_lines_written_before_it_stay_silent(stand):
    """barge-in 21, 24, speech-out 2: стоп-слово обрывает реплику сразу (cut), а строки, записанные до стоп-слова и
    прочитанные после, не звучат (stale) - кроме строки с interrupt; новая строка звучит."""
    d, engine, core, _ = stand
    engine.gate = threading.Event()
    _say(d, "Сейчас расскажу про погоду.")
    _until(lambda: engine.texts)
    before = _iso(datetime.now().astimezone() - timedelta(seconds=1))
    (d / BARGE_FLAG).write_text("stop", encoding="utf-8")
    engine.gate.set()
    _until(lambda: ("Сейчас расскажу про погоду.", "cut") in _fates(d))
    with (d / SAY_OUT).open("a", encoding="utf-8") as f:
        for rec in ({"ts": before, "text": "А ещё ветер."}, {"ts": before, "text": "Срочно: дверь.", "interrupt": True}):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _say(d, "Слушаю.")
    _until(lambda: ("Слушаю.", "played") in _fates(d))

    assert _fates(d) == [("Сейчас расскажу про погоду.", "cut"), ("А ещё ветер.", "stale"),
                         ("Срочно: дверь.", "played"), ("Слушаю.", "played")]
    assert core.halts >= 1 and not (d / BARGE_FLAG).exists()


def test_interrupt_line_drops_the_queue_and_cuts_the_current_one(stand):
    """barge-in 23, speech-out 9: строка с interrupt сбрасывает очередь и обрывает текущую реплику."""
    d, engine, core, speaker = stand
    core.held.set()                                          # реплика звучит, пока тест не снимет
    _say(d, "Длинный рассказ.")
    _until(lambda: core.played)
    _say(d, "Ждёт очереди.")
    _until(lambda: not speaker.q.empty())
    _say(d, "Стоп, важное.", interrupt=True)
    _until(lambda: ("Стоп, важное.", "played") in _fates(d))

    assert "Ждёт очереди." not in engine.texts and core.halts >= 1
    assert ("Длинный рассказ.", "played") not in _fates(d)


def _played_by(core, d, say):
    """Звук одной реплики: куски, ушедшие в колонки после её записи."""
    start = len(core.played)
    say()
    _until(lambda: len(core.played) > start)
    time.sleep(0.2)
    return np.concatenate(core.played[start:])


def test_back_reply_sounds_at_once_quieter_and_slower_without_a_fate(stand):
    """speech-out 3: отклик back звучит сразу поверх речи человека (hearing не ждёт), тише (0,6) и медленнее (0,95)
    при любом темпе сессии, судьба в said.jsonl не пишется."""
    d, engine, core, _ = stand
    normal = _played_by(core, d, lambda: _say(d, "Да."))
    (d / HEARING_FLAG).touch()
    back = _played_by(core, d, lambda: _say(d, "Ага.", back=True))
    assert len(back) == len(normal) and np.allclose(back, normal * say_out.BACK_GAIN, atol=2)   # тот же темп 0,95
    (d / HEARING_FLAG).unlink()
    (d / "session.json").write_text(json.dumps({"speed": 1.2}), encoding="utf-8")
    fast_session = _played_by(core, d, lambda: _say(d, "Нет."))
    (d / HEARING_FLAG).touch()
    back_again = _played_by(core, d, lambda: _say(d, "Угу.", back=True))
    assert len(back_again) == len(back) > len(fast_session) * 1.2      # отклик - всегда 0,95
    assert [text for text, _ in _fates(d)] == ["Да.", "Нет."]
    assert (say_out.BACK_GAIN, say_out.BACK_SPEED) == (0.6, 0.95)


def test_reply_waits_for_the_human_to_finish(stand):
    """speech-out 13: реплика (не back) ждёт снятия hearing - не говорит поверх человека; предел ожидания 30 с."""
    d, engine, core, _ = stand
    (d / HEARING_FLAG).touch()
    _say(d, "Отвечаю.")
    _until(lambda: engine.texts)
    time.sleep(0.5)
    assert core.played == []
    (d / HEARING_FLAG).unlink()
    _until(lambda: core.played)
    assert say_out.HEARING_WAIT_SECS == 30.0


def test_answer_becomes_stale_if_a_new_phrase_came_while_it_waited(stand):
    """speech-out 8: перед первым звуком ответ быстрой проверяется ещё раз: пока он ждал тишины, человек сказал новое -
    ответ на старую фразу не звучит (stale)."""
    d, engine, core, _ = stand
    asked = datetime.now().astimezone() - timedelta(seconds=5)
    _heard(d, "который час", _iso(asked))
    (d / HEARING_FLAG).touch()
    _say(d, "Сейчас девять.", **{"from": "haiku", "reply_to": _iso(asked)})
    _until(lambda: engine.texts)
    _heard(d, "а нет, сколько градусов")
    (d / HEARING_FLAG).unlink()
    _until(lambda: _fates(d))
    assert _fates(d) == [("Сейчас девять.", "stale")] and core.played == []


def test_speaking_flag_outlives_the_last_line_by_the_tail(stand):
    """barge-in 1, speech-out 15: speaking лежит, пока звучит, и ещё 0,4 с после последней реплики: слух не принимает
    хвост эха за фразу человека."""
    d, _, _, _ = stand
    _say(d, "Готово.")
    _until(lambda: ("Готово.", "played") in _fates(d))
    assert (d / SPEAKING_FLAG).exists()
    _until(lambda: not (d / SPEAKING_FLAG).exists(), timeout=say_out.TAIL_SECS + 1)
    assert say_out.TAIL_SECS == 0.4


def test_rest_after_a_loud_barge_waits_for_continue(stand):
    """barge-in 20, 25, speech-out 21, 22: громкое перебивание - договорить текущее, следующие отрезки не звучат и
    ложатся в resume.txt; «продолжай» ставит остаток в очередь, он звучит."""
    d, engine, core, _ = stand
    core.held.set()
    _say(d, " ".join(LONG))
    _until(lambda: core.played)
    (d / BARGE_FLAG).write_text("loud", encoding="utf-8")
    _until(lambda: (d / "resume.txt").exists())
    assert (d / "resume.txt").read_text(encoding="utf-8") == f"{LONG[1]} {LONG[2]}"
    assert engine.texts == [LONG[0], LONG[1]]                 # второй синтезировался заранее, но не прозвучал
    played = len(core.played)
    _heard(d, "продолжай, пожалуйста")
    _until(lambda: (f"{LONG[1]} {LONG[2]}", "played") in _fates(d))
    assert len(core.played) > played and not (d / "resume.txt").exists()


def test_old_rest_is_forgotten_after_two_minutes(stand):
    """speech-out 21: остаток живёт RESUME_TTL (120 с); позже «продолжай» его не договаривает."""
    d, engine, core, speaker = stand
    rest = d / "resume.txt"
    rest.write_text("давний остаток", encoding="utf-8")
    old = time.time() - say_out.RESUME_TTL - 1
    os.utime(rest, (old, old))
    _heard(d, "продолжай")
    _until(lambda: not rest.exists())
    time.sleep(0.3)
    assert engine.texts == [] and speaker.q.empty() and say_out.RESUME_TTL == 120.0


def test_tempo_comes_from_session_json_then_the_default(tmp_path, monkeypatch):
    """speech-out 28: темп - speed в session.json, иначе SAM_VOICE_SPEED (0,95)."""
    from voice.layer import tts_client
    assert tts_client.session_voice(tmp_path)[1] == tts_client.SPEED == 0.95
    (tmp_path / "session.json").write_text(json.dumps({"speed": 1.2}), encoding="utf-8")
    assert tts_client.session_voice(tmp_path)[1] == 1.2


def test_loudness_before_the_first_sound_is_not_a_barge(stand):
    """barge-in 19: громкость, пока реплика ещё не зазвучала, - это он заговорил, а не перебил: реплика ждёт тишины
    и звучит целиком, а не обрывается."""
    d, engine, core, _ = stand
    engine.gate = threading.Event()
    _say(d, "Напомню про ужин.")
    _until(lambda: engine.texts)
    (d / BARGE_FLAG).write_text("loud", encoding="utf-8")
    engine.gate.set()
    _until(lambda: _fates(d))
    assert _fates(d) == [("Напомню про ужин.", "played")] and core.played


class _SilentEngine(_Engine):
    def stream(self, text, voice):
        self.texts.append(text)
        raise ConnectionError("движок не ответил")
        yield   # pragma: no cover - генератор


def test_line_without_sound_is_dropped_not_reported_as_played(tmp_path, monkeypatch):
    """speech-out 14: синтез не дал ни куска - ошибка в лог, реплика бросается; прозвучавшей она не считается:
    иначе в историю ушло бы то, чего никто не слышал."""
    engine = _SilentEngine()
    monkeypatch.setattr(say_out, "stream", engine.stream)
    monkeypatch.setattr(say_out, "tts_backend", lambda voice=None: "silero")
    for name in (SAY_OUT, VOICE_IN):
        (tmp_path / name).touch()
    core = _Core()
    threading.Thread(target=say_out.serve, args=(tmp_path, core), daemon=True).start()
    time.sleep(0.3)
    _say(tmp_path, "Скажу, если движок жив.")
    _until(lambda: engine.texts)
    _until(lambda: _fates(tmp_path))
    time.sleep(0.5)
    assert core.played == [] and _fates(tmp_path) == [("Скажу, если движок жив.", "dropped")]


class _FailsAfterFirstEngine(_Engine):
    def stream(self, text, voice):
        self.texts.append(text)
        if len(self.texts) > 1:
            raise ConnectionError("движок не ответил")
        yield CHUNK, ENGINE_RATE


def _serve_with(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(say_out, "stream", engine.stream)
    monkeypatch.setattr(say_out, "tts_backend", lambda voice=None: "silero")
    monkeypatch.setattr(say_out, "segment_chars", lambda backend=None: 1)   # отрезок - предложение
    for name in (SAY_OUT, VOICE_IN):
        (tmp_path / name).touch()
    core = _Core()
    threading.Thread(target=say_out.serve, args=(tmp_path, core), daemon=True).start()
    time.sleep(0.3)
    return core


def test_synthesis_failing_mid_line_is_cut_and_the_rest_waits(tmp_path, monkeypatch):
    """speech-out 14: первый отрезок прозвучал, синтез следующего сорвался - реплика cut, как при перебивании, а не
    played; непрозвучавший хвост ждёт «продолжай»."""
    engine = _FailsAfterFirstEngine()
    core = _serve_with(tmp_path, monkeypatch, engine)
    _say(tmp_path, "Первое прозвучит. Второе нет.")
    _until(lambda: _fates(tmp_path))
    time.sleep(0.5)
    assert core.played and _fates(tmp_path) == [("Первое прозвучит. Второе нет.", "cut")]
    assert (tmp_path / "resume.txt").read_text(encoding="utf-8") == "Второе нет."


def test_back_reply_without_sound_leaves_no_fate(tmp_path, monkeypatch):
    """Отклик без звука, как и прозвучавший, в said не пишется: тикер не будит ведущего из-за «угу»; хвост отклика,
    сорвавшегося на втором отрезке, на «продолжай» не возвращается."""
    engine = _FailsAfterFirstEngine()
    _serve_with(tmp_path, monkeypatch, engine)
    _say(tmp_path, "Угу. Слушаю.", back=True)
    _until(lambda: len(engine.texts) > 1)
    time.sleep(0.8)
    assert _fates(tmp_path) == [] and not (tmp_path / "resume.txt").exists()


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def _serve(tmp_path, monkeypatch, core):
    engine = _Engine()
    monkeypatch.setattr(say_out, "stream", engine.stream)
    monkeypatch.setattr(say_out, "tts_backend", lambda voice=None: "silero")
    (tmp_path / VOICE_IN).touch()
    threading.Thread(target=say_out.serve, args=(tmp_path, core), daemon=True).start()
    return engine


def test_lines_written_shortly_before_start_still_sound(tmp_path, monkeypatch):
    """speech-out 1: при старте озвучки строки say-out, записанные за последние 15 с, звучат (написанное, пока
    процесс поднимался, не теряется); старше - нет."""
    now = datetime.now().astimezone()
    with (tmp_path / SAY_OUT).open("w", encoding="utf-8") as f:
        for age_s, text in ((40, "Старая реплика."), (5, "Свежая реплика.")):
            f.write(json.dumps({"ts": _iso(now - timedelta(seconds=age_s)), "text": text}, ensure_ascii=False) + "\n")
    _serve(tmp_path, monkeypatch, _Core())
    _until(lambda: _fates(tmp_path))
    _say(tmp_path, "Новая реплика.")
    _until(lambda: len(_fates(tmp_path)) == 2)
    assert _fates(tmp_path) == [("Свежая реплика.", "played"), ("Новая реплика.", "played")]


class _Sounddevice:
    """sounddevice на границе: play пишет буфер, звучание мгновенное."""

    def __init__(self):
        self.played = []
        self.stream = type("S", (), {"active": False})()

    def play(self, audio, rate):
        self.played.append((len(audio), rate))

    def get_stream(self):
        return self.stream

    def stop(self):
        pass


def test_without_the_sound_core_a_line_plays_as_one_buffer_through_sounddevice(tmp_path, monkeypatch):
    """speech-out 18: без ядра звука (отдельный запуск say_out) фраза синтезируется до конца и играется одним буфером
    через sounddevice, а не кусками по мере синтеза."""
    sd = _Sounddevice()
    monkeypatch.setattr(say_out, "sd", sd)
    (tmp_path / SAY_OUT).touch()
    _serve(tmp_path, monkeypatch, None)
    def pieces(text, voice):
        for _ in range(ENGINE_PIECES):
            yield CHUNK, ENGINE_RATE
    monkeypatch.setattr(say_out, "stream", pieces)
    time.sleep(0.3)                                      # serve встал на конец файла
    _say(tmp_path, "Свет выключила.")
    _until(lambda: _fates(tmp_path))
    assert _fates(tmp_path) == [("Свет выключила.", "played")]
    [(samples, rate)] = sd.played
    whole = ENGINE_PIECES * len(CHUNK) * say_out.OUT_RATE / ENGINE_RATE
    assert rate == say_out.OUT_RATE and abs(samples - whole) < whole * 0.1


ENGINE_PIECES = 3   # движок отдаёт фразу тремя кусками
