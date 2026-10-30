"""Поддельный исполнитель быстрой песочницы для тестов пула без Docker: протокол шлюза (sandbox_repl), поведение - по
тексту кода. Настоящий исполнитель нужен Linux (fork, prctl, SCM_RIGHTS) и проверяется живым тестом.

  set имя=значение / get имя  - состояние между запусками (нет имени - NameError)
  hang                        - не отвечать
  flood                       - ответить и прислать кадр без запроса
  forge                       - ответить кадром с чужим nonce
  die                         - выйти
  остальное                   - вывод равен коду

Ключи: --deaf - после готовности не читать stdin; --slow-start С - готовность через С секунд.
"""
import json
import sys
import time

FOREVER_S = 3600


def main() -> None:
    args = sys.argv[1:]
    if "--slow-start" in args:
        time.sleep(float(args[args.index("--slow-start") + 1]))
    out = sys.stdout.buffer

    def send(frame: dict) -> None:
        out.write(json.dumps(frame).encode("utf-8") + b"\n")
        out.flush()

    send({"ready": True, "langs": ["python", "shell", "node", "sql"]})
    if "--deaf" in args:
        time.sleep(FOREVER_S)
    state = {}
    for line in sys.stdin.buffer:
        frame = json.loads(line)
        code = frame.get("code", "")
        if code == "hang":
            time.sleep(FOREVER_S)
        if code == "die":
            return
        if code.startswith("set "):
            name, value = code[4:].split("=", 1)
            state[name] = value
            text = ""
        elif code.startswith("get "):
            text = state.get(code[4:], "NameError")
        else:
            text = code
        nonce = "угаданный" if code == "forge" else frame.get("nonce")
        send({"nonce": nonce, "code": 0, "out": text, "truncated": False, "ms": 0.1})
        if code == "flood":
            send({"nonce": frame.get("nonce"), "code": 0, "out": "лишний", "truncated": False, "ms": 0.1})


if __name__ == "__main__":
    main()
