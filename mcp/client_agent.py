"""
SAM Client Agent — запускается на каждом ПК при старте.

Что делает:
1. Собирает инфу о системе (IP, GPU, RAM, CPU, OS)
2. Запускает mcp-proxy (Playwright + filesystem + Windows Control)
3. Регистрируется на SAM сервере (POST /sam/client/register)
4. Каждые 30 сек шлёт heartbeat с presence_score
5. При завершении — POST /sam/client/offline

Запуск: python mcp/client_agent.py
        python mcp/client_agent.py --server http://<SAM_SERVER_IP>:8000 --name workpc
"""

import os
import sys
import json
import time
import socket
import signal
import asyncio
import argparse
import platform
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

# Ключ SAM_API_KEY для запросов к gateway (SAM-79): модуль доступа общий с gateway, корень репозитория в пути
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gateway.access import auth_headers  # noqa: E402
from gateway.env import env_int  # noqa: E402

# ─── Конфиг ──────────────────────────────────────────────────────────────────

DEFAULT_SERVER = os.getenv("SAM_SERVER", "http://localhost:8000")
DEFAULT_NAME   = os.getenv("DEVICE_NAME", socket.gethostname().lower())
MCP_PORT       = env_int("MCP_PORT", 3000)
HEARTBEAT_SEC  = 30

# ─── Системная инфа ───────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_mac() -> str:
    """MAC адрес основного сетевого интерфейса."""
    try:
        import uuid
        mac = uuid.getnode()
        return ":".join(f"{(mac >> (8*i)) & 0xff:02x}" for i in reversed(range(6)))
    except Exception:
        return ""


