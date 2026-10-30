"""Сервис души SAM (её характера): засев хранилища из заготовки, чтение, правка и откат разделов с проверкой.

Один для инструментов SAM (gateway/tools/soul.py) и REST монитора (gateway/soul_api.py). Душа - только разделы
заготовки (gateway/prompts/soul_seed.md): остальные записи хранилища не показываются и не правятся, инструкции живут
в SAM.md, личный слой - в data/SAM.md. Засев: разделы заготовки, которых в хранилище нет, дописываются; раздел,
который не правили, следует за новой заготовкой, правленый остаётся как есть (строка в лог). Записи вне заготовки,
которые никто не правил (автор seed или перенос из data/SAM.md), засев удаляет: их текст есть в файлах репозитория и
data/. Правленую запись вне заготовки засев не трогает и называет в логе: текст правки переносят в файл руками.
Правка проверяется сборкой снимка с новым текстом (gateway/prompts/soul.py compose): битая не пишется, причина одной
строкой. После записи кэш снимка сбрасывается, и правка доходит до следующего хода. Решения (проверка, выбор версии
отката, что удалить) отделены от хранилища: репозиторий и подстановки приходят параметрами.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .prompts import personal, soul
from .prompts.prompt_md import unknown_placeholders
from .repos.soul_repo import SoulRepo

HISTORY_ENV = "SAM_SOUL_HISTORY"
DEFAULT_HISTORY = 20   # версий раздела для отката: правка в день, три недели назад
DB_TIMEOUT_ENV = "SAM_SOUL_DB_TIMEOUT_MS"
DEFAULT_DB_TIMEOUT_MS = 1500   # бриф ведущего и скрипты без Mongo не ждут дольше полутора секунд
DB_NAME = "sam"   # база шлюза (gateway/agent.py)
SEED_AUTHOR = "seed"
SEED_HASH_CHARS = 16   # 64 бита sha256: случайного совпадения у двух редакций раздела не будет
MIGRATED_AUTHOR = "data/SAM.md"   # автор записи _personal, перенесённой из файла, пока личный слой жил в хранилище
UNTOUCHED_AUTHORS = frozenset({SEED_AUTHOR, MIGRATED_AUTHOR})
LOG_TAG = soul.LOG_TAG
UNAVAILABLE = "хранилище души недоступно (Mongo); правка не записана, проверь docker"


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str
    version: int = 0
    conflict: bool = False   # раздел изменился после того, как автор его прочитал


def changed(name: str, version: int) -> Result:
    return Result(False, f"раздел {name} изменился (v{version}), перечитай", version, conflict=True)


def seed_hash(text: str) -> str:
    """Отпечаток текста заготовки: засев узнаёт, изменилась ли заготовка с прошлого засева."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:SEED_HASH_CHARS]


def history_limit() -> int:
    return int(os.getenv(HISTORY_ENV, "") or DEFAULT_HISTORY)


