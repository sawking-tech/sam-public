"""Узел звука (ADR-017): формат кадра, раздача подписчикам и TCP-вход, без звука."""
import threading

import numpy as np
import pytest

from voice.layer import frames, vmic

WAIT_S = 5.0   # потолок ожидания события по TCP на 127.0.0.1; в норме - миллисекунды


def test_frames_survive_split_and_batched_delivery():
    block = np.arange(-800, 800, dtype=np.int16)
    data = frames.encode(block, 12.5)
    dec = frames.Decoder()
    assert dec.feed(data[:7]) == []                      # заголовок пришёл не целиком
    (ts, got), = dec.feed(data[7:])
    assert ts == 12.5 and np.array_equal(got, block)

    a, b = np.ones(4, np.int16), np.full(3, 7, np.int16)   # два кадра одним чтением
    out = frames.Decoder().feed(frames.encode(a, 1.0) + frames.encode(b, 2.0))
    assert [ts for ts, _ in out] == [1.0, 2.0] and np.array_equal(out[1][1], b)

    fan = frames.Fanout()                                  # медленный подписчик теряет старые, не держит других
    slow, fast, gone = fan.add(limit=2), fan.add(limit=10), fan.add()
    fan.remove(gone)
    for i in range(5):
        fan.publish(bytes([i]))
    assert list(slow.frames) == [bytes([3]), bytes([4])] and slow.dropped == 3
    assert len(fast.frames) == 5 and fast.dropped == 0
    assert fan.count == 2 and gone.take(0) is None


class _JoinSignal(frames.Fanout):
    """Настоящая раздача, которая ставит событие, когда подписчик подключился."""

    def __init__(self):
        super().__init__()
        self.joined = threading.Event()

    def add(self, limit: int = frames.QUEUE_FRAMES) -> frames.Subscriber:
        sub = super().add(limit)
        self.joined.set()
        return sub


@pytest.mark.slow
def test_virtual_mic_hears_the_hub():
    fan = _JoinSignal()
    hub = vmic.FramePublisher("127.0.0.1", 0, fanout=fan)
    hub.start()
    got, ready = [], threading.Event()

    def listen():
        for block in vmic.VirtualMic("127.0.0.1", hub.port, reconnect_s=0.1).blocks():
            got.append(block)
            if len(got) == 2:
                ready.set()
                return
    threading.Thread(target=listen, daemon=True).start()
    try:
        assert fan.joined.wait(WAIT_S), "виртуальный микрофон не подключился к узлу"
        hub.publish(np.full(1600, 5, np.int16))
        hub.publish(np.full(1600, 9, np.int16))
        assert ready.wait(WAIT_S)
        assert got[0][0] == 5 and got[1][0] == 9 and len(got[1]) == 1600
    finally:
        hub.close()
