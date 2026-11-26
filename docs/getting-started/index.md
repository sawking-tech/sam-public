# getting-started

Установка и первые настройки: зависимости, ключ поставщика, чей голос SAM слышит.

[install.md](install.md): клонирование, `.env`, виртуальное окружение, `./start_sam.sh`, адрес монитора. Нужны
Python 3.12, Docker и ключ одного поставщика LLM либо локальная LM Studio.

[models.md](models.md): `LLM_SOURCE` выбирает поставщика, `LLM_ADAPTER` - чем к нему ходить. Пустой `LLM_SOURCE`
означает авто: lmstudio, затем gemini, затем dashscope. Смена модели без рестарта - `POST /sam/main-model`.

[household.md](household.md): люди и питомцы дома с ролью и короткой характеристикой. Сколько их и кем они друг
другу приходятся, задаёт дом: слотов в коде и промптах нет.

Возможности - [features](../features/index.md). Устройство - [architecture](../architecture/index.md).
