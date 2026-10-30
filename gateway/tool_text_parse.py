"""Разбор вызовов инструментов из текста ответа модели (мета-формат): Harmony, теги <tool_call>, Gemma, [name(...)],
[tool_call: name(...)], [tool_call] {json} и голые вызовы name(...) в начале строки.

Чистый модуль: известные инструменты приходят параметром, поэтому разбор одинаков в шлюзе (реестр его tools) и в
Cortex (инструменты MCP сессии).
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping


def parse_channel_format(content: str, registry: Mapping[str, Any]) -> list[tuple[str, dict]]:
    """
    Парсит tool calls из текстового формата моделей со special tokens.

    Формат 1 — OpenAI Harmony (gpt-oss-20b):
      <|channel|>commentary to=functions.TOOL <|constrain|>json<|message|>{args_json}<|call|>
      Аргументы — плоский JSON (не {"name": ..., "arguments": ...})

    Формат 2 — <tool_call> теги (Qwen и другие):
      <tool_call>{"name": "TOOL", "arguments": {...}}</tool_call>

    registry - известные инструменты: имя -> их parameters (список описаний с полем name или JSON-схема).
    Возвращает список (tool_name, args) пар.
    """
    calls = []

    # Формат 1: Harmony — <|channel|>commentary to=functions.TOOL <|constrain|>json<|message|>{...}
    if "<|channel|>" in content:
        for segment in content.split("<|channel|>")[1:]:
            # Пропускаем non-tool каналы: final, analysis, voice
            channel_type = segment.split()[0] if segment.strip() else ""
            if channel_type in ("final", "analysis"):
                continue
            # to=functions.create_task  ИЛИ  to=create_task (без префикса)
            to_m = re.search(r'to=(?:functions\.)?(\w+)', segment)
            if not to_m:
                continue
            tool_name = to_m.group(1)
            if tool_name in ("assistant", "functions"):  # служебные имена
                continue
            msg_m = re.search(r'<\|message\|>(.*?)(?:<\|(?:call|end|return)\|>|\Z)', segment, re.DOTALL)
            if not msg_m:
                msg_m = re.search(r'<\|message\|>(.*)', segment, re.DOTALL)
            try:
                raw_json = (msg_m.group(1).strip() if msg_m else "").strip()
                args = json.loads(raw_json) if raw_json.startswith("{") else {}
            except Exception:
                args = {}
            calls.append((tool_name, args))

    # Формат 2: <tool_call>{...}</tool_call>  ИЛИ  <tool_code>{...}</tool_code>
    for m in re.finditer(r'<tool_(?:call|code)>(.*?)</tool_(?:call|code)>', content, re.DOTALL):
        inner = m.group(1).strip()
        # Подформат 2a: JSON {"name": "...", "arguments": {...}}
        if inner.startswith("{"):
            # Модель иногда добавляет мусорные теги после JSON — извлекаем только JSON-объект
            json_str = inner
            try:
                data = json.loads(json_str)
            except Exception:
                # Попытка вырезать JSON до первого тега или мусора
                m2 = re.search(r'(\{.*\})', inner, re.DOTALL)
                try:
                    data = json.loads(m2.group(1)) if m2 else None
                except Exception:
                    data = None
            if data:
                name = data.get("name") or data.get("function", {}).get("name", "")
                args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
                if name:
                    calls.append((name, args))
        else:
            # Подформат 2b: <function=NAME><parameter=KEY>VALUE</parameter>...</function>
            fn_m = re.search(r'<function=(\w+)>', inner)
            if fn_m:
                name = fn_m.group(1)
                args = {}
                for p in re.finditer(r'<parameter=(\w+)>\s*(.*?)\s*</parameter>', inner, re.DOTALL):
                    key, val = p.group(1), p.group(2).strip()
                    try:
                        args[key] = json.loads(val)
                    except Exception:
                        args[key] = val
                calls.append((name, args))
            else:
                # Подформат 2c: GLM-4 формат —
                #   <tool_call>NAME<arg_key>K</arg_key><arg_value>V</arg_value>...</tool_call>
                # NAME — голый идентификатор перед первым <arg_key>.
                glm_m = re.match(r'\s*([A-Za-z_][\w\-]*)\s*(<arg_key>)', inner)
                if glm_m:
                    name = glm_m.group(1)
                    args = {}
                    for p in re.finditer(
                        r'<arg_key>\s*(.*?)\s*</arg_key>\s*'
                        r'<arg_value>\s*(.*?)\s*</arg_value>',
                        inner, re.DOTALL,
                    ):
                        key, val = p.group(1), p.group(2)
                        try:
                            args[key] = json.loads(val)
                        except Exception:
                            args[key] = val.strip('"\'')
                    calls.append((name, args))

    # Формат 6: Gemma 4 (gemma-4-31b и т.п.) — кастомный мини-DSL
    #   <|tool_call>call:NAME{key:value,key:<|"|>str<|"|>,...}<tool_call|>
    # Ключи без кавычек, строки в <|"|>...<|"|>, booleans/числа bare.
    for m in re.finditer(
        r'<\|tool_call>call:(\w+)\{(.+?)\}<tool_call\|>', content, re.DOTALL,
    ):
        name, raw = m.group(1), m.group(2)
        try:
            args = _parse_gemma_args(raw)
        except Exception:
            args = {}
        calls.append((name, args))

    # Формат 3: [tool_name()] или [tool_name("string")] или [tool_name({...})]
    # Qwen иногда пишет вызовы в таком формате вместо native API
    _known_tools = set(registry.keys())
    for m in re.finditer(r'\[(\w+)\(([^)]*)\)\]', content):
        name = m.group(1)
        if name not in _known_tools:
            continue
        args_str = m.group(2).strip()
        args: dict = {}
        if args_str:
            try:
                parsed = json.loads(args_str)
                if isinstance(parsed, dict):
                    args = parsed
                elif isinstance(parsed, str):
                    # [create_task("description")] → {"description": parsed}
                    first_param = next(iter(registry[name] or []), {})
                    param_name = first_param.get("name", "input") if isinstance(first_param, dict) else "input"
                    args = {param_name: parsed}
            except Exception:
                # Попытка снять кавычки если это просто строка
                s = args_str.strip('"\'')
                first_param = next(iter(registry[name] or []), {})
                param_name = first_param.get("name", "input") if isinstance(first_param, dict) else "input"
                args = {param_name: s} if s else {}
        calls.append((name, args))

    # Формат 4: [tool_call: NAME(key1=val1, key2="str", key3=true)]
    # Qwen иногда так пишет вместо native tool API. kwargs-стиль.
    for m in re.finditer(
        r'\[tool_call:\s*(\w+)\((.*?)\)\]', content, re.DOTALL,
    ):
        name = m.group(1)
        if name not in _known_tools:
            continue
        kwargs_str = m.group(2).strip()
        args: dict = {}
        if kwargs_str:
            for kv in re.finditer(
                r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^,]+)',
                kwargs_str,
            ):
                k = kv.group(1)
                v_raw = kv.group(2).strip().rstrip(",").strip()
                try:
                    v = json.loads(v_raw)
                except Exception:
                    if v_raw.lower() == "true":
                        v = True
                    elif v_raw.lower() == "false":
                        v = False
                    elif v_raw.lower() in ("none", "null"):
                        v = None
                    else:
                        v = v_raw.strip('"\'')
                args[k] = v
        calls.append((name, args))

    # Формат 7: [tool_call] {"name": NAME, "arguments": {...}} - метка без тегов и JSON за ней; так пишет glm-5.3-flash
    # вместо нативного вызова (SAM-217). JSON читается raw_decode: вложенные скобки аргументов regex не берёт
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\[tool_call\]\s*(?=\{)', content):
        try:
            data, _ = decoder.raw_decode(content, m.end())
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or (data.get("function") or {}).get("name", "")
        args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name in _known_tools and isinstance(args, dict):
            calls.append((name, args))

    # Формат 5: голые Python-style вызовы в начале строки:
    #   report_status("text")
    #   mcp_call(device, "browser_navigate", {"url": "..."})
    # Qwen такое пишет когда «рассуждает планом» вместо tool API.
    # Используем balanced bracket scan (regex не справляется с вложенными).
    if _known_tools:
        already = {(n, json.dumps(a, sort_keys=True)) for n, a in calls}
        names_re = "|".join(re.escape(n) for n in _known_tools)
        start_pattern = re.compile(
            r'(?:^|\n)\s*(' + names_re + r')\s*\(', re.DOTALL,
        )
        for m in start_pattern.finditer(content):
            name = m.group(1)
            # Сканируем от позиции после ( до балансированной закрывающей )
            i = m.end()
            depth = 1
            in_str: str | None = None
            while i < len(content) and depth > 0:
                ch = content[i]
                if in_str:
                    if ch == "\\" and i + 1 < len(content):
                        i += 2; continue
                    if ch == in_str:
                        in_str = None
                elif ch in ('"', "'"):
                    in_str = ch
                elif ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            inner = content[m.end():i-1]
            args = _parse_python_call_args(
                inner.strip(), _registry=registry, name=name,
            )
            key = (name, json.dumps(args, sort_keys=True))
            if key in already:
                continue
            already.add(key)
            calls.append((name, args))

    return calls


def _parse_gemma_args(raw: str) -> dict:
    """Парсер мини-DSL аргументов Gemma 4 tool_call.

    Формат top-level: key:value,key:value (ключи bare, без кавычек).
    Значения:
      <|"|>str<|"|>  → строка
      true / false   → bool
      123 / 1.5      → число
      {...}          → вложенный объект (рекурсивно, ключи тоже bare)
      [...]          → массив

    Стратегия: трансформируем DSL → валидный JSON, потом json.loads.
    1. <|"|>...<|"|> → "..." (теперь строки в стандартном JSON виде).
    2. Bare identifier перед ':' → "identifier": (квотим ключи).
    Идентификаторы НЕ перед ':' (true/false/null) оставляем как есть.
    """
    s = raw.replace('<|"|>', '"')
    out: list[str] = []
    i, in_str, esc = 0, False, False
    while i < len(s):
        ch = s[i]
        if esc:
            out.append(ch); esc = False; i += 1; continue
        if in_str:
            out.append(ch)
            if ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            i += 1; continue
        if ch == '"':
            in_str = True; out.append(ch); i += 1; continue
        if ch.isalpha() or ch == '_':
            j = i
            while j < len(s) and (s[j].isalnum() or s[j] == '_'):
                j += 1
            ident = s[i:j]
            k = j
            while k < len(s) and s[k] in ' \t\n\r':
                k += 1
            if k < len(s) and s[k] == ':':
                out.append('"' + ident + '"')
            else:
                out.append(ident)
            i = j; continue
        out.append(ch); i += 1
    json_str = '{' + ''.join(out) + '}'
    try:
        data = json.loads(json_str)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_python_call_args(s: str, _registry: Mapping[str, Any], name: str) -> dict:
    """Парсер аргументов Python-style: позиционные и kwargs.

    Поддерживает:
      "string"     | 'string'      → str
      key=val      | key="str"     → kwargs
      {...}        → JSON dict     (для аргумента типа args)
      true/false/null/123/1.5

    Маппит позиционные на parameters регистрированного tool'а
    в порядке: 1-й positional → 1-й param и т.д.
    """
    if not s.strip():
        return {}

    args: dict = {}
    positional: list = []

    # Простой токенизатор: уважает кавычки и {} []
    tokens: list[str] = []
    buf = []
    depth = 0
    in_str: str | None = None
    i = 0
    while i < len(s):
        ch = s[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(s):
                buf.append(s[i+1]); i += 2; continue
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            tokens.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf).strip())

    def _coerce(v: str):
        v = v.strip()
        if not v:
            return None
        try:
            return json.loads(v)
        except Exception:
            pass
        # Python-style константы
        if v.lower() == "true": return True
        if v.lower() == "false": return False
        if v.lower() in ("none", "null"): return None
        # Снимаем кавычки если строка
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        return v

    for tok in tokens:
        kv = re.match(r'^([A-Za-z_]\w*)\s*=\s*(.*)$', tok, re.DOTALL)
        if kv:
            args[kv.group(1)] = _coerce(kv.group(2))
        else:
            positional.append(_coerce(tok))

    # Mapping позиционных на parameters tool'а
    if positional and name in _registry:
        try:
            param_names = [p["name"] for p in (_registry[name] or [])]
            for i, val in enumerate(positional):
                if i < len(param_names) and param_names[i] not in args:
                    args[param_names[i]] = val
        except Exception:
            pass

    return args
