"""Живое сжатие истории на LM Studio (SAM-36): привычки и договорённости переживают сводку.

Запуск: pytest -m integration tests/integration/test_live_compression.py. Нужна загруженная модель в LM Studio;
облачные поставщики не берутся. История и архив - mongomock, сводку пишет настоящий суммаризатор шлюза."""

from __future__ import annotations

import pytest

from gateway import compression
from gateway.prompts import is_trivial_summary
from gateway.repos import ArchiveRepo, SessionMetaRepo, SessionRepo
from tests.fixtures.sample_dialogues import SCENARIO_HABITS_VS_EPISODICS

pytestmark = [pytest.mark.integration, pytest.mark.slow]

LIVE_SOURCE = "lmstudio"
SESSION = "scenario1"


async def test_live_compression_keeps_habits(mongo_db):
    from gateway.agent import _summarize_chunk
    from gateway.llm_router import get_best_llm

    llm = await get_best_llm()
    if not llm or llm.get("source") != LIVE_SOURCE:
        pytest.skip(f"нужна модель в LM Studio, выбран {llm and llm.get('source')}: загрузи модель в LM Studio")
    for m in SCENARIO_HABITS_VS_EPISODICS["messages"]:
        mongo_db.sessions.insert_one({**m, "session_id": SESSION})

    await compression.compress_session_history(
        SESSION, llm, session_repo=SessionRepo(mongo_db), archive_repo=ArchiveRepo(mongo_db),
        meta_repo=SessionMetaRepo(mongo_db), summarizer=_summarize_chunk,
        is_trivial_summary=is_trivial_summary, force=True)

    summaries = " ".join(d["content"].lower() for d in mongo_db.sessions.find({"is_summary": True}))
    assert summaries, "сжатие не оставило сводки"
    lost = [w for w in SCENARIO_HABITS_VS_EPISODICS["facts_must_keep"] if w not in summaries]
    assert not lost, f"потеряны устойчивые факты {lost}: {summaries[:500]}"
