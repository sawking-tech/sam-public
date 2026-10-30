"""Бюджет агента (ADR-022, «Бюджет дерева»): шаги на задачу, общие для всего дерева агентов.

Создатель назначает бюджет при создании; ребёнку он выделяется из остатка родителя, и родитель узнаёт, что его остаток
уменьшился. Шаг - один оборот модели в цикле агента: его человек прикидывает этапами («найти, прочесть, поправить, прогнать»). На доле WARN_SHARE агент получает
подсказку: закончить или попросить ещё (request_steps с причиной); создатель выдаёт из своего остатка (grant_steps)
или отказывает. Кончился - ход заканчивается итогом того, что успел. Закрытая задача возвращает неистраченное
родителю. Основная модель - корень: её выдачу ограничивает CAP_STEPS. Модуль чистый: учёт в памяти, без сети.
"""
from __future__ import annotations

from dataclasses import dataclass

from .env import env_float, env_int

# Правке кода нужно 20-30 шагов (найти, прочесть, поправить, прогнать тесты, отчитаться); 12 прежних кончались на поиске
DEFAULT_STEPS = env_int("SAM_AGENT_STEPS", 30)
CAP_STEPS = env_int("SAM_AGENT_STEPS_CAP", 100)   # больше одной выдачи основная модель не даёт
WARN_SHARE = env_float("SAM_AGENT_BUDGET_WARN", 0.8)   # доля истраченного, на которой агент получает подсказку
# Обращения по каналам (вопрос создателю, просьба о шагах) шагов не тратят - иначе вопросы ребёнка жгли бы пулы; у
# бесплатного своя квота на задачу, чтобы им не абузили: десятка вопросов на задачу хватает, дальше - решать самому
CHANNEL_CALLS = env_int("SAM_AGENT_CHANNEL_CALLS", 10)
# Каналы: оборот, в котором агент зовёт только их, шага не стоит
FREE_TOOLS = frozenset({"ask_creator", "request_steps", "estimate_steps", "report_status", "send_to_agent", "grant_steps"})
NO_MORE_QUESTIONS = "Обращения к создателю на эту задачу кончились ({calls}): решай сам по тому, что знаешь"
ROOT = ""   # создатель-корень: основная модель или оркестратор

WARN = ("Шаги почти кончились: осталось {left} из {total}. Закончи с тем, что есть, или попроси ещё - "
        "request_steps(steps, why), с причиной.")
REDUCED = "Ребёнку {child} выделено {steps} шагов из твоих; твой остаток {left}. Не хватит - request_steps."
EXHAUSTED = "[SAM] Шаги агента кончились."
TABLE_EXAMPLE = ('[{"block": "прочитать входы", "steps": 2}, {"block": "модуль", "steps": 3}, '
                 '{"block": "тесты и мутация", "steps": 3}, {"block": "коммит", "steps": 1}]')
NEED_TABLE = ("Оценка принимается таблицей крупных блоков работы с шагами на каждый, не одним числом. Пример: "
              "estimate_steps(plan=" + TABLE_EXAMPLE + ")")
BAD_BLOCK = "Блок {n} таблицы без названия или с числом шагов не больше нуля: {block}"
TOO_MANY_BLOCKS = "Блоков больше {limit}: оценка крупными кусками, а не по шагам"
ESTIMATE_RESERVE = env_float("SAM_ESTIMATE_RESERVE", 0.25)   # запас оценивающего поверх суммы блоков: модели в оценках ошибаются
MAX_BLOCKS = env_int("SAM_ESTIMATE_MAX_BLOCKS", 12)
POOL = "Твой пул: {pool} шагов. Распоряжайся сам; не хватит - request_steps(steps, why)."
ESTIMATE_RULE = ("Разобрав задачу в первом ответе, в нём же вызови estimate_steps(plan) - таблицу крупных блоков работы с "
                 "шагами (обращениями к модели) на каждый, считая этот ответ и итог, - вместе с первым действием, отдельного "
                 "шага на оценку не трать: уже сделанные шаги в пул входят сами, сверх них пул будет не больше оценки. Не хватит - "
                 "request_steps(steps, why) с причиной; создатель решит.")
