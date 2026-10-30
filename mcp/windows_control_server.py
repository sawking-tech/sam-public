"""
SAM Windows Control MCP Server.

Инструменты:
  run_command(cmd)          — запустить команду в cmd.exe, вернуть вывод
  open_terminal(title, cmd) — открыть новое окно cmd/PowerShell
  powershell(script)        — выполнить PowerShell скрипт
  show_notification(msg)    — всплывающее уведомление через PowerShell
  get_windows()             — список открытых окон с заголовками
  get_audio_state()         — что сейчас воспроизводит звук и громкость
  screenshot()              — скриншот экрана в base64 (для визуальной проверки)
"""

import subprocess
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp.server.fastmcp import FastMCP
from gateway.env import env_float, env_int  # noqa: E402

mcp = FastMCP("windows-control")


# Срок команды меньше срока вызова в агрегаторе (MCP_CALL_TIMEOUT_S, 30 с): иначе агрегатор отвечает
# «timed out» раньше, чем команда вернёт свой вывод и признак таймаута.
COMMAND_TIMEOUT_S = env_float("SAM_COMMAND_TIMEOUT_S", 25.0)
OUTPUT_LIMIT = env_int("SAM_COMMAND_OUTPUT_LIMIT", 3000)   # символов вывода в ответ модели
NO_OUTPUT = "(no output)"
IS_WINDOWS = sys.platform == "win32"


def console_codepage() -> str:
    """Кодировка вывода консольных программ: OEM-страница консоли (cp866 на ru-RU) или кодировка локали."""
    if IS_WINDOWS:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP() or ctypes.windll.kernel32.GetOEMCP()
        if cp:
            return f"cp{cp}"
    import locale
    return locale.getpreferredencoding(False)


def decode_output(data: bytes, fallback: str) -> str:
    """UTF-8, если байты им являются (python с PYTHONIOENCODING, git), иначе кодировка консоли:
    cmd и Windows PowerShell пишут в OEM, и при чтении как UTF-8 кириллица становилась «�ਢ��»."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(fallback, errors="replace")


def format_output(stdout: str, stderr: str, limit: int = OUTPUT_LIMIT) -> str:
    output = (stdout + stderr).strip()
    return output[:limit] if output else NO_OUTPUT


def _kill_tree(proc: subprocess.Popen) -> None:
    """Убить процесс с потомками: при shell=True kill() гасит только cmd.exe, а внук держит
    каналы вывода, и чтение остатка вывода после таймаута висело бы без конца."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdin=subprocess.DEVNULL, capture_output=True)
    else:
        import signal
        os.killpg(proc.pid, signal.SIGKILL)


def run_process(args, shell: bool = False, timeout: float = COMMAND_TIMEOUT_S) -> str:
    """Выполнить процесс и вернуть его вывод.

    stdin=DEVNULL обязателен: stdin сервера - канал JSON-RPC агрегатора, поток FastMCP висит на нём
    в синхронном ReadFile, и на Windows потомок, унаследовавший этот дескриптор, ждал следующего
    запроса в канале - любая команда, даже git --version, заканчивалась таймаутом вызова (15 окт)."""
    proc = subprocess.Popen(args, shell=shell, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=not IS_WINDOWS)
    codepage = console_codepage()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        partial = format_output(decode_output(out, codepage), decode_output(err, codepage))
        return f"Timeout ({timeout:g}s), process killed. Output so far:\n{partial}"
    return format_output(decode_output(out, codepage), decode_output(err, codepage))


@mcp.tool()
def run_command(command: str) -> str:
    """Run a shell command via cmd.exe. Returns stdout + stderr.
    For PowerShell code use the `powershell` tool (argument `script`), not `powershell -Command` here."""
    try:
        return run_process(command, shell=True)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def open_terminal(title: str = "SAM", command: str = "") -> str:
    """
    Open a new cmd.exe terminal window.
    title   — window title
    command — optional command to run in the terminal (stays open after)
    """
    try:
        if command:
            cmd = f'start "{title}" cmd.exe /K "{command}"'
        else:
            cmd = f'start "{title}" cmd.exe'
        subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL)   # stdin сервера - канал JSON-RPC, см. run_process
        return f"Terminal '{title}' opened" + (f" with: {command}" if command else "")
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def powershell(script: str) -> str:
    """
    Execute a PowerShell script. Good for: UI automation, registry, system info,
    scheduled tasks, notifications, reading files.
    The only argument is `script` - PowerShell code as text (there is no `command` argument).
    """
    try:
        return run_process(["powershell", "-NoProfile", "-NonInteractive", "-Command", powershell_prelude() + script])
    except Exception as e:
        return f"Error: {e}"


def powershell_prelude() -> str:
    """Префикс скрипта PowerShell: вывод в UTF-8 (по умолчанию OEM, кириллица в заголовках окон
    приходила как «��������»), Get-Content читает UTF-8 (Windows PowerShell 5 без -Encoding читает
    файл в ANSI, и русский docstring из репозитория приходил как «РџРѕРґРЅСЏС‚СЊ»)."""
    return ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$PSDefaultParameterValues['Get-Content:Encoding'] = 'UTF8'\n")


