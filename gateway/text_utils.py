"""Текстовые утилиты SAM (SAM-40 шаг 5).

Pure-функции для пост-обработки ответов LLM:
- `strip_thinking` — удалить thinking-блоки (Qwen3, gpt-oss, и т.д.)
- `transparent_speech` — что звучит: весь ответ без рассуждения и мета-блоков
- `voice_clean` — финальная очистка для TTS (URL, пути, технические маркеры)
- `fmt_msg` — форматирование MongoDB-сообщения для LLM-prompt'а

Без зависимостей от MongoDB / LLM / event_bus — переносимо в AVOX-core.
"""

from __future__ import annotations

import re
from datetime import datetime

from .speech_meta import strip_meta


# ─── Thinking stripping ────────────────────────────────────────────────────────

_REASONING_STARTERS = (
    "окей,", "хорошо,", "нужно ", "итак,", "значит,", "вижу,", "я вижу",
    "получил ", "получил,", "смотрю", "анализирую", "проверяю", "давай ",
    "сначала ", "первым делом", "разберём", "разберемся",
    # Паттерны где модель описывает свои действия вместо того чтобы их делать
    "ответ должен", "пользователь попросил", "пользователь хочет",
    "теперь нужно", "нужно просто", "мне нужно", "мне надо",
    "факт успешно", "факт сохранён", "факт запомнен",
    "задача выполнена", "задача создана", "задача запущена",
    "я должен ответить", "я должен сказать", "буду говорить",
)


def strip_thinking(text: str) -> str:
    """Убрать thinking-блоки из ответов Qwen3 и подобных моделей."""
    if not text:
        return text or ""
    # Стандартные теги
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|thinking\|>.*?<\|/thinking\|>", "", text, flags=re.DOTALL)
    # Qwen3 "thinking" без тегов: если ответ начинается с reasoning-маркеров —
    # ищем первое "чистое" предложение после них
    lines = text.strip().splitlines()
    clean = []
    skip_mode = True
    for line in lines:
        low = line.strip().lower()
        if skip_mode:
            if any(low.startswith(s) for s in _REASONING_STARTERS):
                continue
            else:
                skip_mode = False
        clean.append(line)
    result = "\n".join(clean).strip()
    return result if result else text.strip()


# ─── Voice cleanup ─────────────────────────────────────────────────────────────

def transparent_speech(text: str) -> str:
    """Речь в прозрачном режиме (ADR-003): весь ответ без рассуждения и мета-блоков в парных тегах (<silent>,
    <tool_call>, любые другие - gateway/speech_meta.py), очищенный как voice_clean."""
    if not text:
        return ""
    return voice_clean(strip_meta(strip_thinking(text)))


def own_speech(text: str) -> str:
    """Своя реплика SAM или поправка: без мета-блоков и очищенная как voice_clean. Эвристику рассуждения
    strip_thinking не берёт: в готовой реплике строка «Сначала проверила лампы.» - речь, а не план."""
    return voice_clean(strip_meta(text)) if text else ""


def voice_clean(text: str) -> str:
    """Очистить финальный ответ для голосового вывода: URL, технические детали."""
    if not text:
        return ""
    # Стрипаем <|channel|> special tokens
    if "<|channel|>" in text:
        msg_m = re.search(r'<\|message\|>(.*?)(?:<\||$)', text, re.DOTALL)
        if msg_m:
            text = msg_m.group(1).strip()
        else:
            text = re.sub(r'<\|[^|]*\|>', ' ', text).strip()
    # URL и пути — не озвучиваемы
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[A-Za-z]:\\[^\s,]+', '', text)
    text = re.sub(r'/(?:var|usr|etc|home|tmp)/[^\s,]+', '', text)
    # Технические маркеры
    text = re.sub(r'\[(?:GUARD|ERROR|BLOCK)\][^\n]*', '', text)
    # Нормализуем пробелы
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ─── History formatting ────────────────────────────────────────────────────────

def fmt_msg(m: dict, max_len: int = 400, now: datetime | None = None) -> str:
    """Форматировать одно сообщение из истории для LLM-контекста с time-меткой; now - часы (UTC), по умолчанию текущие."""
    ts = m.get("timestamp")
    time_str = ""
    if ts:
        now = now or datetime.utcnow()
        ts_naive = ts.replace(tzinfo=None) if hasattr(ts, "tzinfo") and ts.tzinfo else ts
        delta = now - ts_naive
        total_sec = int(delta.total_seconds())
        if total_sec < 60:
            time_str = "[только что] "
        elif total_sec < 3600:
            time_str = f"[{total_sec // 60}мин назад] "
        elif total_sec < 86400:
            time_str = f"[{total_sec // 3600}ч назад] "
        else:
            time_str = f"[{total_sec // 86400}д назад] "

    if m.get("is_summary"):
        authors = m.get("authors", [])
        label = ",".join(dict.fromkeys(authors)) if authors else "диалог"
        return f"{time_str}[{label}]: {m['content'][:max_len]}"
    if m.get("tool_calls"):
        names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
        return f"{time_str}assistant→tools: {names}"
    if m.get("role") == "tool":
        return f"{time_str}tool[{m.get('tool_call_id', '')[:8]}]: {(m.get('content') or '')[:max_len]}"
    return f"{time_str}{m['role']}: {(m.get('content') or '')[:max_len]}"