def stale_records(state: Mapping[str, Mapping], required: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Записи хранилища вне заготовки: (никем не правленные - удалить, правленные - оставить и назвать в логе)."""
    extra = [name for name in state if name not in required]
    untouched = [name for name in extra if state[name].get("author") in UNTOUCHED_AUTHORS]
    return untouched, [name for name in extra if name not in untouched]


def rollback_target(history: list[dict], version: int | None) -> dict | str:
    """Версия для отката из истории (текущая первой): без номера - предыдущая. Нельзя - причина строкой."""
    if len(history) < 2:
        return "у записи нет прошлых версий"
    if version is None:
        return history[1]
    if version == history[0].get("version"):
        return f"версия {version} и так текущая"
    for entry in history[1:]:
        if entry.get("version") == version:
            return entry
    return f"нет версии {version}; есть: {', '.join(str(e.get('version')) for e in history[1:])}"


class SoulService:
    def __init__(self, repo: SoulRepo, read_seed: Callable[[], Mapping[str, str]] = soul.read_seed,
                 values: Callable[[], dict] = personal.values,
                 invalidate: Callable[[], None] = soul.invalidate, log: Callable[[str], None] = print):
        self._repo, self._read_seed, self._values = repo, read_seed, values
        self._invalidate, self._log = invalidate, log
        self._seeded = False

    @property
    def available(self) -> bool:
        return self._repo.available

    def required(self) -> tuple[str, ...]:
        """Разделы заготовки; заготовка недоступна - обязательные разделы снимка процесса, без SoulError в ход."""
        try:
            return soul.required_sections(self._read_seed())
        except soul.SoulError as exc:
            self._log(f"{exc.code}; обязательные разделы - по последней рабочей версии")
            return soul.store().required()

    def seed(self, refresh: bool = True) -> list[str]:
        """Засев из заготовки; вернуть имена засеянных, обновлённых и удалённых записей.

        Записей нет - засеять. Раздел, который в базе не правили (автор seed), а в заготовке он другой, - обновить из
        заготовки; правили - строка в лог, раздел не трогать (хэш заготовки, с которой он засеян, лежит в записи).
        Записи вне заготовки - stale_records: неправленые удалить, правленые назвать в логе."""
        if not self.available:
            return []
        try:
            seed = dict(self._read_seed())
        except soul.SoulError as exc:
            self._log(f"{exc.code}; засев пропущен")
            return []
        state = self._repo.seed_state()
        self._seeded = True
        order = {name: i for i, name in enumerate(seed)}
        hashes = {name: {"seed_hash": seed_hash(text)} for name, text in seed.items()}
        changed_names = self._repo.insert_missing({k: v for k, v in seed.items() if k not in state}, order,
                                                  SEED_AUTHOR, hashes)
        for name, text in seed.items():
            if name in state and self._section_update(name, text, state[name]):
                changed_names.append(name)
        if changed_names:
            self._log(f"{LOG_TAG} из заготовки: {', '.join(changed_names)}")
        changed_names += self._retire(state, soul.required_sections(seed))
        if changed_names and refresh:
            self._invalidate()
        return changed_names

    def _section_update(self, name: str, text: str, doc: dict) -> bool:
        """Раздел без правок обновить из новой заготовки; с правкой - строка в лог."""
        new_hash = seed_hash(text)
        if doc.get("seed_hash") == new_hash:
            return False
        if doc.get("author") != SEED_AUTHOR:
            if doc.get("seed_hash"):   # без хэша (засеяно до хэшей) не узнать, менялась ли заготовка
                self._log(f"{LOG_TAG} заготовка раздела {name} изменилась, в базе правка человека, не трогаю")
            return False
        if doc.get("text") == text:
            self._repo.set_fields(name, {"seed_hash": new_hash})
            return False
        return bool(self._repo.put(name, text, SEED_AUTHOR, int(doc.get("version") or 0),
                                   extra={"seed_hash": new_hash}))

    def _retire(self, state: Mapping[str, Mapping], required: tuple[str, ...]) -> list[str]:
        """Удалить неправленые записи вне заготовки, правленые назвать в логе; вернуть имена удалённых."""
        untouched, edited = stale_records(state, required)
        removed = self._repo.delete(untouched) if untouched else []
        if removed:
            self._log(f"{LOG_TAG} не душа, удалены из хранилища: {', '.join(removed)}; инструкции - в SAM.md, "
                      f"личный слой - в data/SAM.md")
        if edited:
            self._log(f"{LOG_TAG} не душа, правленые записи оставлены и не читаются: {', '.join(edited)}; перенеси "
                      f"текст в SAM.md или data/SAM.md и удали запись")
        return removed

    def _ready(self) -> bool:
        """Хранилище есть и засеяно: первое удачное обращение засевает и сверяет с заготовкой."""
        if not self.available:
            return False
        if not self._seeded:
            self._seeded = True   # до засева: снимок, который засев перечитывает, сюда не возвращается
            try:
                self.seed(refresh=False)
            except Exception:
                self._seeded = False
                raise
        return True

    def _soul_only(self, records: Mapping[str, str]) -> dict[str, str]:
        required = self.required()
        return {name: text for name, text in records.items() if name in required}

    def load(self) -> dict[str, str] | None:
        """Разделы души для снимка (soul.configure); хранилища нет - None."""
        return self._soul_only(self._repo.load()) if self._ready() else None

    def entries(self) -> list[dict]:
        if not self._ready():
            return []
        required = self.required()
        return [entry for entry in self._repo.entries() if entry["_id"] in required]

    def show(self, name: str) -> dict | None:
        return self._repo.get(name) if self._ready() and name in self.required() else None

    def history(self, name: str) -> list[dict]:
        return self._repo.history(name) if self._ready() and name in self.required() else []

    def write(self, name: str, text: str, author: str, expected: int | None = None) -> Result:
        """Заменить текст записи поверх версии expected, которую видел автор (None - текущей). Битая структура, чужая
        или одновременная правка - отказ одной строкой."""
        if not self._ready():
            return Result(False, UNAVAILABLE)
        current = self._soul_only(self._repo.load())
        if name not in current:
            return Result(False, f"{name} не раздел души; есть: {', '.join(sorted(current))}; инструкции - в SAM.md, "
                                 f"их правят в репозитории")
        candidate = {**current, name: text}
        try:
            composed = soul.compose(candidate, self.required())
        except soul.SoulError as exc:
            return Result(False, f"{exc.code}; правка не записана")
        seen = self._repo.version(name) if expected is None else expected
        version = self._repo.put(name, text, author, seen)
        if not version:
            return changed(name, self._repo.version(name))
        self._invalidate()
        return Result(True, f"{name}: версия {version}, дойдёт до следующего хода{self._warning(composed)}", version)

    def rollback(self, name: str, version: int | None, author: str, expected: int | None = None) -> Result:
        if not self._ready():
            return Result(False, UNAVAILABLE)
        target = rollback_target(self._repo.history(name), version)
        if isinstance(target, str):
            return Result(False, f"{name}: {target}")
        return self.write(name, target.get("text", ""), f"{author}, откат к версии {target.get('version')}", expected)

    def _warning(self, composed: soul.Soul) -> str:
        values = self._values()
        unknown = sorted({key for text in composed.sections.values() for key in unknown_placeholders(text, values)})
        return (f"; нет значения для {', '.join('{' + k + '}' for k in unknown)} - задай в шапке SAM.md или data/SAM.md"
                if unknown else "")


def mongo_connect(uri: str | None = None, timeout_ms: int | None = None):
    """База шлюза, если Mongo отвечает; иначе исключение драйвера."""
    from pymongo import MongoClient
    client = MongoClient(uri or os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
                         serverSelectionTimeoutMS=timeout_ms or int(os.getenv(DB_TIMEOUT_ENV, "") or DEFAULT_DB_TIMEOUT_MS))
    client.admin.command("ping")
    return client[DB_NAME]


class LazyDb:
    """Подключение, которое пробуется не чаще раза в retry_s, пока не удастся: шлюз, стартовавший без Mongo, начинает
    писать душу, когда Mongo поднялась."""

    def __init__(self, connect: Callable[[], object], retry_s: float, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        self._connect, self._retry_s, self._clock, self._log = connect, retry_s, clock, log
        self._next = 0.0
        self._failed = False

    def __call__(self):
        if self._clock() < self._next:
            return None
        try:
            db = self._connect()
        except Exception as exc:   # драйвер: сеть, таймаут выбора сервера
            self._next = self._clock() + self._retry_s
            if not self._failed:
                self._failed = True
                self._log(f"{LOG_TAG} Mongo недоступна ({str(exc).splitlines()[0][:120]}); пробую раз в {self._retry_s:.0f} с")
            return None
        if self._failed:
            self._log(f"{LOG_TAG} Mongo поднялась, душа снова пишется в хранилище")
        return db


_default: list[SoulService] = []


def default_service() -> SoulService:
    """Сервис процесса без своего корня композиции (бриф ведущего, скрипты): Mongo по MONGODB_URI с коротким
    ожиданием. Шлюз собирает свой сервис на общем подключении (gateway/agent.py)."""
    if not _default:
        _default.append(SoulService(SoulRepo(None, history_limit(), connect=LazyDb(mongo_connect, soul.retry_s()))))
    return _default[0]
