"""
SAM CLI — управление gateway из командной строки.

Использование:
    python -m gateway.cli llm switch litellm
    python -m gateway.cli llm switch custom
    python -m gateway.cli llm switch auto
    python -m gateway.cli llm status
"""

import sys
import json
import os
import urllib.request
import urllib.error

from gateway import privilege_key
from gateway.access import auth_headers  # ключ SAM_API_KEY (SAM-79)


SAM_SERVER = os.getenv("SAM_SERVER", "http://localhost:8000")


def _post(path: str, data: dict) -> dict:
    url = f"{SAM_SERVER}{path}"
    body = json.dumps(data).encode()
    extra = privilege_key.headers() if privilege_key.needs("POST", path) else {}   # ручки агентов (SAM-177)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **auth_headers(), **extra},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _get(path: str) -> dict:
    url = f"{SAM_SERVER}{path}"
    req = urllib.request.Request(url, headers=auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_llm(args: list[str]) -> None:
    if not args:
        print("Usage: gateway.cli llm <switch|status> [adapter] [--source SOURCE]")
        sys.exit(1)

    sub = args[0]

    if sub == "status":
        result = _get("/sam/llm/status")
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif sub == "switch":
        if len(args) < 2:
            print("Usage: gateway.cli llm switch <litellm|custom|auto> [--source SOURCE]")
            sys.exit(1)
        adapter = args[1]
        source = None
        if "--source" in args:
            idx = args.index("--source")
            if idx + 1 < len(args):
                source = args[idx + 1]
        payload: dict = {"adapter": adapter}
        if source:
            payload["source"] = source
        result = _post("/sam/llm/switch", payload)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    else:
        print(f"Unknown llm subcommand: {sub!r}")
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m gateway.cli <llm> ...")
        sys.exit(0)

    cmd = args[0]
    if cmd == "llm":
        cmd_llm(args[1:])
    else:
        print(f"Unknown command: {cmd!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
