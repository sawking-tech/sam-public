"""Конец хода моделью (voice/layer/turn_end.py) и его офлайн-замер (turn_end_eval.py): логика без звука и модели.

Модель Smart Turn лежит вне git (data/voice/smart-turn): её тест - под маркером integration.
"""

from __future__ import annotations

import numpy as np
import pytest

from voice.layer.config import TURN_END_MODEL
from voice.layer.turn_end import SAMPLE_RATE, WINDOW_SECS, TurnEnd, last_window
from voice.layer.turn_end_eval import BLOCK_SECS, continued, pause_points, verdict

LOUD, QUIET = 1000.0, 50.0


def test_turn_end_rule() -> None:
    rule = TurnEnd(pause=0.4, threshold=0.7)
    # модель спрашивается один раз, когда тишина дошла до паузы
    assert [rule.should_ask(a, b) for a, b in ((0.2, 0.3), (0.3, 0.4), (0.4, 0.5))] == [False, True, False]
    assert rule.closes(0.7) and not rule.closes(0.69)


def test_model_window_is_last_seconds() -> None:
    window = last_window(np.ones(SAMPLE_RATE, dtype=np.float32))   # короткое дополняется тишиной спереди
    assert len(window) == WINDOW_SECS * SAMPLE_RATE and window[0] == 0 and window[-1] == 1
    audio = np.arange((WINDOW_SECS + 2) * SAMPLE_RATE, dtype=np.float32)
    assert last_window(audio)[-1] == audio[-1]                       # у длинного остаётся конец


@pytest.mark.parametrize("levels, points", [
    # предзапись 5 тихих блоков, речь, пауза 0,5 с, речь, хвост тишины 1,5 с
    ([QUIET] * 5 + [LOUD] * 10 + [QUIET] * 5 + [LOUD] * 5 + [QUIET] * 15, [(18, False), (28, True)]),
    ([QUIET] * 10, []),
], ids=["from-first-loud-last-is-final", "quiet-preroll-is-not-a-pause"])
def test_pause_points_of_a_phrase(levels, points) -> None:
    assert pause_points(levels, pause=0.4, silence_rms=230) == points


def _row(events: list[dict], wait: float = 1.5) -> dict:
    return {"wav": "a.wav", "wait": wait, "events": events}


FINAL_HIGH = {"pause": 0.4, "at": 3.0, "final": True, "p": 0.9}


@pytest.mark.parametrize("events, early, cut_at, gain", [
    ([FINAL_HIGH], True, None, 1.5 - 0.4 - BLOCK_SECS),
    ([{"pause": 0.4, "at": 1.0, "final": False, "p": 0.8}, FINAL_HIGH], False, 1.0, 0.0),
    ([{"pause": 0.4, "at": 3.0, "final": True, "p": 0.3}], False, None, None),
], ids=["final-closes-early-with-gain", "internal-is-a-cut", "low-answer-waits"])
def test_offline_eval_of_turn_end(events, early, cut_at, gain) -> None:
    v = verdict(_row(events), 0.4, 0.5)
    assert v["early"] == early
    assert (v["cut"] or {}).get("at") == cut_at
    if gain is not None:
        assert v["gain"] == pytest.approx(gain)


def test_next_phrase_soon_after_marks_continuation() -> None:
    # первая кончилась в 10,0 (ts 10,5 минус stt 0,5); вторая длиной 2 с кончилась в 12,5 - началась в 10,5
    rows = [{"wav": "a", "ts": "2026-10-09T00:00:10.5+06:00", "stt": 0.5, "secs": 3.0},
            {"wav": "b", "ts": "2026-10-09T00:00:12.5+06:00", "stt": 0.0, "secs": 2.0},
            {"wav": "c", "ts": "2026-10-09T00:00:30.0+06:00", "stt": 0.0, "secs": 2.0}]
    assert continued(rows, gap=1.0) == {"a"}


@pytest.mark.integration
def test_smart_turn_model_gives_probability() -> None:
    if not TURN_END_MODEL.exists():
        pytest.skip("модели Smart Turn нет в data/voice/smart-turn")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("transformers")
    from voice.layer.turn_end import SmartTurn
    rng = np.random.default_rng(0)
    p = SmartTurn(TURN_END_MODEL).probability((rng.standard_normal(SAMPLE_RATE) * 1000).astype(np.int16))
    assert 0.0 <= p <= 1.0