def get_gpu() -> str:
    """Название GPU через nvidia-smi или WMI."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip().split("\n")[0]
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            r = subprocess.run(
                ["wmic", "path", "win32_videocontroller", "get", "name", "/value"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if line.startswith("Name=") and line.strip() != "Name=":
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


def get_ram_gb() -> float:
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024**3), 1)
    except ImportError:
        if platform.system() == "Windows":
            try:
                r = subprocess.run(
                    ["wmic", "computersystem", "get", "TotalPhysicalMemory", "/value"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in r.stdout.splitlines():
                    if line.startswith("TotalPhysicalMemory="):
                        return round(int(line.split("=")[1]) / (1024**3), 1)
            except Exception:
                pass
    return 0.0


def get_presence_score() -> float:
    """
    Простой presence score на основе активности мыши/клавиатуры.
    Полная версия — в radar/presence.py.
    """
    score = 0.0
    try:
        import psutil
        # CPU activity как прокси присутствия
        cpu = psutil.cpu_percent(interval=1)
        score = min(cpu * 0.5, 50.0)
    except ImportError:
        pass
    return round(score, 1)


def collect_system_info(name: str, server: str) -> dict:
    ip = get_local_ip()
    return {
        "name":         name,
        "ip":           ip,
        "mac":          get_mac(),
        "os":           f"{platform.system()} {platform.release()}",
        "cpu":          platform.processor() or platform.machine(),
        "ram_gb":       get_ram_gb(),
        "gpu":          get_gpu(),
        "mcp_url":      f"http://{ip}:{MCP_PORT}",
        "capabilities": get_capabilities(),
        "auto_wake":    True,
    }


def get_capabilities() -> list[str]:
    caps = ["filesystem"]
    # Playwright
    try:
        subprocess.run(["npx", "@playwright/mcp", "--version"],
                       capture_output=True, timeout=5)
        caps.append("playwright")
    except Exception:
        pass
    # Windows Control
    if platform.system() == "Windows":
        caps.append("windows-control")
        caps.append("screenshot")
    return caps


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def post_json(url: str, data: dict) -> bool:
    try:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", **auth_headers()},
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[ClientAgent] POST {url} failed: {e}")
        return False


# ─── Chrome CDP ───────────────────────────────────────────────────────────────

CHROME_CDP_PORT = env_int("CHROME_CDP_PORT", 9222)

def start_chrome_cdp() -> bool:
    """
    Запускает Chrome с --remote-debugging-port если он ещё не запущен.
    Playwright MCP подключится к нему через CDP — использует реальный профиль пользователя.
    """
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{CHROME_CDP_PORT}/json/version", timeout=2)
        print(f"[Chrome] Already running with CDP on port {CHROME_CDP_PORT}")
        return True
    except Exception:
        pass  # не запущен, стартуем

    # Ищем Chrome: реестр → стандартные пути
    chrome = None
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
        chrome, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
    except Exception:
        pass
    if not chrome or not os.path.exists(chrome):
        for p in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")]:
            if os.path.exists(p):
                chrome = p
                break
    if not chrome:
        print("[Chrome] Chrome not found — skipping CDP")
        return False
    if not chrome:
        print("[Chrome] Chrome not found — skipping CDP")
        return False

    subprocess.Popen([
        chrome,
        f"--remote-debugging-port={CHROME_CDP_PORT}",
        "--profile-directory=Default",
        "--disable-features=PostQuantumKyber,QUIC",
        "--no-first-run",
        "--no-default-browser-check",
    ])
    time.sleep(2)
    print(f"[Chrome] Started with CDP on port {CHROME_CDP_PORT}")
    return True


# ─── MCP Aggregator ───────────────────────────────────────────────────────────

def start_mcp_proxy() -> subprocess.Popen | None:
    """
    Запускает mcp_aggregator.py — агрегирует все MCP серверы из configs/local.json
    на одном HTTP порту. Добавить новый MCP: вписать в configs/local.json и рестарт.
    """
    aggregator = os.path.join(os.path.dirname(__file__), "mcp_aggregator.py")
    config = os.path.join(os.path.dirname(__file__), "configs", "local.json")

    if not os.path.exists(config):
        print(f"[MCP] configs/local.json not found — skipping aggregator")
        return None

    cmd = [sys.executable, aggregator, "--port", str(MCP_PORT)]
    print(f"[MCP] Starting aggregator: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(3)  # дать время стартануть всем серверам
        if proc.poll() is not None:
            out, _ = proc.communicate()
            print(f"[MCP] Aggregator died. Output: {out[:500]}")
            return None
        print(f"[MCP] Aggregator running on port {MCP_PORT} (pid={proc.pid})")
        return proc
    except Exception as e:
        print(f"[MCP] Failed to start aggregator: {e}")
        return None


# ─── Main loop ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SAM Client Agent")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="SAM Gateway URL")
    parser.add_argument("--name", default=DEFAULT_NAME, help="Device name")
    parser.add_argument("--no-mcp", action="store_true", help="Don't start mcp-proxy")
    args = parser.parse_args()

    print(f"[ClientAgent] Starting on '{args.name}' -> {args.server}")

    # Собрать инфу о системе
    info = collect_system_info(args.name, args.server)
    print(f"[ClientAgent] IP={info['ip']} GPU={info['gpu']} caps={info['capabilities']}")


    # Запустить mcp-proxy
    mcp_proc = None
    if not args.no_mcp:
        mcp_proc = start_mcp_proxy()

    # Зарегистрироваться
    ok = post_json(f"{args.server}/sam/client/register", info)
    if ok:
        print(f"[ClientAgent] Registered successfully")
    else:
        print(f"[ClientAgent] Registration failed — will retry in heartbeats")

    # Graceful shutdown
    running = True
    def shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Heartbeat loop
    while running:
        time.sleep(HEARTBEAT_SEC)
        if not running:
            break
        score = get_presence_score()
        ok = post_json(f"{args.server}/sam/client/heartbeat", {
            "name": args.name,
            "presence_score": score,
        })
        if not ok:
            # Попробовать перерегистрироваться
            post_json(f"{args.server}/sam/client/register", info)

    # Уведомить сервер об офлайн
    post_json(f"{args.server}/sam/client/offline", {"name": args.name})
    print(f"[ClientAgent] Offline signal sent. Bye.")

    if mcp_proc and mcp_proc.poll() is None:
        mcp_proc.terminate()


if __name__ == "__main__":
    main()