@mcp.tool()
def show_notification(message: str, title: str = "SAM") -> str:
    """Show a Windows toast notification (bottom-right corner)."""
    script = (
        f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        f"ContentType = WindowsRuntime] | Out-Null; "
        f"$template = [Windows.UI.Notifications.ToastNotificationManager]"
        f"::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$template.GetElementsByTagName('text')[0].AppendChild("
        f"$template.CreateTextNode('{title}')) | Out-Null; "
        f"$template.GetElementsByTagName('text')[1].AppendChild("
        f"$template.CreateTextNode('{message}')) | Out-Null; "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        f"[Windows.UI.Notifications.ToastNotificationManager]"
        f"::CreateToastNotifier('SAM').Show($toast)"
    )
    return powershell(script)


@mcp.tool()
def get_windows() -> str:
    """
    List all visible windows with their titles and process names.
    Use this to verify an app opened, check what's running on screen.
    """
    script = (
        "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | "
        "Select-Object @{N='pid';E={$_.Id}}, @{N='name';E={$_.Name}}, @{N='title';E={$_.MainWindowTitle}} | "
        "ConvertTo-Json -Compress"
    )
    return powershell(script)


@mcp.tool()
def get_audio_state() -> str:
    """
    Check what applications are currently playing audio and at what volume.
    Use this to verify music/video started playing.
    """
    script = r"""
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
'@ -ErrorAction SilentlyContinue

$result = @()
try {
    # Get active audio sessions via Get-Process + audio heuristic
    $procs = Get-Process | Where-Object { $_.Name -match 'chrome|firefox|msedge|yandex|spotify|vlc|wmplayer|groove|music' }
    foreach ($p in $procs) {
        $result += [PSCustomObject]@{ pid=$p.Id; name=$p.Name; title=$p.MainWindowTitle }
    }
} catch {}

# Check system audio volume
$vol = (Get-AudioDevice -Playback -ErrorAction SilentlyContinue).Volume
if (-not $vol) {
    $vol = (powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys('')" 2>$null)
}

[PSCustomObject]@{
    audio_processes = $result
    note = "If chrome/browser is in list — likely playing audio"
} | ConvertTo-Json -Depth 3
"""
    return powershell(script)


# Полноэкранный PNG 4K весит сотни килобайт base64 и не помещается в ответ модели:
# отдаём либо весь экран мелким, либо нужную зону, и то и другое в рамке max_side x max_side (512 на 512 хватает).
# Значения по умолчанию - из окружения, параметры вызова их перекрывают.
SCREENSHOT_MAX_SIDE = env_int("SAM_SCREENSHOT_MAX_SIDE", 512)  # длинная сторона результата, 0 - без уменьшения
SCREENSHOT_QUALITY = env_int("SAM_SCREENSHOT_QUALITY", 70)     # 1-100, JPEG
SCREENSHOT_FILE = "sam_screenshot.jpg"


