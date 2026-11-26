# Прогон и трасса хода

Как проверить разговор без живых людей и как увидеть, из чего сложился ход.

`gateway/evals.py`, `scripts/eval_voice.py`, `gateway/token_usage.py` (`trace_turn`).

## Прогон на наборе случаев

Случай - фраза и ожидание, которое проверяет код: вызван ли `spawn_agent`, ответ или молчание, нет ли обещания
действия без вызова. Набор из истории собирается командой, прогон идёт без записи в историю:

```bash
python scripts/eval_voice.py export     # data/evals/cases.jsonl, вне git
python scripts/eval_voice.py run        # итог JSON, ходы в историю не пишутся
```

Без своего набора берётся образец `tests/fixtures/voice_eval_cases.py`.

## Трасса хода

Внутри `token_usage.trace_turn()` этапы хода пишутся в поле `stages` записи `_token_usage`, миллисекунды от начала
хода:

| Этап | Когда |
|---|---|
| `context` | контекст собран |
| `first_token` | первая дельта ответа |

Этапы голосового слоя - первое предложение на синтез, первый звук - шлюз не видит: они в логе слоя.

## Проверка

`tests/test_evals.py`, `tests/test_token_usage.py`, `tests/test_usage_cost.py`.
