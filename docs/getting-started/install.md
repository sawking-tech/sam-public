# install

Нужны Python 3.12, Docker и ключ одного поставщика LLM (или локальная LM Studio).

```bash
git clone https://github.com/sawking-tech/sam-public.git && cd sam-public
cp .env.example .env                       # LLM_SOURCE и ключ поставщика
python -m venv .venv && . .venv/bin/activate
pip install -r gateway/requirements.txt
./start_sam.sh                             # Windows: .\start_sam.ps1 -NoLead
```

Монитор - <http://127.0.0.1:8000/monitor>. Проверка: `/health` отдаёт `status`, `main_model`, `components`.

Голос ставится отдельно: `pip install -r voice/requirements.txt`, запуск - `python -m voice.layer.voice` и
`python -m voice.layer.bridge`. Блоки стенда - [stand](../architecture/stand/stand.md).

Данные лежат вне git: `.env`, папка `data/`, тома Docker.
