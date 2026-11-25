# Форматы вызова инструментов

Разные модели пишут вызов инструмента по-разному. Шлюз определяет формат по имени модели и разбирает ответ
соответственно; хранит вызовы нейтрально, а под модель форматирует при чтении истории.

`gateway/llm_router.py` (`prompt_format`), `gateway/tool_text_parse.py` (разбор текста),
`gateway/provider_transform.py` (схемы и сообщения под поставщика).

## Форматы

| Формат | Модели | Как выглядит вызов |
|---|---|---|
| `harmony` | `gpt-oss-*`, `harmony-*` | `<\|channel\|>commentary to=functions.ИМЯ <\|constrain\|>json<\|message\|>{"arg": "val"}<\|call\|>` |
| `qwen` | `qwen*` | `<tool_call>{"name": "ИМЯ", "arguments": {...}}</tool_call>` |
| `standard` | остальные | нативный OpenAI `tool_calls` |

`glm` и `gemma` определяются по имени, но отдельной обработки не имеют: в цикле агента идут как `standard`.

Финальный голосовой ответ модели `harmony` идёт каналом `final`:
`<|channel|>final <|constrain|>voice<|message|>текст`, стоп-токены `<|call|>` и `<|return|>`. Системный промпт
дополняется инструкцией по каналам.

Разбор текста идёт на любом адаптере: Qwen через litellm писал вызов текстом там, где ждали нативный. Поддержаны
Harmony, `<tool_call>`, `[tool_call: ...]`, голые Python-вызовы, GLM-4, Gemma 4 - у каждого свой тест
(`tests/test_tool_text_parse.py`).

`provider_transform` чистит схемы инструментов под поставщика (Gemini отвечал 400 на часть JSON Schema) и сообщения
(особенности Claude, Mistral, Gemini).

## Как хранится вызов

Нейтрально, без привязки к модели:

```json
{"role": "tool_call", "tool_name": "spawn_agent", "tool_args": {"description": "..."}}
```

При сборке истории для модели вызов форматируется под её `prompt_format`.

## Проверка

`tests/test_llm_router.py`, `tests/test_tool_text_parse.py`, `tests/test_provider_transform.py`.
