# Мост голоса и сессия Claude Code - основная модель SAM (SAM-91): start_sam поднимает их после шлюза.
# Решение «уже жив» (Test-LeadAlive, Select-BridgeProcess) отделено от чтения файлов и процессов, его можно
# прогнать на подставленных данных. Настройки - переменная окружения, затем .env, затем значение по умолчанию.

$BRIDGE_MODULE   = 'voice.layer.bridge'          # подстрока командной строки моста, как в scripts/stand_watch.py
$BRIDGE_LOG      = 'logs\bridge.log'
$LEAD_ALIVE_NAME = 'lead.alive'                  # voice/layer/bus.py LEAD_ALIVE, пишет scripts/lead_heartbeat.py
$LEAD_ALIVE_S    = 5                             # как SAM_LEAD_ALIVE_S в gateway/agent.py: отметка старше - сессии нет
$LEAD_TAB_TITLE  = 'SAM: Claude Code'
$LEAD_MODEL_NAME = 'claude-code'                 # gateway/main_model.py CLAUDE_CODE
$MAIN_ORDER_DEFAULT = 'llm'                    # gateway/main_model.py DEFAULT_ORDER: claude-code - обход, ставится явно
$PYTHON_NAMES    = @('python.exe', 'pythonw.exe')
# Первое сообщение сессии: без него она молчит до ввода с клавиатуры; шаги старта перечисляет бриф SessionStart.
# Латиницей: Windows PowerShell 5.1 читает .ps1 без BOM в кодовой странице ANSI, кириллица в строке дойдёт до claude битой.
$LEAD_PROMPT     = 'SAM stand was started by start_sam.ps1, you are the SAM lead agent. Do the start steps from the SessionStart brief, then listen to the conversation.'

function Read-DotEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path $Path)) { return $values }
    foreach ($line in (Get-Content $Path -Encoding UTF8)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $name, $raw = $Matches[1], $Matches[2]
            # Без кавычек « # ...» - комментарий, как у python-dotenv
            if ($raw -notmatch '^["'']') { $raw = ($raw -replace '\s+#.*$', '') }
            $values[$name] = $raw.Trim('"', "'")
        }
    }
    return $values
}

function Get-SamSetting([string]$Name, [string]$Default, [hashtable]$DotEnv) {
    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv) { return $fromEnv }
    if ($DotEnv -and $DotEnv[$Name]) { return $DotEnv[$Name] }
    return $Default
}

# Порт из настройки; не число - строка в лог и умолчание, как gateway/env.py env_int
function Get-SamPortSetting([string]$Name, [int]$Default, [hashtable]$DotEnv) {
    $raw = Get-SamSetting $Name "$Default" $DotEnv
    $port = 0
    if ([int]::TryParse("$raw".Trim(), [ref]$port) -and $port -gt 0) { return $port }
    Write-Host "[SAM] $Name=$raw - not a port number, using $Default" -ForegroundColor Red
    return $Default
}

function Get-LeadConfig([string]$Root) {
    $dotEnv = Read-DotEnv "$Root\.env"
    $bus = Get-SamSetting 'SAM_VOICE_BUS_DIR' "$Root\data\voice\bus" $dotEnv
    return [pscustomobject]@{
        Terminal = Get-SamSetting 'SAM_LEAD_TERMINAL' 'wt' $dotEnv               # аргументы вкладки - синтаксис wt
        Command  = Get-SamSetting 'SAM_LEAD_COMMAND' 'claude' $dotEnv
        Args     = Get-SamSetting 'SAM_LEAD_ARGS' '' $dotEnv                     # через пробел, например --model sonnet
        Prompt   = Get-SamSetting 'SAM_LEAD_PROMPT' $LEAD_PROMPT $dotEnv
        AliveS   = [double](Get-SamSetting 'SAM_LEAD_ALIVE_S' "$LEAD_ALIVE_S" $dotEnv)
        Alive    = Join-Path $bus $LEAD_ALIVE_NAME
        MainOrder = Get-SamSetting 'SAM_MAIN_MODELS' $MAIN_ORDER_DEFAULT $dotEnv   # порядок основных моделей, как в шлюзе
    }
}

