"""mcp_call tool (SAM-40).

Вызов инструментов на удалённых устройствах через mcp_client.
Все snapshots / большие ответы обрезаются до 1200 символов чтобы не
переполнить LLM-контекст.
"""

from __future__ import annotations

import json
from typing import Callable

MISSING_PARAMS = ("Нужны device и tool. Пример: mcp_call(device=\"<имя устройства>\", tool=\"tools/list\") - список "
                  "инструментов, затем mcp_call(device=..., tool=\"<имя из списка>\", args={...})")
BAD_ARGS = "args - объект JSON, не строка и не список. Пример: args={\"path\": \"C:/work/a.txt\"}"


def normalize_args(raw) -> dict | str:
    """args инструмента MCP: объект как есть, строка JSON разбирается (так их шлёт слабая модель, SAM-239);
    не объект - строка с примером."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return BAD_ARGS
    return raw if isinstance(raw, dict) else BAD_ARGS


async def mcp_call_impl(
    params: dict,
    resolve_mcp_url: Callable[[str], tuple[str | None, str]],
    mcp_request_fn: Callable,
) -> str:
    """Выполнить MCP tool на устройстве.

    Args:
        params: device + tool + args
        resolve_mcp_url: (device) → (url, error_msg). Обычно _get_device_mcp_url
        mcp_request_fn: async (url, method, params, req_id?) → dict
    """
    try:
        device = params.get("device", "")
        tool = params.get("tool", "")
        if not device or not tool:
            return MISSING_PARAMS
        args = normalize_args(params.get("args", {}))
        if isinstance(args, str):
            return args

        mcp_url, err = resolve_mcp_url(device)
        if mcp_url is None:
            return err

        # Специальный case: list всех инструментов на устройстве
        if tool == "tools/list":
            data = await mcp_request_fn(mcp_url, "tools/list", {})
            tools = data.get("result", {}).get("tools", [])
            if not tools:
                return f"Нет инструментов на '{device}'"
            lines = [f"Инструменты '{device}':"]
            for t in tools:
                lines.append(f"  {t['name']}: {t.get('description', '')[:100]}")
            return "\n".join(lines)

        # Обычный вызов
        data = await mcp_request_fn(
            mcp_url, "tools/call", {"name": tool, "arguments": args},
        )
        if not data:
            return f"MCP error: пустой или нечитаемый ответ от '{device}'"
        if "error" in data:
            err_info = data["error"]
            err_str = str(err_info).lower()
            if "unknown" in err_str or "not found" in err_str:
                # Подсказка: что есть на устройстве
                data2 = await mcp_request_fn(mcp_url, "tools/list", {}, req_id=99)
                names = [t["name"]
                         for t in data2.get("result", {}).get("tools", [])]
                return f"Инструмент '{tool}' не найден. Доступные: {', '.join(names)}"
            return f"MCP error: {err_info}"

        content = data.get("result", {}).get("content", [])
        if data.get("result", {}).get("isError"):
            # Ошибка инструмента (протокол MCP): без пометки модель принимала её за результат (SAM-76)
            parts = [c.get("text", str(c)) if isinstance(c, dict) else str(c)
                     for c in (content if isinstance(content, list) else [content])]
            return f"MCP error ({tool} на {device}): {' '.join(parts)[:1200]}"
        if isinstance(content, list) and content:
            text = content[0].get("text", str(content[0]))
            # Screenshot и другие image tools могут вернуть data URL —
            # пропускаем через image_utils: сжать до HD + порезать длинные
            # (multi-monitor 5760x1080 или длинная страница) на HD-чанки.
            if "data:image/" in text and "base64," in text:
                try:
                    from ..image_utils import process_text_with_images
                    return process_text_with_images(text)
                except Exception as e:
                    print(f"[mcp_call] image_utils error: {e}")
                    return text  # хоть с исходным base64
            return text[:1200]  # snapshots могут быть огромными
        return str(data.get("result", ""))[:400]
    except Exception as e:
        return (f"mcp_call ошибка "
                f"({params.get('tool', '?')} на {params.get('device', '?')}): {e}")
