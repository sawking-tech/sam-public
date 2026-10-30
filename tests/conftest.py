"""Общие фикстуры для pytest."""

from __future__ import annotations

import os

import pytest

# Доступ к gateway (SAM-79): клиент TestClient приходит с адреса testclient, он считается этой
# машиной; ключ из .env в тесты не попадает (load_dotenv не перезаписывает заданную переменную)
os.environ.setdefault("SAM_LOCAL_HOSTS", "127.0.0.1,::1,localhost,testclient")
os.environ["SAM_API_KEY"] = ""
os.environ["SAM_ALLOWED_HOSTS"] = "testserver"   # Host запросов TestClient и фейков (gateway/access.py host_reason)
os.environ["SAM_WORKFLOWS"] = "0"
os.environ["SAM_STAND_WATCH_KEEP_S"] = "0"   # шлюз и модули в тестах надзор стенда не поднимают (SAM-195)
os.environ["SAM_CORTEX_WARM"] = "0"   # шлюз в тестах не поднимает процесс Cortex при старте (SAM-195)
os.environ["SAM_SESSION_SWEEP"] = "0"   # старт шлюза в тестах не убирает сессии (gateway/session_purge.py)
os.environ["SAM_APPS_REVIVE"] = "0"   # старт шлюза в тестах не поднимает приложения настоящим Docker
# Имена домашних стенда лежат в .env; тесты берут свои выдуманные, чтобы прогон от .env не зависел
os.environ["SAM_SPEAKER_NAMES"] = "owner=Антон,owner-loud=Антон,partner=Ирина,friend=Петя"   # id профилей - прежние
os.environ["SAM_CAT_NAMES"] = "барс,мурк,кис"
os.environ["SAM_LOCAL_DEVICE"] = "workpc"
# Mongo стенда тесты не трогают: импорт шлюза подключался к нему и писал учёт токенов тестов в _token_usage стенда.
# Закрытый порт - подключение отказывает сразу, тесты с Mongo идут через mongomock
os.environ["MONGODB_URI"] = "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200"
# Ключ привилегий ручек агентов (gateway/privilege_key.py): свой файл во временной папке, файл стенда тесты не трогают
import tempfile  # noqa: E402
os.environ["SAM_PRIVILEGE_FILE"] = os.path.join(tempfile.mkdtemp(prefix="sam-test-"), "privilege.key")
# Синтез в тестах не ходит в живой Silero/CosyVoice стенда (10 окт ночью pytest агентов синтезировал
# «Третий час, ложись» на живом :10202 - строки в логе синтеза): закрытый порт, клиент получает отказ сразу
os.environ["WYOMING_SILERO"] = "127.0.0.1:1"
os.environ["WYOMING_COSY"] = "127.0.0.1:1"   # планировщик workflow стенда (SAM-96) не стартует в тестах TestClient
# Личный слой (gateway/prompts/personal.py): data/SAM.md стенда тесты не читают, имена - по умолчанию из шапки SAM.md
os.environ["SAM_PERSONAL_FILE"] = os.path.join(tempfile.mkdtemp(prefix="sam-test-"), "SAM.md")
# Душа SAM (gateway/prompts/soul.py): тесты собирают характер из заготовки, а не из Mongo стенда; копия души стенда
# (data/soul-cache.json) в тесты не попадает. Тесты хранилища души задают свой источник через soul.configure
os.environ["SAM_SOUL_COPY"] = os.path.join(tempfile.mkdtemp(prefix="sam-test-"), "soul-cache.json")
from gateway.prompts import soul as _soul  # noqa: E402
_soul.configure(_soul.read_seed)

# Шлюз в тестах ходит в Mongo стенда: запись реплики через общую шину не ставит сжатие живых сессий (SAM-97).
# Свои шины тестов (EventBus()) слушателя получают как обычно
from gateway.event_bus import event_bus as _shared_bus  # noqa: E402
_shared_bus.set_history_listener = lambda listener: None


@pytest.fixture
def mongo_db():
    """In-memory MongoDB клиент через mongomock.

    Используется везде где код вызывает _db.X.find/update/delete напрямую.
    Изолирован между тестами — каждый тест получает чистую БД.
    """
    import mongomock
    client = mongomock.MongoClient()
    db = client["sam_test"]
    yield db
    client.close()


@pytest.fixture
def fake_llm_response():
    """Простая фабрика мок-ответов LLM в формате OpenAI completion.

    Использование:
        async def fake(*args, **kwargs):
            return fake_llm_response("ОТВЕТ от LLM")
        monkeypatch.setattr(llm_client, "make_llm_call", fake)
    """
    class _Choice:
        def __init__(self, content: str, tool_calls: list | None = None):
            self.message = _Message(content, tool_calls)
            self.finish_reason = "stop"

    class _Message:
        def __init__(self, content: str, tool_calls: list | None):
            self.content = content
            self.tool_calls = tool_calls

    class _Response:
        def __init__(self, content: str, tool_calls: list | None = None):
            self.choices = [_Choice(content, tool_calls)]

    def factory(content: str, tool_calls: list | None = None):
        return _Response(content, tool_calls)

    return factory


@pytest.fixture
def llm_config():
    """Минимальная конфигурация LLM для тестов (как возвращает llm_router)."""
    return {
        "model": "fake-model",
        "model_server": "http://localhost:1234/v1",
        "api_key": "test",
        "source": "test",
        "prompt_format": "standard",
    }


@pytest.fixture(autouse=True)
def _own_cooldown_file(tmp_path, monkeypatch):
    """Долгие паузы поставщиков пишутся в файл (gateway/cooldown_store.py): тесты с 403 не трогают настоящий data/."""
    from gateway import llm_client
    monkeypatch.setattr(llm_client, "_COOLDOWN_PATH", tmp_path / "llm_cooldowns.json")


@pytest.fixture(autouse=True)
def _fresh_lmstudio_cache():
    """Проверка LM Studio кэшируется на секунды (llm_router._lmstudio_cache): тесты с подменой сети её не делят."""
    from gateway import llm_router
    llm_router._lmstudio_cache.clear()
    yield
    llm_router._lmstudio_cache.clear()