# --- решение -----------------------------------------------------------------

function Test-LeadAlive($LastWrite, [datetime]$Now, [double]$AliveS) {
    return ($null -ne $LastWrite) -and (($Now - $LastWrite).TotalSeconds -lt $AliveS)
}

# Сессия Claude Code нужна, только если она первая в порядке основных моделей: иначе основная модель в шлюзе
function Test-LeadWanted([string]$MainOrder) {
    $first = ($MainOrder -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -First 1)
    return $first -eq $LEAD_MODEL_NAME
}

function Select-BridgeProcess($Processes) {
    # cmd.exe, который держит перенаправление в лог, несёт ту же строку - считаем только python
    return @($Processes | Where-Object { ($PYTHON_NAMES -contains $_.Name) -and ($_.CommandLine -like "*$BRIDGE_MODULE*") })
}

# --- внешний мир -------------------------------------------------------------

function Get-BridgeProcess {
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Select-Object ProcessId, Name, CommandLine
    return Select-BridgeProcess $all
}

function Get-LeadLastWrite([string]$AlivePath) {
    $item = Get-Item $AlivePath -ErrorAction SilentlyContinue
    if ($item) { return $item.LastWriteTime } else { return $null }
}

function Start-Bridge([string]$Root, [string]$Python) {
    $running = Get-BridgeProcess
    if ($running.Count -gt 0) {
        Write-Host "  Bridge already running (PID $($running[0].ProcessId))" -ForegroundColor Gray
        return
    }
    New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
    Start-Process cmd -WindowStyle Hidden -WorkingDirectory $Root `
        -ArgumentList "/c `"`"$Python`" -u -m $BRIDGE_MODULE >> $BRIDGE_LOG 2>&1`""
    Write-Host "  Bridge started, log $BRIDGE_LOG" -ForegroundColor Gray
}

# Вкладка запускает claude один раз, без перезапуска: закрытая сессия - решение человека.
# SAM_LEAD_SESSION=1 говорит SessionStart-хуку (.claude/hooks/lead_brief.py), что сессия - основной агент.
function New-LeadScript([string]$Root, $Config) {
    $tmp = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.ps1'
    $prompt = $Config.Prompt -replace "'", "''"
    $extra = ($Config.Args -split '\s+' | Where-Object { $_ } | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ' '
    Set-Content -Path $tmp -Encoding UTF8 -Value @"
Set-Location '$Root'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
`$Host.UI.RawUI.WindowTitle = '$LEAD_TAB_TITLE'
`$env:SAM_LEAD_SESSION = '1'
& '$($Config.Command)' $extra '$prompt'
"@
    return $tmp
}

function Start-LeadSession([string]$Root) {
    $cfg = Get-LeadConfig $Root
    if (-not (Test-LeadWanted $cfg.MainOrder)) {
        Write-Host "  Claude Code session not needed: main model is not claude-code (SAM_MAIN_MODELS=$($cfg.MainOrder))" -ForegroundColor Gray
        return
    }
    if (Test-LeadAlive (Get-LeadLastWrite $cfg.Alive) (Get-Date) $cfg.AliveS) {
        Write-Host "  Claude Code session already alive ($($cfg.Alive))" -ForegroundColor Gray
        return
    }
    if (-not (Get-Command $cfg.Command -ErrorAction SilentlyContinue)) {
        Write-Host "[SAM] Claude Code not started: $($cfg.Command) not found (SAM_LEAD_COMMAND)" -ForegroundColor Red
        return
    }
    $tmp = New-LeadScript $Root $cfg
    Start-Process $cfg.Terminal -ArgumentList "--title `"$LEAD_TAB_TITLE`" -d `"$Root`" -- powershell -NoExit -File `"$tmp`""
    Write-Host "  Claude Code session opened in $($cfg.Terminal)" -ForegroundColor Gray
}
