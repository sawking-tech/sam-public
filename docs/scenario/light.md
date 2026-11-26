# Лёгкий сценарий

Наблюдение, порог и одно действие. Модель нужна только для самого действия, отсев делает код.

```yaml
name: disk-watch
category: stand
on:
 schedule:
  every: 15m
gate:
 command: python -m gateway.disk_watch
steps:
 - name: clean
  main_model: |
   Место на диске ниже 10%: проверь, что занимает, и почисти безопасное.
```

Порядок: триггер (`on`) срабатывает по расписанию или событию шины. Условие вызова модели (`gate`) запускает код без модели — здесь
проверка свободного места; пустой вывод означает «всё в норме», и шаг не идёт. Шаг (`steps`) зовёт основную модель
только тогда, когда условие что-то нашло.

Из чего собирать — [features](../features/README.md): рабочее место в файле — [workflows](../features/automation/workflows.md),
проверка без модели — [guard](../features/safety/guard.md), сигнал человеку — [voice](../features/voice/voice.md).

Решение о такой форме — [ADR-020](../adr/020-light-stand-workflows.md).
