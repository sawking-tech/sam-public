# operations

Расход денег и запуск. Устройство - в [architecture](../architecture/index.md), настройки фич - в
[features](../features/index.md).

**[usage-cost.md](usage-cost.md)** - цена вызова считается из `usage` ответа и таблицы `prices.json`. Токены каждого
вызова пишутся в `_token_usage` с ролью (`main`, `worker`, `guard`, `summarize`) и живут 30 дней. Вызов без цены
или без usage в сумму не входит, но числится в `calls`: неполный итог не выдаётся за полный. `GET /sam/usage/cost`
отдаёт сумму по валютам, разбивку по `role`, `model`, `source`, `day` и остаток у поставщика.

**[dev-budget.md](dev-budget.md)** - тяжёлая работа ограничена рублями по скользящим окнам: час 15 ₽, рабочее окно
5 ч - 40 ₽, сутки 150 ₽, месяц 3000 ₽. Задача сверх лимита не теряется, а ждёт в `_deferred_tasks` и запускается
старейшей, когда лимит пропускает. Ступень `normal` / `saving` / `minimal` / `floor` понижается сразу, повышается
после 30 минут выдержки.

**[install](../getting-started/install.md)** - запуск с нуля. Компоненты по отдельности и полный старт -
[README](../../README.md). Состояние стенда и надзор - [stand](../architecture/stand.md).
