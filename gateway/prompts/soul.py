"""Душа SAM - её характер (кто она, характер и принципы, тон) по разделам: проверка структуры и кэш снимка из хранилища.

Душа живёт в хранилище SAM (коллекция `_soul`, gateway/repos/soul_repo.py) записями «раздел -> текст». Инструкции
(как делать дела, слышать, говорить, инструменты) - не душа: они в SAM.md (gateway/prompts/instructions.py), имена
и личные инструкции - в личном слое data/SAM.md (gateway/prompts/personal.py). Формат текста -
gateway/prompts/prompt_md.py; в промпт раздел идёт без комментариев и краевых пустых строк. Какие разделы и в каком
порядке складываются в промпт, решают модули сборки (system.py, voice.py, lead.py), этот модуль только читает.

Заготовка - soul_seed.md рядом (`SAM_SOUL_SEED`, относительный путь - от корня репозитория): разделы `## ИМЯ`. Её
разделы - вся душа: снимок без любого из них или с пустым не принимается, сборка берёт последнюю рабочую версию
записи и пишет одну строку в лог; записи хранилища вне заготовки в снимок не идут. Сборка идёт в ходе, а SoulError -
SystemExit: из чтения снимка он не выходит.

Снимок кэшируется на процесс; запись через сервис души (gateway/soul_service.py) сбрасывает кэш (invalidate), и правка
доходит до следующей сборки без рестарта. Принятый снимок пишется копией в data/soul-cache.json (`SAM_SOUL_COPY`).
Хранилище недоступно - последний рабочий снимок, иначе копия, иначе заготовка; повтор не раньше `SAM_SOUL_RETRY_S`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .prompt_md import REPO_ROOT, PromptError, clean, frontmatter_lines, raw_sections, resolve

SEED_ENV = "SAM_SOUL_SEED"
DEFAULT_SEED = Path(__file__).resolve().parent / "soul_seed.md"
RETRY_ENV = "SAM_SOUL_RETRY_S"
DEFAULT_RETRY_S = 30.0   # хранилище недоступно: копия в ходе, новая попытка не чаще раза в полминуты
COPY_ENV = "SAM_SOUL_COPY"
DEFAULT_COPY = REPO_ROOT / "data" / "soul-cache.json"   # data/ вне git, рядом с личным слоем
LOG_TAG = "[Soul]"

SoulError = PromptError   # душу не собрать: SystemExit со строкой


@dataclass(frozen=True)
class Soul:
    sections: dict = field(default_factory=dict)   # имя раздела -> текст для промпта


def seed_path() -> Path:
    return resolve(os.getenv(SEED_ENV, "").strip() or DEFAULT_SEED)


def seed_entries(text: str, source: str = "soul_seed.md") -> dict:
    """Записи души из текста заготовки в порядке файла, как написаны, с комментариями; шапка, если есть, не душа."""
    _, body = frontmatter_lines(text, source, LOG_TAG)
    return raw_sections(body)


def read_seed(path: Path | None = None) -> dict:
    path = path or seed_path()
    try:
        return seed_entries(path.read_text(encoding="utf-8"), path.name)
    except FileNotFoundError:
        raise SoulError(f"{LOG_TAG} нет заготовки {path}; верни gateway/prompts/soul_seed.md или укажи путь в {SEED_ENV}")
    except (OSError, UnicodeDecodeError) as exc:
        raise SoulError(f"{LOG_TAG} {path} не читается ({exc}); проверь файл и кодировку UTF-8")


def required_sections(seed: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(name for name in seed if not name.startswith("_"))


# ── Сборка снимка из записей и проверка структуры ────────────────────────────

def compose(entries: Mapping[str, str], required: tuple[str, ...]) -> Soul:
    """Снимок души из записей «имя -> текст»: только разделы заготовки (required). Нет раздела или он пуст -
    SoulError одной строкой, что не так и что делать."""
    missing = [name for name in required if not clean(entries.get(name) or "").strip()]
    if missing:
        raise SoulError(f"{LOG_TAG} нет раздела или он пуст: {', '.join(missing)}; верни текст или откати раздел")
    names = required or tuple(name for name in entries if not name.startswith("_"))
    return Soul({name: clean(entries.get(name) or "") for name in names})


class SoulCopy:
    """Копия последней рабочей души в файле (data/, вне git): без хранилища сборка идёт из неё, с правками, а не из
    заготовки без них. Пишется атомарно и только при изменении."""

    def __init__(self, path: Path, log: Callable[[str], None] = print):
        self.path, self._log = path, log
        self._written: dict | None = None

    def read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self._log(f"{LOG_TAG} копия {self.path} не читается ({exc}); без неё")
            return None
        if not isinstance(data, dict):
            return None
        return {str(k): v for k, v in data.items() if isinstance(v, str)}

    def write(self, entries: Mapping[str, str]) -> None:
        data = dict(entries)
        if data == self._written:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._written = data
        except OSError as exc:
            self._log(f"{LOG_TAG} копия {self.path} не записана ({exc}); без хранилища возьму прежнюю копию")


def _short(exc: BaseException) -> str:
    text = str(exc.code if isinstance(exc, SystemExit) else exc)
    return text.splitlines()[0] if text else type(exc).__name__


def in_thread(job: Callable[[], None]) -> None:
    """Фоновое обновление снимка: синхронный драйвер Mongo не держит event loop хода."""
    threading.Thread(target=job, name="soul-refresh", daemon=True).start()


PREVIOUS, COPY, SEED = "последнюю рабочую версию", "копию", "заготовку"   # источники снимка без хранилища


class SoulStore:
    """Кэш снимка души. load() - записи из хранилища (исключение или None - хранилище недоступно), seed() - записи
    заготовки, copy - копия последней рабочей души в файле.

    Запись, которая не текст, или пустой раздел заготовки - строка в лог и последняя рабочая версия этой записи.
    Хранилища нет - последний рабочий снимок процесса, иначе копия, иначе заготовка; строка лога называет источник.
    Заготовка недоступна - то, что есть, без исключения: SoulError - SystemExit, в ход он не выходит."""

    def __init__(self, load: Callable[[], Mapping[str, str] | None], seed: Callable[[], Mapping[str, str]],
                 retry_s: float, log: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
                 copy: SoulCopy | None = None, background: Callable[[Callable[[], None]], None] = in_thread):
        self._load, self._seed, self._retry_s, self._log, self._clock = load, seed, retry_s, log, clock
        self._copy, self._background = copy, background
        self._refreshing = False                    # фоновое обновление уже идёт
        self._lock = threading.RLock()
        self._soul: Soul | None = None              # снимок для сборки
        self._good: dict | None = None              # последние рабочие записи из хранилища
        self._retry_at: float | None = None         # снимок не из хранилища: когда снова спросить его
        self._required: tuple[str, ...] | None = None
        self._said: set[str] = set()                # строки сбоя, уже сказанные

    def _say(self, line: str) -> None:
        if line not in self._said:   # повтор раз в retry_s не засоряет лог той же строкой
            self._said.add(line)
            self._log(line)

    def _safe_seed(self) -> dict | None:
        try:
            return dict(self._seed())
        except SoulError as exc:
            self._say(f"{_short(exc)}; без заготовки")
            return None

    def required(self) -> tuple[str, ...]:
        """Обязательные разделы - разделы заготовки; заготовки нет - разделы последней рабочей версии."""
        if self._required is None:
            seed = self._safe_seed()
            if seed is None:
                return required_sections(self._good or {})
            self._required = required_sections(seed)
        return self._required

    def refresh(self) -> Soul:
        """Перечитать снимок в потоке вызывающего: запись души (поток инструмента или REST), старт шлюза."""
        fresh = self._reload()
        with self._lock:
            self._soul = fresh
        return fresh

    def invalidate(self) -> None:
        """После записи: снимок перечитывается здесь же, в потоке писавшего, и следующий ход видит правку."""
        self.refresh()

    def _refresh_in_background(self) -> None:
        try:
            self.refresh()
        finally:
            self._refreshing = False

    def current(self) -> Soul:
        """Снимок для сборки. В Mongo ходит только первый вызов процесса (старт шлюза, бриф); повтор после сбоя
        хранилища идёт в фоне, ход берёт текущий снимок и не ждёт драйвер."""
        with self._lock:
            if self._soul is None:
                self._soul = self._reload()
            elif self._retry_at is not None and self._clock() >= self._retry_at and not self._refreshing:
                self._refreshing = True
                self._background(self._refresh_in_background)
            return self._soul

    def _base(self) -> dict:
        """Откуда брать рабочую версию битой записи: последняя рабочая, копия, заготовка."""
        return self._good or (self._copy.read() if self._copy else None) or self._safe_seed() or {}

    def _repair(self, entries: Mapping, base: Mapping[str, str]) -> dict:
        out: dict[str, str] = {}
        for name, text in entries.items():
            if isinstance(text, str):
                out[name] = text
                continue
            self._say(f"{LOG_TAG} запись {name} не текст ({type(text).__name__}); беру её последнюю рабочую версию")
            if isinstance(base.get(name), str):
                out[name] = base[name]
        for name in self.required():
            if not clean(out.get(name) or "").strip():
                self._say(f"{LOG_TAG} раздела {name} нет или он пуст; беру его последнюю рабочую версию")
                if base.get(name):
                    out[name] = base[name]
        return out

    def _reload(self) -> Soul:
        try:
            entries = self._load()
            reason = "хранилище недоступно"
        except Exception as exc:   # сеть, Mongo, драйвер: ход идёт на копии
            entries, reason = None, f"хранилище недоступно ({_short(exc)})"
        if entries is not None:
            try:
                good = self._repair(entries, self._base())
                composed = compose(good, self.required())
            except Exception as exc:   # SoulError и любой сбой разбора: снимок из хранилища не принят
                reason = _short(exc)
            else:
                self._good, self._retry_at = good, None
                if self._copy:
                    self._copy.write(good)
                return composed
        self._retry_at = self._clock() + self._retry_s
        return self._fallback(reason)

    def _fallback(self, reason: str) -> Soul:
        copy = None if self._good or not self._copy else self._copy.read()
        seed = None if self._good or copy else self._safe_seed()
        for source, entries in ((PREVIOUS, self._good), (COPY, copy), (SEED, seed)):
            if not entries:
                continue
            try:
                composed = compose(self._repair(entries, entries), self.required())
            except Exception as exc:
                self._say(f"{LOG_TAG} {source} не собрать ({_short(exc)})")
                continue
            where = f"{COPY} {self._copy.path}" if source == COPY else source
            self._say(f"{LOG_TAG} {reason}; беру {where}")
            return composed
        self._say(f"{LOG_TAG} {reason}; души нет ни в хранилище, ни в копии, ни в заготовке")
        return Soul()


def _default_load() -> Mapping[str, str] | None:
    """Хранилище процесса без своего корня композиции (бриф ведущего, скрипты): Mongo по MONGODB_URI."""
    from gateway.soul_service import default_service
    return default_service().load()


def retry_s() -> float:
    return float(os.getenv(RETRY_ENV, "") or DEFAULT_RETRY_S)


def copy_path() -> Path:
    return resolve(os.getenv(COPY_ENV, "").strip() or DEFAULT_COPY)


_store = SoulStore(_default_load, read_seed, retry_s(), copy=SoulCopy(copy_path()))


def configure(load: Callable[[], Mapping[str, str] | None],
              seed: Callable[[], Mapping[str, str]] = read_seed, copy: SoulCopy | None = None,
              background: Callable[[Callable[[], None]], None] = in_thread) -> SoulStore:
    """Корень композиции (шлюз, тесты) задаёт, откуда брать записи души и где держать копию; кэш сбрасывается."""
    global _store
    _store = SoulStore(load, seed, retry_s(), copy=copy, background=background)
    return _store


def store() -> SoulStore:
    return _store


def invalidate() -> None:
    _store.invalidate()


def soul() -> Soul:
    return _store.current()
