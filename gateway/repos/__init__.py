"""Repositories — слой доступа к MongoDB.

Все прямые обращения к коллекциям (sessions, sessions_archive,
worker_sessions, _sessions_meta) идут только через эти классы.
agent.py больше не знает про структуру коллекций.

Это первый шаг к чистой архитектуре: domain (agent loop, compression)
зависит от repos через интерфейс, не от MongoDB напрямую.
"""

from .session_repo import SessionRepo
from .archive_repo import ArchiveRepo
from .meta_repo import SessionMetaRepo
from .memory_repo import MemoryRepo
from .token_usage_repo import TokenUsageRepo
from .dialogue_notes_repo import DialogueNotesRepo

__all__ = ["SessionRepo", "ArchiveRepo", "SessionMetaRepo", "MemoryRepo", "TokenUsageRepo", "DialogueNotesRepo"]