def scaled_size(width: int, height: int, max_side: int) -> tuple[int, int]:
    """Размер после уменьшения так, чтобы длинная сторона стала max_side; меньший кадр не увеличивается."""
    longest = max(width, height)
    if max_side <= 0 or longest <= max_side:
        return width, height
    ratio = max_side / longest
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def clamp_region(x: int, y: int, width: int, height: int, screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Зона в границах экрана как (left, top, right, bottom); width или height 0 - до края экрана."""
    left = min(max(x, 0), screen_w - 1)
    top = min(max(y, 0), screen_h - 1)
    right = screen_w if width <= 0 else min(left + width, screen_w)
    bottom = screen_h if height <= 0 else min(top + height, screen_h)
    return left, top, right, bottom


def _capture_script(path: str) -> str:
    return f"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
$g.Dispose()
$bmp.Save('{path.replace(chr(92), "/")}')
$bmp.Dispose()
Write-Output 'saved'
"""


def _compress(png_path: str, jpg_path: str, region: tuple[int, int, int, int], max_side: int, quality: int) -> tuple[int, int]:
    """PNG -> JPEG зоны в рамке max_side; вернуть итоговый размер. Сжатие в Pillow: PowerShell-вариант блокирует антивирус."""
    from PIL import Image
    with Image.open(png_path) as img:
        left, top, right, bottom = clamp_region(*region, *img.size)
        zone = img.convert("RGB").crop((left, top, right, bottom))
        out_w, out_h = scaled_size(*zone.size, max_side)
        zone.resize((out_w, out_h), Image.LANCZOS).save(jpg_path, "JPEG", quality=quality)
    return out_w, out_h


@mcp.tool()
def screenshot(x: int = 0, y: int = 0, width: int = 0, height: int = 0,
               max_side: int = SCREENSHOT_MAX_SIDE, quality: int = SCREENSHOT_QUALITY) -> str:
    """
    Screenshot of the whole screen (default) or of a zone, downscaled to fit max_side pixels and JPEG-compressed.
    To read fine detail, call again with the zone: x, y = top-left corner in screen pixels, width, height = its size
    (0 = up to the screen edge); the zone is then shown at up to max_side, so small text becomes legible.
    max_side - longest side of the result in pixels (0 = no scaling); quality - JPEG quality 1-100.
    Full-resolution PNG stays at the path in the temp dir (sam_screenshot.png).
    """
    import tempfile, base64
    png_path = os.path.join(tempfile.gettempdir(), "sam_screenshot.png")
    jpg_path = os.path.join(tempfile.gettempdir(), SCREENSHOT_FILE)
    result = powershell(_capture_script(png_path))
    if "saved" not in result or not os.path.exists(png_path):
        return f"screenshot failed: {result}"
    out_w, out_h = _compress(png_path, jpg_path, (x, y, width, height), max_side, min(100, max(1, quality)))
    with open(jpg_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    # Полный data URL - gateway agent_loop вытащит его и пробросит
    # в multimodal content tool message для vision-моделей.
    return f"screenshot_ready path={jpg_path} size={out_w}x{out_h} bytes={len(b64)}\ndata:image/jpeg;base64,{b64}"


# ─── Background processes (для запуска dev servers, npm, watcher'ов и т.п.) ──

import threading
import time as _time
from collections import deque
import uuid as _uuid

_bg_lock = threading.Lock()
_bg_procs: dict[str, dict] = {}  # handle → {proc, stdout_buf, started_at, cmd}


def _reader_thread(handle: str, proc):
    """Читает stdout/stderr процесса в кольцевой буфер."""
    entry = _bg_procs.get(handle)
    if not entry:
        return
    buf = entry["stdout_buf"]
    for line in iter(proc.stdout.readline, b""):
        try:
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
        except Exception:
            decoded = "<decode-err>"
        with _bg_lock:
            buf.append(decoded)
    entry["exit_code"] = proc.poll()


@mcp.tool()
def bg_start(command: str, cwd: str = "", shell: bool = True) -> str:
    """Запустить фоновый процесс, читать его stdout в кольцевой буфер.

    Возвращает handle (короткий id). Используй bg_tail(handle) чтобы
    посмотреть последние строки вывода. bg_stop(handle) — убить.

    command — команда (если shell=true — через cmd.exe; иначе argv-список)
    cwd     — рабочая директория, по умолчанию текущая
    """
    handle = _uuid.uuid4().hex[:8]
    try:
        proc = subprocess.Popen(
            command,
            shell=shell,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,   # stdin сервера - канал JSON-RPC, см. run_process
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
    except Exception as e:
        return f"Error starting: {e}"
    entry = {
        "proc": proc,
        "stdout_buf": deque(maxlen=2000),
        "started_at": _time.time(),
        "cmd": command,
        "exit_code": None,
    }
    with _bg_lock:
        _bg_procs[handle] = entry
    threading.Thread(
        target=_reader_thread, args=(handle, proc), daemon=True,
    ).start()
    return f"bg_handle={handle} pid={proc.pid} cmd={command[:60]!r}"


@mcp.tool()
def bg_tail(handle: str, n: int = 30) -> str:
    """Вернуть последние N строк stdout фонового процесса по handle."""
    with _bg_lock:
        entry = _bg_procs.get(handle)
        if not entry:
            return f"No such handle: {handle}"
        buf = list(entry["stdout_buf"])
        proc = entry["proc"]
        ec = entry.get("exit_code")
    status = "running" if proc.poll() is None else f"exited(code={proc.poll()})"
    lines = buf[-n:] if n > 0 else buf
    age = int(_time.time() - entry["started_at"])
    head = f"[{handle}] {status} age={age}s lines={len(buf)}\n"
    return head + "\n".join(lines) if lines else head + "(no output yet)"


@mcp.tool()
def bg_stop(handle: str) -> str:
    """Убить фоновый процесс по handle."""
    with _bg_lock:
        entry = _bg_procs.pop(handle, None)
    if not entry:
        return f"No such handle: {handle}"
    proc = entry["proc"]
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        return f"Error stopping {handle}: {e}"
    return f"Stopped {handle} (was: {entry['cmd'][:60]!r})"


@mcp.tool()
def bg_list() -> str:
    """Список всех живых фоновых процессов с краткой инфой."""
    with _bg_lock:
        items = list(_bg_procs.items())
    if not items:
        return "No background processes"
    lines = []
    for h, e in items:
        proc = e["proc"]
        status = "running" if proc.poll() is None else f"exited({proc.poll()})"
        age = int(_time.time() - e["started_at"])
        lines.append(
            f"  {h}  pid={proc.pid}  age={age}s  {status}  cmd={e['cmd'][:50]!r}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
