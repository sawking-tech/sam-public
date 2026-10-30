"""Прокси-«жучок» перед OpenAI-совместимым сервером модели (LM Studio): пропускает запросы как есть и пишет тело каждого
запроса строкой JSONL - чтобы сравнить, что разные агенты (шлюз SAM, Hermes, OpenCode) на деле шлют одной модели.

Запуск:
  python scripts/llm_tap.py [--port 1235] [--target http://127.0.0.1:1234] [--out data/llm_tap/requests.jsonl]
Агенту - base_url http://127.0.0.1:1235/v1 вместо сервера модели. Сводка по записанному:
  python scripts/llm_tap.py --summary [--out ...]
"""

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 1235
DEFAULT_TARGET = "http://127.0.0.1:1234"
DEFAULT_OUT = ROOT / "data" / "llm_tap" / "requests.jsonl"
TIMEOUT_S = 600.0            # ответ локальной модели с рассуждением идёт минутами
SKIP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


def record(out: Path, path: str, body: bytes) -> None:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {"raw": body[:2000].decode("utf-8", "replace")}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "path": path, "body": data}, ensure_ascii=False) + "\n")


def summary_line(entry: dict) -> str:
    """Одна строка сравнения: инструменты, tool_choice, размер системной части и всего входа, роли сообщений."""
    body = entry.get("body", {})
    messages = body.get("messages", [])
    system = sum(len(str(m.get("content", ""))) for m in messages if m.get("role") == "system")
    total = len(json.dumps(messages, ensure_ascii=False))
    tools = body.get("tools") or []
    names = [t.get("function", {}).get("name", "?") for t in tools]
    return (f"{entry.get('path')} model={body.get('model')} tools={len(tools)} tool_choice={body.get('tool_choice')} "
            f"system_chars={system} messages_chars={total} roles={[m.get('role') for m in messages]} "
            f"tools_chars={len(json.dumps(tools, ensure_ascii=False))} names={names[:12]}")


def summary(out: Path) -> None:
    for line in out.read_text(encoding="utf-8").splitlines():
        print(summary_line(json.loads(line)))


def build_app(target: str, out: Path):
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import Response, StreamingResponse

    app = FastAPI()
    client = httpx.AsyncClient(base_url=target, timeout=TIMEOUT_S)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        body = await request.body()
        if request.method == "POST":
            record(out, "/" + path, body)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in SKIP_HEADERS}
        upstream = client.build_request(request.method, "/" + path, content=body, headers=headers,
                                        params=dict(request.query_params))
        resp = await client.send(upstream, stream=True)
        passed = {k: v for k, v in resp.headers.items() if k.lower() not in SKIP_HEADERS | {"content-encoding"}}
        if "text/event-stream" in resp.headers.get("content-type", ""):
            return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=passed)
        content = await resp.aread()
        await resp.aclose()
        return Response(content, status_code=resp.status_code, headers=passed)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--summary", action="store_true", help="сводка по записанным запросам и выход")
    args = parser.parse_args()
    if args.summary:
        summary(args.out)
        return
    import uvicorn
    uvicorn.run(build_app(args.target, args.out), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
