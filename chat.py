"""
SAM Chat — интерактивный чат в терминале.
Запуск: python chat.py
Или с device ID: python chat.py --device workpc
"""
import sys
import json
import argparse
import urllib.request
import urllib.error

from gateway.access import auth_headers  # ключ SAM_API_KEY (SAM-79)

GATEWAY = "http://localhost:8000"


def chat(message: str, device_id: str = "cli") -> str:
    data = json.dumps({"messages": [{"role": "user", "content": message}]}).encode("utf-8")
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "X-Device-ID": device_id, **auth_headers()},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return f"[Ошибка {e.code}]: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return f"[Ошибка]: {e}"


def main():
    parser = argparse.ArgumentParser(description="SAM Chat CLI")
    parser.add_argument("--device", default="cli", help="Device ID (default: cli)")
    parser.add_argument("--gateway", default=GATEWAY, help=f"Gateway URL (default: {GATEWAY})")
    args = parser.parse_args()

    # Проверка здоровья
    try:
        with urllib.request.urlopen(f"{args.gateway}/health", timeout=3) as r:
            health = json.loads(r.read())
            llm = health.get("model", "unknown")
            source = health.get("llm", "?")
            print(f"SAM готов | {source}: {llm}")
    except Exception:
        print(f"[!] Gateway недоступен: {args.gateway}")
        sys.exit(1)

    print("Введите сообщение (Ctrl+C или 'выход' для выхода)\n")

    while True:
        try:
            user_input = input("Вы: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nПока!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("выход", "exit", "quit", "q"):
            print("Пока!")
            break

        response = chat(user_input, device_id=args.device)
        print(f"SAM: {response}\n")


if __name__ == "__main__":
    # Настройка stdout для UTF-8 на Windows
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    main()
