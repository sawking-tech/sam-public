"""list_memories: обзор без фильтра режет факт, по тегу запись читается целиком (трек self-tutor, карточка человека)."""
from gateway.tools.memory import LIST_PREVIEW_CHARS, LIST_TAG_TEXT_CHARS, LIST_TAG_TOTAL_CHARS, list_memories_impl

LONG = "образец задания дословно " * 40


class FakeRepo:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    def find_many(self, query: dict, sort=None, limit: int = 0) -> list[dict]:
        tags = query.get("tags", {}).get("$in")
        return [d for d in self.docs if not tags or set(tags) & set(d["tags"])]


def test_tag_filter_returns_whole_record() -> None:
    repo = FakeRepo([{"text": LONG, "tags": ["track", "t:python:library"]}])
    out = list_memories_impl({"tag": "t:python:library"}, store=None, memory_repo=repo)
    assert len(LONG) < LIST_TAG_TEXT_CHARS and LONG in out


def test_overview_without_filter_keeps_preview() -> None:
    repo = FakeRepo([{"text": LONG, "tags": ["track"]}])
    out = list_memories_impl({}, store=None, memory_repo=repo)
    assert LONG[:LIST_PREVIEW_CHARS] in out and LONG not in out


def test_tag_output_is_capped_in_total() -> None:
    repo = FakeRepo([{"text": f"{i} " + "x" * (LIST_TAG_TEXT_CHARS - 10), "tags": ["person"]} for i in range(10)])
    out = list_memories_impl({"tag": "person"}, store=None, memory_repo=repo)
    assert len(out) < LIST_TAG_TOTAL_CHARS + LIST_TAG_TEXT_CHARS and "не показано" in out
