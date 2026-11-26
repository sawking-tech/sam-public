# Деньги

Сколько SAM тратит, на чём это видно и когда тяжёлая работа останавливается. Считают два места: `_token_usage`
пишет каждый вызов LLM, `dev_budget` ограничивает расход по скользящим окнам.

## Учёт вызовов

Токены каждого вызова пишет `gateway/token_usage.py` в `_token_usage`; записи живут `SAM_TOKEN_USAGE_TTL_DAYS` (30)
дней. Роль ставит вызывающий код через `usage_scope`: ход главного агента - `main`, воркер - `worker`, guard -
`guard`, сжатие - `summarize`, вызов вне разметки - `unknown`. Токены сессии по ролям отдаёт
`GET /sam/metrics/tokens/sessions/{id}`.

Стрим отдаёт usage, только если поставщик прислал его в последнем чанке; запрос включает
`LLM_STREAM_INCLUDE_USAGE=1`, по умолчанию выключен. Без него стримовый вызов числится в `calls_without_usage`.

Цена за 1M токенов лежит в `gateway/prices.json`, свою таблицу задаёт `SAM_PRICES_FILE`. Ключ строки -
`поставщик/модель`, запись `lmstudio/*` покрывает все модели поставщика, валюта у строки своя. Поставщик может
прислать стоимость вызова в `usage.cost`: запись хранит её как есть, валюту объявляет секция `cost_currency`.

`GET /sam/usage/cost?days=7&group_by=role` отдаёт `cost` - сумму и разбивку по `role`, `model`, `source` или `day`
отдельно по валютам; `calls` - сколько вызовов всего, сколько учтено в сумме, сколько без usage и без цены;
`unpriced_models` - модели без строки в таблице; `balances` - остаток у поставщика. Вызов без цены или без usage в
сумму не входит, но числится в `calls`: неполный итог не выдаётся за полный.

Остаток отдают OpenRouter (`GET /credits`, `GET /key`) и ключи с объявленным лимитом; у z.ai по подписке и у
polza.ai остатка в ответе нет.

## Лимиты

| Окно | Переменная | По умолчанию |
|---|---|---|
| час | `SAM_DEV_HOUR_RUB` | 15 ₽ |
| рабочее окно | `SAM_DEV_WINDOW_H`, `SAM_DEV_WINDOW_RUB` | 5 ч, 40 ₽ |
| сутки | `SAM_DEV_DAY_RUB` | 150 ₽ |
| неделя | `SAM_DEV_WEEK_RUB` | 0 ₽, выключено |
| месяц тяжёлой работы | `SAM_DEV_MONTH_RUB` | 3000 ₽ за 30 дней |
| общий потолок месяца | `SAM_MONTHLY_BUDGET_RUB` | 5000 ₽ за 30 дней |

`spent_heavy` считает расход роли `worker` по источникам `SAM_BUDGET_SOURCES` (по умолчанию `polza`) без сессий
лёгких задач; общий месячный расход включает все роли.

Тяжёлой задача считается, если классификатор дал уровень 3 или выше либо заказанный пул шагов больше
`SAM_LIGHT_TASK_STEPS` (10). Лёгкие задачи идут без ограничения окнами. Задача сверх лимита ждёт в
`_deferred_tasks`; шедулер проверяет очередь раз в `SAM_DEFERRED_EVERY_S` (60 с) и запускает старейшую, когда
лимит её пропускает. Младшие старейшую не обгоняют.

Ступень по остатку: `normal` - не ниже `SAM_DEGRADE_SAVING` (35%), `saving` - ниже, `minimal` - ниже
`SAM_DEGRADE_MINIMAL` (15%) или исчерпан, `floor` - остаток счёта не выше `SAM_HOUSEHOLD_FLOOR_RUB` (500 ₽). В
`minimal` и `floor` тяжёлая работа остановлена. Ступень понижается сразу, повышается на одну после выдержки
`SAM_RECOVER_HOLD_MIN` (30 мин).

Состояние отдаёт `GET /sam/usage/dev-budget`.

## Где код

`gateway/pricing.py` - цена и стоимость вызова, `gateway/usage_cost.py` - сводка, `gateway/balance.py` - остатки,
`gateway/dev_budget.py` - окна и ступени, `gateway/deferred_tasks.py` - очередь. Тесты: `tests/test_usage_cost.py`,
`tests/test_dev_budget.py`, `tests/test_deferred_tasks.py`, `tests/test_budget_balances.py`.
