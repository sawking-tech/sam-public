"""amend_reply - поправка своей последней реплики SAM шагом workflow (SAM-166): что не прозвучало - отзывается, остальное
поправляется следом.

Срочная (count 1: проверен один ответ, после его фразы человек ничего не сказал) - сообщение fix голосовому слою: мост
отзывает ещё не прозвучавшие реплики после последней фразы человека (voice/layer/bridge.fix_lines, turns.py) и говорит
поправку; успела прозвучать - поправка звучит после неё («ой, поправлюсь: ...»). Поздняя - count больше 1 (проверялась
пачка, {{count}} workflow с on.event.collect) или после проверенной фразы (phrase) человек уже сказал новую: отзыв задел
бы ответ на неё. Поздняя звучит своей репликой SAM в паузе и протухает, если пауза не наступила.
Поправленный ответ (reply) убирается из контекста диалога: скрывается в истории (IGNORE), следующие ходы на нём не
строятся. text = DROP_MARK - бред убирается из контекста без поправки вслух (ошибка ничего не меняла для человека).
Сейчас поправку слышат в голосовой сессии; другие каналы (чат монитора) - через ту же шину, когда придут.
Инструмент только для workflow: в схему модели не входит. Голосовой слой и запись в историю приходят параметрами.
"""
from __future__ import annotations

from typing import Callable

from ..env import env_float
from ..text_utils import own_speech

# Поздняя поправка без паузы протухает: через 20 с она относится уже не к тому (прежде SAM_CHATTER_FIX_TTL_S)
LATE_FIX_TTL_S = env_float("SAM_AMEND_TTL_S", 20.0)
# Ответ шага проверки «убрать без поправки»: служебное слово, его модель пишет вместо текста поправки (fast-review.yml)
DROP_MARK = "DROP"


def amend_reply_impl(params: dict, session: str, broadcast: Callable[[dict], bool],
                   record_fix: Callable[[str, str], None],
                   say_own: Callable[..., bool],
                   last_phrase: Callable[[str], str | None] = lambda session: None,
                   forget: Callable[[str, str], bool] = lambda session, reply: False) -> str:
    """params: text - поправка; count - сколько ответов проверялось (больше 1 - поздняя, по умолчанию 1);
    phrase - фраза человека, на которую был проверенный ответ (не последняя - поправка поздняя);
    reply - сам неверный ответ: убирается из контекста диалога."""
    text = own_speech(str(params.get("text") or "")).strip()   # мета-блоки поправки не звучат (ADR-003)
    if not text:
        return "Нужен text - поправка одной-двумя фразами"
    reply = str(params.get("reply") or "").strip()
    cleaned = bool(reply) and forget(session, reply)
    if text.strip(" .!").upper() == DROP_MARK:
        if not reply:   # пачка: какой ответ убрать, не названо - и не поправка (откат не включается)
            return "Убирать нечего: в пачке ответ не назван"
        return "OK: ответ убран из контекста без поправки" if cleaned else "Ответ для чистки не найден в истории"
    try:
        late = int(str(params.get("count") or 1).strip()) > 1
    except ValueError:
        return f"count - число проверенных ответов, получено {params.get('count')!r}"
    phrase, last = str(params.get("phrase") or "").strip(), last_phrase(session)
    late = late or (bool(phrase) and last is not None and last.strip() != phrase)
    if late:
        if not say_own(session, text, ttl_s=LATE_FIX_TTL_S):
            return "Голосовой слой не подключён: поздняя поправка не сказана"
        return "OK: поздняя поправка встала в очередь паузы"
    if not broadcast({"type": "fix", "text": text}):
        return "Голосовой слой не подключён: поправка не сказана"
    record_fix(session, text)
    return "OK: реплика быстрой отозвана, поправка звучит"
