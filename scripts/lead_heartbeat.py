"""Живость сессии Claude Code - основной модели SAM (SAM-91): отметка lead.alive, пока процесс жив.

  python -u scripts/lead_heartbeat.py      (фоновый процесс сессии; у Monitor лимит 30 мин, у него - нет)

Конец сессии снимает отметку хуком SessionEnd (.claude/settings.json), шлюз тут же видит, что сессии нет.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUS = Path(os.getenv("SAM_VOICE_BUS_DIR", "").strip() or REPO / "data" / "voice" / "bus")
ALIVE = BUS / "lead.alive"          # то же имя, что voice/layer/bus.py LEAD_ALIVE
BEAT_S = float(os.getenv("SAM_LEAD_BEAT_S", "1"))   # чаще порога шлюза SAM_LEAD_ALIVE_S (5 с) в пять раз


def main() -> None:
    while True:
        ALIVE.touch()
        time.sleep(BEAT_S)


if __name__ == "__main__":
    main()
