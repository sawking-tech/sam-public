# Документация SAM

**SAM (Smart Ambient Mate)** - автономная агентная платформа и среда выполнения для дома и небольшого производства.
Голос, память о людях, агенты, песочница, живые приложения. Что умеет и с чего начать - [README](../README.md).

Тут справка по устройству: правила, пороги, переменные, файлы. Решения и их причины - отдельно, в ADR.

## Начать

- [Архитектура](architecture.md): схемы потоков, очереди, жизненный цикл задачи и воркера
- [Зоны мозга](brain-map.md): почему части разделены так; имена модулей
- [Цели автора](v1-goals.md): что в v1, чего в нём нет

## Устройство разделов

[Все разделы](features/README.md) - по одному файлу на подсистему: что делает, где код, пороги и переменные, что не
закрыто. Крупное:

| Раздел | Файл |
|---|---|
| Блоки стенда: где, зачем, что есть, если упал | [stand](features/stand.md) |
| Основная модель и ведущий | [main-model](features/main-model.md) |
| Голосовой слой: ядро звука, слух, опознание, перебивание, синтез | [audio-core](features/audio-core.md), [hearing](features/hearing.md), [voice-id](features/voice-id.md), [barge-in](features/barge-in.md), [speech-out](features/speech-out.md) |
| Быстрая модель и поправка основной | [fast-voice](features/fast-voice.md) |
| Кому сказана фраза | [addressee](features/addressee.md) |
| Память: запись, гибридный поиск, эмбеддер | [memory-search](features/memory-search.md) |
| Сжатие и память в паузах | [compression](features/compression.md), [memory-in-pauses](features/memory-in-pauses.md) |
| Агенты и воркеры | [workers](features/workers.md), [agents](features/agents.md), [agent-loop](features/agent-loop.md) |
| Guard и пределы агентов | [guard](features/guard.md) |
| Песочница и живые приложения | [sandbox](features/sandbox.md), [apps](features/apps.md) |
| Проактивность | [workflows](features/workflows.md) |
| Характер, промпт, душа | [character](features/character.md), [prompt](features/prompt.md) |
| Доступ, ключи, секрет команд | [access](features/access.md), [command-secret](features/command-secret.md) |
| Хранилище: коллекции MongoDB и Qdrant | [storage](features/storage.md) |
| Выбор модели и отказы поставщиков | [llm-router](features/llm-router.md) |
| Форматы вызова инструментов | [tool-formats](features/tool-formats.md) |
| Расходы и трасса хода | [usage-cost](features/usage-cost.md), [trace](features/trace.md) |
| Состав дома | [household](features/household.md) |
| Каркас страниц | [shell](features/shell.md) |

## API

- [REST API шлюза](api/rest.md): главные ручки, агент от основной модели вне шлюза

## Решения

[ADR](adr/README.md) - где SAM отходит от принятого и почему. Каждый ADR отвечает одной строкой на вопрос «почему
так», механика живёт в разделе.

## Порог

Номер задачи (SAM-###) в тексте - не украшение: он говорит, где искать историю. Но текст читается и без неё:
правило, порог и файл названы рядом с номером.
