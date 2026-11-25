"""Отметить место устройства для отпечатков радара (SAM-118): несколько сообщений «я здесь с устройством» подряд, по одному отпечатку на каждое.

Запуск: python scripts/radar_mark_place.py "диван в зале" [--device <часть имени>] [--samples 5] [--interval 2] [--delay 30]
Адрес радара - SAM_RADAR_URL (по умолчанию http://localhost:8010)."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

DEFAULT_URL = "http://localhost:8010"
DEFAULT_DEVICE = "phone"      # подстрока имени устройства, которое носит человек
DEFAULT_SAMPLES = 5          # отпечатков за отметку: уровень шумит, одного мало (в классификации нужно не менее 3 на место)
DEFAULT_DELAY_S = 0.0        # пауза до первого отсчёта: человек успевает дойти и лечь с устройством, писать в чат ему нечем
DEFAULT_INTERVAL_S = 2.0     # между отсчётами уровень успевает обновиться (опрос роутера около секунды)
TIMEOUT_S = 5.0
ENDPOINT = "/sam/presence/message"
CHANNEL = "told"


def payload(place: str, device: str, who: str) -> dict:
    return {"channel": CHANNEL, "device": device, "who": who, "place": place}


def post(url: str, body: dict) -> None:
    request = urllib.request.Request(url + ENDPOINT, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=TIMEOUT_S).read()


def mark(url: str, place: str, device: str, who: str, samples: int, interval: float, delay: float, send=post, sleep=time.sleep) -> None:
    """Пауза `delay`, затем `samples` отсчётов через `interval`; отправка и сон передаются снаружи ради проверки без сети."""
    sleep(delay)
    for i in range(samples):
        send(url, payload(place, device, who))
        if i + 1 < samples:
            sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("place")
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--who", required=True, help="ключ человека из data/presence_owners.json")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S)
    args = ap.parse_args()
    url = os.environ.get("SAM_RADAR_URL", DEFAULT_URL)
    try:
        mark(url, args.place, args.device, args.who, args.samples, args.interval, args.delay)
    except OSError as e:
        print(f"радар не отвечает на {url}: {e}; запустить: powershell -File start_radar.ps1")
        return 1
    print(f"отмечено «{args.place}»: {args.samples} отсчётов для {args.device}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
