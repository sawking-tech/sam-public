# getting-started

Установка и первый запуск. Нужны Python 3.12, Docker и ключ одного поставщика LLM либо локальная LM Studio. Старт —
`git clone`, `cp .env.example .env`, venv, `pip install -r gateway/requirements.txt`, `./start_sam.sh` (Windows:
`.\start_sam.ps1 -NoLead`). Монитор — `http://127.0.0.1:8000/monitor`, проверка — `/health` с полями `status`,
`main_model`, `components`.

Голос ставится отдельно: `pip install -r voice/requirements.txt` и два процесса, `python -m voice.layer.voice` и
`python -m voice.layer.bridge`. Данные лежат вне git: `.env`, папка `data/`, тома Docker.

| Файл | Механизм |
|---|---|
| [install.md](install.md) | зависимости, `.env`, виртуальное окружение, `start_sam.sh`, монитор, отдельная установка голоса |
| [models.md](models.md) | `LLM_SOURCE` выбирает поставщика, `LLM_ADAPTER` — чем ходить; авто-цепочка lmstudio → gemini → dashscope; вид задачи выбирает роутер, а не глобальная настройка |

Сценарии — [scenario](../scenario/README.md). Блоки стенда и что делать при падении —
[stand](../architecture/stand/stand.md) и [operations](../operations/README.md).
