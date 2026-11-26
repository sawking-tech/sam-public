# Состояние и сбои

`GET /health` отдаёт одной строкой, кто ведёт ход и что с блоками. Каждая проверка идёт не дольше
`SAM_HEALTH_PROBE_TIMEOUT_S` (1 с).

| Поле | Что значит |
|---|---|
| `main_model` | кто ведёт ход сейчас |
| `components` | `mongo`, `qdrant`, `mcp`, `cortex`: `ok`, `down: причина` или `timeout` |
| `compression` | сжатие истории по сессиям |
| `workflows` | загруженные workflow и точки решения |
| `stand_watch` | когда надзор последний раз отбился, возраст в секундах |

Что делать при сбое:

| Что сломано | Как смотреть | Чем поднять |
|---|---|---|
| блок стенда | строка `components`, лог `logs/<компонент>.log` | вкладка-надзиратель через 3 с |
| шлюз целиком | `/health` не отвечает | внешний надзор, `scripts/stand_watch.py` |
| надзор молчит | строка `stand_watch`, `logs/stand_watch.log` | workflow `stand-silence` тревожит; поднять `python -u scripts/stand_watch.py` |
| модель не отвечает | `main_model`, лог шлюза | `POST /sam/main-model`, `switch_main_model` |

Кто что поднимает и как устроен надзор — [stand](../architecture/stand/stand.md).
