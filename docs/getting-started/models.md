# models

Модель SAM задаётся двумя переменными: `LLM_SOURCE` выбирает поставщика, `LLM_ADAPTER` - чем к нему ходить.

```bash
LLM_SOURCE=dashscope   # dashscope | gemini | lmstudio | anthropic | claude_code | zai | acp
LLM_SOURCE=            # пусто - авто: lmstudio -> gemini -> dashscope
LLM_ADAPTER=openai     # openai (по умолчанию) | custom | litellm | auto
```

После смены - рестарт стенда. Поставщик без ключа в цепочку не попадает; недоступный ставится на паузу, и ход
берёт следующий. Локальная LM Studio: `LMSTUDIO_URL`, `LMSTUDIO_MODEL` (пусто - единственная загруженная).

Вид задачи выбирает роутер, а не глобальная настройка: голосовой ход идёт маршрутом `fast`, текстовый - `turn`.
Устройство - [llm-router](../architecture/llm/llm-router.md). Смена основной модели без рестарта -
[main-model](../architecture/gateway/main-model.md).
