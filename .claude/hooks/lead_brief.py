"""SessionStart-хук: сессия Claude Code в папке SAM - основной агент SAM.

Срабатывает на startup, /clear, resume и compact (load-context.sh молчит 8 часов после первого показа, после /clear
ведущий оставался без роли и без разговора). Печатает роль (только среда), промпт SAM из gateway/prompts/lead.py в роде
её голоса (SAM-95: правила поведения те же, что у модели шлюза), шаги старта, раздел «После /clear» из current.md,
короткую выжимку памяти о доме (SAM-101: запрос шлюзу по фиксированным темам) и диалог шины за последние
SAM_LEAD_BRIEF_MINUTES минут; stdout SessionStart-хука попадает в контекст сессии.

Сессия - основной агент, если её открыл start_sam.ps1 (SAM_LEAD_SESSION=1, scripts/sam_lead.ps1) или тикер недавно
писал в шину. Сессию от start_sam первым сообщением просят выполнить шаги старта.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from voice.layer.turns import speaker_label  # noqa: E402  подпись реплики та же, что в тикере
from gateway.access import auth_headers  # noqa: E402  ключ шлюза, как у voice.layer.say
from gateway.prompts.identity import DEFAULT_GENDER  # noqa: E402
from gateway.prompts.lead import lead_prompt  # noqa: E402  промпт SAM из шлюза импортом, шлюз не нужен (SAM-95)
CURRENT_MD = REPO / ".claude" / "session-context" / "current.md"
BUS_DIR = Path(os.getenv("SAM_VOICE_BUS_DIR", "").strip() or REPO / "data" / "voice" / "bus")
BUS_FILES = ("voice-in.jsonl", "say-out.jsonl", "agenda.jsonl")  # слова людей, реплики SAM, её свои сообщения
MINUTES = int(os.getenv("SAM_LEAD_BRIEF_MINUTES", "30"))           # полчаса покрывают разговор до /clear
TAIL_LINES = 400                                                   # с конца файла хватает на полчаса разговора
MAX_LINES = int(os.getenv("SAM_LEAD_BRIEF_LINES", "120"))         # больше - шум в контексте новой сессии
TEXT_LIMIT = 200                                                  # длинные реплики обрезаются, суть видна
SECTION = re.compile(r"^## После /clear\s*$(.*?)(?=^## |\Z)", re.M | re.S)

LIVE_FILE = "ticks.jsonl"
LIVE_MINUTES = int(os.getenv("SAM_LEAD_LIVE_MINUTES", "10"))      # тиков дольше нет - SAM не запущена, хук молчит
LEAD_FLAG = "SAM_LEAD_SESSION"                                    # ставит вкладка start_sam (scripts/sam_lead.ps1)
ALIVE_FILE = "lead.alive"                                         # voice/layer/bus.py LEAD_ALIVE
ALIVE_S = float(os.getenv("SAM_LEAD_ALIVE_S", "5"))               # тот же порог, что у шлюза (gateway/agent.py)

# Роль - только среда: Claude Code - носитель, вход, выход, инструменты. Правила поведения - промпт SAM из шлюза (SAM-95), раздел ниже
ROLE = (
    "Эта сессия, скорее всего, основная модель SAM, голосовой системы дома: ты - SAM, Claude Code - носитель; "
    "история SAM и обещания в ней - твои, переписка сессии после /clear пропадает. Как SAM себя ведёт - промпт ниже.\n"
    "Среда SAM:\n"
    "- вход: слух и чат монитора, тиками;\n"
    "- шлюз отдаёт вход основной модели: тебе, пока сессия жива, иначе модели в шлюзе;\n"
    "- агенты: действия, поиск, код - агентам SAM (создаёшь через шлюз) или своим фоновым;\n"
    "- память: долгая память и история сессий - в шлюзе; факты к входу едут в тике (поле facts, тик rag), "
    "поиск - `GET /sam/memory/search?q=`;\n"
    "- выход: голос `python -m voice.layer.say` (колонки, монитор, история) - это и есть речь промпта; "
    "фразы не к SAM - молча;\n"
    "- твой основной терминал - монитор (веб или TUI): человек смотрит туда; свой shell - запасной, не основной;\n"
    "- текст без звука (схема псевдографикой, список) - `python -m voice.layer.say --text-only \"...\"` "
    "(или `-` и текст в stdin): в монитор моноширинно и в историю, не в колонки;\n"
    "- часы - `date` и время тиков; факты о людях и доме - сначала поиск в памяти SAM, "
    "услышанное новое - сразу в неё;\n"
    "- шлюз лежит, а с тобой говорят голосом - всё равно ответь `say` (он кладёт реплику в шину первой), "
    "без звука не оставляй;\n"
    "- ключи (SAM-88, SAM-104): тик с `secret: [имена]` - человек назвал ключи этих назначений; имя `command` - "
    "привилегированная команда из этого входа (права, настройки, удаление, push, выпуск) подтверждена: выполняй "
    "сразу, без вопросов и напоминаний про ключ; другие имена привилегий команд не открывают. Самих ключей в тике "
    "нет - не ищи, не записывай и не повторяй. Тик без поля - обычный вход, привилегированное без ключа не "
    "выполняется;\n"
    "- тик с `push_ids` (шаг workflow через очередь push): ответив, подтверди их инструментом push_ack (или `POST /sam/push/ack` "
    "`{\"ids\": [...]}`) - иначе workflow считает сообщение непрочитанным и повторяет;\n"
    "- инструменты: MCP стенда первым, терминал вторым; дела SAM - инструментами MCP-сервера sam: memory_search, "
    "remember, create_agent, answer_agent, push_ack, switch_main_model (SAM-175), не curl.\n"
    "Справка по среде - по нужде, не наугад: docs/features/stand.md (у каждого блока где, зачем, что есть, если "
    "упал), main-model.md; инструменты - tools/list MCP стенда; состояние - `/health` шлюза. "
    "Сначала тикер и разговор ниже."
)
# Бриф читает только шину голоса; запроса содержимого истории у шлюза нет (пункт плана monitor-history-api)
HISTORY_HINT = (
    "Ниже только голос из шины. Чат монитора и ходы шлюза - в MongoDB `sam.sessions` по session_id "
    "(список - `GET /sam/sessions`), сжатое - `GET /sam/archive/search?q=...`; запроса истории сессии у шлюза нет."
)

# Выжимка памяти о доме (SAM-101): темы фиксированы - люди, питомцы, правила; вся память в начале сессии - шум
# 127.0.0.1, не localhost: на Windows urllib сначала пробует ::1 и теряет ~2 с на тему (замер 10 окт: 2,1 с против 0,06)
SERVER = os.getenv("SAM_SERVER", "http://127.0.0.1:8000")
MEMORY_PATH = "/sam/memory/search"
HOME_TOPICS = ("люди дома, семья", "питомцы дома", "правила поведения SAM, просьбы домашних")
HOME_FACTS_PER_TOPIC = 5
# ~15 фактов по 100 знаков с запасом - треть экрана; больше - полотно в начале сессии, контекст нужен разговору
HOME_CHARS = int(os.getenv("SAM_LEAD_BRIEF_MEMORY_CHARS", "1500"))
MEMORY_TIMEOUT_S = 3     # на тему; поиск на тёплом эмбеддере - доли секунды, хук не должен висеть


def home_digest(search: Callable[[str], list[str]], topics=HOME_TOPICS, limit_chars: int = HOME_CHARS) -> list[str]:
    """Факты по темам без повторов, пока помещаются в limit_chars. search(тема) -> строки фактов."""
    seen, out, used = set(), [], 0
    for topic in topics:
        for fact in search(topic):
            if fact in seen:
                continue
            if used + len(fact) > limit_chars:
                return out
            seen.add(fact)
            out.append(fact)
            used += len(fact)
    return out


def gateway_search(topic: str) -> list[str]:
    query = urllib.parse.urlencode({"q": topic, "limit": HOME_FACTS_PER_TOPIC})
    req = urllib.request.Request(f"{SERVER}{MEMORY_PATH}?{query}", headers=auth_headers())
    with urllib.request.urlopen(req, timeout=MEMORY_TIMEOUT_S) as r:
        return list(json.load(r).get("facts") or [])


def memory_section(search: Callable[[str], list[str]] = gateway_search) -> str:
    """Раздел брифа о доме; шлюз недоступен - одна строка о причине, бриф идёт дальше."""
    try:
        facts = home_digest(search)
    except Exception as exc:
        return f"память недоступна ({exc}); позже - GET {MEMORY_PATH}?q=..."
    return "\n".join(f"- {f}" for f in facts) or "в памяти о доме пусто"


# Род речи SAM - настройка голоса в шлюзе (self_config voice_gender, SAM-78); шлюз лежит - род по умолчанию
CONFIG_PATH = "/sam/config"


def gateway_gender() -> str:
    req = urllib.request.Request(f"{SERVER}{CONFIG_PATH}", headers=auth_headers())
    with urllib.request.urlopen(req, timeout=MEMORY_TIMEOUT_S) as r:
        return json.load(r).get("voice_gender") or DEFAULT_GENDER


def prompt_section(gender: Callable[[], str] = gateway_gender) -> str:
    """Промпт SAM в роде её голоса; род не узнать - род по умолчанию и строка о причине первой."""
    try:
        return lead_prompt(gender())
    except Exception as exc:
        return f"род голоса не узнан ({exc}), беру {DEFAULT_GENDER}\n" + lead_prompt(DEFAULT_GENDER)


HEARTBEAT_STEP = "живость: `python -u scripts/lead_heartbeat.py` - Bash run_in_background (у Monitor лимит 30 мин)"
START_STEPS = (
    "тикер: Monitor `python -u -m voice.layer.ticker`, если тики в эту сессию ещё не приходят",
    # Надзор поднимают модули стенда (gateway/stand_watch_keeper.py, SAM-195); сессия только читает его лог
    "надзор: Monitor `tail -n 0 -F logs/stand_watch.log`",
    "проверка: `curl -s localhost:8000/health` - `main_model` равен claude-code; мост start_sam поднял сам",
)


def start_steps(heartbeat_alive: bool) -> list[str]:
    """Что поднять сессии, чтобы шлюз отдавал ей ход. Живость уже отмечается - второй процесс не нужен."""
    return ([] if heartbeat_alive else [HEARTBEAT_STEP]) + list(START_STEPS)


def is_lead(flag: str | None, ticks_live: bool) -> bool:
    return flag == "1" or ticks_live


def after_clear_section(text: str) -> str:
    m = SECTION.search(text)
    return m.group(1).strip() if m else ""


def recent_dialogue(rows: list[tuple[str, str, str]], since: str) -> list[str]:
    """rows: (ts, кто, текст) -> строки по времени не раньше since, без повторов, последние MAX_LINES."""
    seen, out = set(), []
    for ts, who, text in sorted(rows):
        if ts >= since and text not in seen:   # слух пишет фразу дважды: частичную и склеенную
            seen.add(text)
            out.append(f"{ts[11:19]} {who}: {text[:TEXT_LIMIT]}")
    return out[-MAX_LINES:]


def read_bus(folder: Path) -> list[tuple[str, str, str]]:
    rows = []
    for name in BUS_FILES:
        path = folder / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            text = (r.get("text") or "").strip()
            if text:
                who = (speaker_label(r["from"]) if "from" in r else r.get("who")) or ("SAM · своя" if name.startswith("agenda") else "?")
                rows.append((r.get("ts", ""), who, text))
    return rows


def is_live(path: Path, minutes: float) -> bool:
    """Файл шины обновлялся за последние minutes минут: тикер - SAM запущена, lead.alive - живость отмечается."""
    return path.exists() and datetime.now().timestamp() - path.stat().st_mtime < minutes * 60


def main() -> None:
    if not is_lead(os.getenv(LEAD_FLAG), is_live(BUS_DIR / LIVE_FILE, LIVE_MINUTES)):
        return
    since = (datetime.now().astimezone() - timedelta(minutes=MINUTES)).isoformat(timespec="seconds")
    section = after_clear_section(CURRENT_MD.read_text(encoding="utf-8")) if CURRENT_MD.exists() else ""
    dialogue = recent_dialogue(read_bus(BUS_DIR), since)
    print(ROLE)
    print(HISTORY_HINT)
    print("\n## Промпт SAM (gateway/prompts/lead.py)\n" + prompt_section())
    steps = start_steps(is_live(BUS_DIR / ALIVE_FILE, ALIVE_S / 60))
    print("\n## Старт - первым делом, по порядку\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
    print("\n## После /clear (current.md)\n" + (section or "раздела нет - читать current.md целиком"))
    print(f"\n## Память о доме ({MEMORY_PATH})\n" + memory_section())
    print(f"\n## Разговор за {MINUTES} мин\n" + ("\n".join(dialogue) or "тишина"))


if __name__ == "__main__":
    main()
