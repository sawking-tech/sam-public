"""Своя недавняя работа SAM для хода любой сессии: задачи агентов и живые приложения, где бы их ни начали.

SAM одна, а истории сессий раздельные: приложение, сделанное по просьбе из чата монитора, в голосовой сессии не видно
ни в истории, ни в list_agents (он показывает только идущие задачи). Без этой сводки модель в другой сессии отвечала
«такого у меня нет» и искала своё приложение по диску. Сводка короткая: подробности - list_agents и app_list.

Решение (что войдёт и как сказать) отделено от Mongo и файлов: `render` чистая, источники приходят параметрами.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Iterable

from .env import env_float, env_int

# Сколько часов назад начатая работа ещё «недавняя»: вчерашний вечер утром помнится, позавчерашнее - уже по запросу
HOURS = env_float("SAM_OWN_WORK_HOURS", 24.0)
MAX_TASKS = env_int("SAM_OWN_WORK_TASKS", 8)       # больше - сводка вытесняет разговор
MAX_APPS = env_int("SAM_OWN_WORK_APPS", 5)
RESULT_CHARS = 140                                  # итог задачи одной строкой: суть, без отчёта целиком
HEADER = "Твоя недавняя работа (все сессии; подробности - list_agents, app_list):"


def _line(text: str, limit: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def render(tasks: Iterable[dict], apps: Iterable[dict], now: datetime, hours: float = HOURS,
           max_tasks: int = MAX_TASKS, max_apps: int = MAX_APPS) -> str:
    """Блок для системной части хода; работы нет - пустая строка.

    tasks - записи sam.tasks (id, name, description, status, created_at, result, error), свежие первыми;
    apps - живые приложения (slug, name, wanted, created), свежие первыми."""
    since = now - timedelta(hours=hours)
    rows = []
    for t in tasks:
        created = t.get("created_at")
        if isinstance(created, datetime) and created < since:
            continue
        what = t.get("name") or _line(t.get("description"), 60)
        outcome = _line(t.get("result") or t.get("error"), RESULT_CHARS)
        rows.append(f"- задача {str(t.get('id', ''))[:8]} {what} · {t.get('status', '?')}"
                    + (f" · {outcome}" if outcome else ""))
        if len(rows) == max_tasks:
            break
    for a in list(apps)[:max_apps]:
        rows.append(f"- приложение {a.get('slug')} «{a.get('name')}» · {a.get('wanted')} · /apps/{a.get('slug')}/"
                    + (f" · сделано {a['created']}" if a.get("created") else ""))
    return "\n".join([HEADER, *rows]) if rows else ""


def collector(tasks: Callable[[], list[dict]], apps: Callable[[], list[dict]],
              clock: Callable[[], datetime] = datetime.utcnow, log: Callable[[str], None] = print) -> Callable[[], str]:
    """Сводка для хода: источники недоступны - без сводки и одна строка в лог, ход идёт дальше."""
    def collect() -> str:
        try:
            return render(tasks(), apps(), clock())
        except Exception as e:     # Mongo или папка приложений: ход важнее сводки
            log(f"[OwnWork] сводка своей работы не собрана: {type(e).__name__}: {e}")
            return ""
    return collect