REQUEST = ("Агент {id} просит ещё {steps} шагов: {why}. Реши сама, молчать нельзя: выдать из своего остатка - "
           "grant_steps(task_id={id}, steps={steps}) (вне шлюза - grant_agent_steps MCP sam); отказать ответом агенту с "
           "причиной; или забрать работу - откажи, его итог придёт тебе, доделай сама (не хватит тебе - request_steps у "
           "своего создателя)")
GRANTED = "Тебе выдано ещё {steps} шагов: {reason}"
# Каскад: у выдающего-агента остатка мало - он просит у своего создателя и выдаёт потом, или отказывает с причиной
NOT_ENOUGH = ("Не хватает шагов: у тебя {left}, просишь {steps}. Попроси у своего создателя request_steps(steps={need}, "
              "why=...) и выдай после, или откажи агенту с причиной")
ABOVE_CEILING = ("Запрос выходит за оценку задачи: потолок {ceiling}, выдано {total}, просишь ещё {steps}. Работа встаёт, "
                 "создатель разбирается по закрытым блокам таблицы")
CLOSED = "задачи {task} нет в учёте бюджета: агент уже закончил, ответ ему воскресит его с историей"


def table_total(plan, reserve: float = ESTIMATE_RESERVE) -> tuple[int, str]:
    """Оценка агента таблицей: (сумма шагов с запасом, ошибка). Число без таблицы и пустая таблица не принимаются."""
    if not isinstance(plan, list) or not plan:
        return 0, NEED_TABLE
    if len(plan) > MAX_BLOCKS:
        return 0, TOO_MANY_BLOCKS.format(limit=MAX_BLOCKS)
    total = 0
    for n, row in enumerate(plan, 1):
        block = str(row.get("block") or "").strip() if isinstance(row, dict) else ""
        try:
            steps = int(row.get("steps")) if isinstance(row, dict) else 0
        except (TypeError, ValueError):
            steps = 0
        if not block or steps <= 0:
            return 0, BAD_BLOCK.format(n=n, block=row)
        total += steps
    return -(-int(total * (1 + reserve) * 100) // 100), ""   # вверх до целого: запас не округляется вниз


@dataclass
class _Account:
    total: int
    spent: int = 0
    parent: str = ROOT
    warned: bool = False
    channel_calls: int = 0
    ceiling: int = 0   # потолок: большая из оценок создателя и агента; в его пределах просьбы одобряются сами (SAM-257)

    @property
    def left(self) -> int:
        return max(self.total - self.spent, 0)


def _done_before_turn(account: _Account) -> int:
    """Шаги до текущего оборота: step списывает шаг в начале оборота, оценка приходит внутри него."""
    return max(account.spent - 1, 0)


class Budgets:
    def __init__(self) -> None:
        self._accounts: dict[str, _Account] = {}

    def open(self, task: str, steps: int | None, parent: str = ROOT) -> tuple[int, str]:
        """Счёт задачи: (выделено, сообщение родителю). У родителя-агента - из его остатка, не больше остатка."""
        want = min(int(steps or DEFAULT_STEPS), CAP_STEPS)
        owner = self._accounts.get(parent) if parent else None
        if owner is not None:
            want = min(want, owner.left)
            owner.total -= want
            note = REDUCED.format(child=task, steps=want, left=owner.left)
        else:
            note = ""
        self._accounts[task] = _Account(total=want, parent=parent, ceiling=want)
        return want, note

    def spend(self, task: str, steps: int = 1) -> str | None:
        """Шаг агента; ответ - подсказка агенту, если он только что перешёл долю WARN_SHARE."""
        account = self._accounts.get(task)
        if account is None:
            return None
        account.spent += max(int(steps), 0)
        if not account.warned and account.spent >= account.total * WARN_SHARE:
            account.warned = True
            return WARN.format(left=account.left, total=account.total)
        return None

    def estimate(self, task: str, steps: int) -> str:
        """Оценка агента - сколько шагов ещё нужно, считая текущий: пул задачи - сделанные до него шаги плюс оценка, не
        больше выделенного, излишек - родителю. Ответ - пул агенту."""
        account = self._accounts.get(task)
        if account is None:
            return "Оценка не записана: задачи нет в учёте шагов"
        wanted = _done_before_turn(account) + max(int(steps), 1)
        account.ceiling = max(account.ceiling, wanted)   # агент оценил выше создателя - прав мог быть он
        pool = min(wanted, account.total)
        surplus = account.total - pool
        account.total = pool
        parent = self._accounts.get(account.parent) if account.parent else None
        if parent is not None:
            parent.total += surplus
        return POOL.format(pool=pool)

    def autogrant(self, task: str, steps: int) -> tuple[bool, str]:
        """Просьба агента в пределах потолка (оценка создателя или агента, что больше): одобряется без разбора, шаги берутся
        у родителя-агента, у корня - из общего резерва. Выше потолка - нет: работа встаёт, создатель разбирается (SAM-257)."""
        account = self._accounts.get(task)
        if account is None:
            return False, CLOSED.format(task=task)
        steps = max(int(steps), 0)
        if account.total + steps > account.ceiling:
            return False, ABOVE_CEILING.format(ceiling=account.ceiling, total=account.total, steps=steps)
        parent = self._accounts.get(account.parent) if account.parent else None
        if parent is not None:
            if steps > parent.left:
                return False, NOT_ENOUGH.format(left=parent.left, steps=steps, need=steps - parent.left)
            parent.total -= steps
        account.total += steps
        account.warned = False
        return True, f"выдано {steps} шагов в пределах оценки, остаток задачи {account.left}"

    def step(self, task: str) -> tuple[bool, str | None]:
        """Перед шагом агента: (можно ли, подсказка). Шагов нет - нельзя; задача не в учёте - без ограничения."""
        if self.exhausted(task):
            return False, None
        return True, self.spend(task)

    def channel(self, task: str) -> str | None:
        """Обращение по каналу: в пределах квоты - None, сверх - отказ агенту. Задача не в учёте - без ограничения."""
        account = self._accounts.get(task)
        if account is None:
            return None
        if account.channel_calls >= CHANNEL_CALLS:
            return NO_MORE_QUESTIONS.format(calls=CHANNEL_CALLS)
        account.channel_calls += 1
        return None

    def turn_done(self, task: str, tools: list[str]) -> None:
        """Оборот, в котором агент звал только каналы, шага не стоит: шаг возвращается."""
        account = self._accounts.get(task)
        if account is not None and tools and all(t in FREE_TOOLS for t in tools) and account.spent:
            account.spent -= 1

    def exhausted(self, task: str) -> bool:
        account = self._accounts.get(task)
        return account is not None and account.left == 0

    def grant(self, granter: str, task: str, steps: int) -> tuple[bool, str]:
        """Добавка задаче из остатка выдающего; выдающий-корень - не больше CAP_STEPS за раз."""
        account = self._accounts.get(task)
        if account is None:
            return False, CLOSED.format(task=task)
        steps = max(int(steps), 0)
        source = self._accounts.get(granter) if granter else None
        if source is not None:
            if steps > source.left:
                return False, NOT_ENOUGH.format(left=source.left, steps=steps, need=steps - source.left)
            source.total -= steps
        elif steps > CAP_STEPS:
            return False, f"больше {CAP_STEPS} шагов за раз не выдаётся"
        account.total += steps
        account.warned = False
        return True, f"выдано {steps} шагов, остаток задачи {account.left}"

    def close(self, task: str) -> None:
        """Закрытая задача: неистраченное - родителю."""
        account = self._accounts.pop(task, None)
        if account and account.parent and account.parent in self._accounts:
            self._accounts[account.parent].total += account.left

    def pool(self, task: str) -> int | None:
        """Пул шагов задачи сейчас (выдано всего) или None, если задачи нет в учёте."""
        account = self._accounts.get(task)
        return account.total if account else None

    def left(self, task: str) -> int | None:
        account = self._accounts.get(task)
        return account.left if account else None

    def snapshot(self, task: str) -> dict | None:
        """Шаги задачи для показа: пул, потрачено, осталось, потолок; None - задачи нет в учёте."""
        a = self._accounts.get(task)
        return {"pool": a.total, "spent": a.spent, "left": a.left, "ceiling": a.ceiling} if a else None

    def spent(self, task: str) -> int | None:
        """Сколько шагов задача потратила на деле или None, если её нет в учёте."""
        account = self._accounts.get(task)
        return account.spent if account else None
