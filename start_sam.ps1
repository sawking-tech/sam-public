# SAM Quick Start
# Запуск всего:  .\start_sam.ps1
# Остановка:     .\start_sam.ps1 -Stop
# Только голос:  .\start_sam.ps1 -Voice   (голосовой слой: слух и озвучка)
# CosyVoice:     .\start_sam.ps1 -Cosy    (вместе со всем или один; Silero поднимается всегда)
# Только gateway:.\start_sam.ps1 -Gateway
# Только MCP:    .\start_sam.ps1 -Mcp
# Только client: .\start_sam.ps1 -Client
# Только Silero: .\start_sam.ps1 -Tts;  эмбеддер: -Embedder;  мост: -Bridge  (перезапуск компонента, SAM-195)
# Без Claude Code: .\start_sam.ps1 -NoLead  (мост поднимается, вкладка сессии - нет)
# Надзор (scripts/stand_watch.py) поднимают полный старт и модули стенда, -Stop гасит (SAM-195)

param(
    [switch]$Stop,
    [switch]$Voice,
    [switch]$Gateway,
    [switch]$Mcp,
    [switch]$Client,
    [switch]$Cosy,
    [switch]$Tts,
    [switch]$Embedder,
    [switch]$Bridge,
    [switch]$NoLead
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

$ROOT           = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }
$PIDFILE        = "$ROOT\.sam_pids"
$STOPFLAG       = "$ROOT\.sam_stop"     # пока лежит, вкладки не поднимают упавший компонент (SAM-80)
$RESTART_PAUSE  = 3                     # пауза перед подъёмом упавшего компонента, с
$COMPOSE_DEV    = "$ROOT\docker\docker-compose.dev.yml"
$COMPOSE_CLIENT = "$ROOT\docker\docker-compose.client.yml"
$SANDBOX_OWN_IMAGE = "sam-sandbox:2"     # gateway/sandbox.py, DEFAULT_IMAGE: собирается из Dockerfile.sandbox
$SANDBOX_IMAGE  = if ($env:SAM_SANDBOX_IMAGE) { $env:SAM_SANDBOX_IMAGE } else { $SANDBOX_OWN_IMAGE }
$SANDBOX_DOCKERFILE = "$ROOT\docker\Dockerfile.sandbox"
# Страж сети песочницы (gateway/sandbox_net.py, ADR-016): правила DOCKER-USER для network=internet
$NETGUARD_OWN_IMAGE = "sam-netguard:1"    # gateway/sandbox_net.py, DEFAULT_GUARD_IMAGE: собирается из Dockerfile.netguard
$NETGUARD_IMAGE = if ($env:SAM_SANDBOX_GUARD_IMAGE) { $env:SAM_SANDBOX_GUARD_IMAGE } else { $NETGUARD_OWN_IMAGE }
$NETGUARD_DOCKERFILE = "$ROOT\docker\Dockerfile.netguard"
# Порты синтеза и эмбеддера - после подключения scripts/sam_lead.ps1, как у серверов: окружение, .env, умолчание
$SILERO_PORT_DEFAULT   = 10202          # voice/layer/tts_silero_server.py, SILERO_PORT
$COSY_PORT_DEFAULT     = 10203          # voice/layer/tts_cosy_server.py, COSY_PORT
$EMBEDDER_PORT_DEFAULT = 10204          # gateway/embedder_service.py, SAM_EMBEDDER_PORT (SAM-172)
$EMBEDDER_WAIT_S = 120                  # холодная загрузка модели с torch - 28,8 с (12 окт), с запасом
# CosyVoice работает в venv лаборатории (torch, cosyvoice); путь - COSY_PYTHON
$COSY_PYTHON    = if ($env:COSY_PYTHON) { $env:COSY_PYTHON } else { "python" }
$COSY_WAIT_S    = 180                   # модель, слепки голосов, графы и прогрев; старт не замерен, с запасом
$WT_WINDOW      = if ($env:SAM_WT_WINDOW) { $env:SAM_WT_WINDOW } else { "SAM" }   # имя окна wt для всех вкладок стенда
$GATEWAY_PORT   = 8000
$MCP_PORT       = 3000
$SAM_PORTS      = @($GATEWAY_PORT, $MCP_PORT)    # порты стенда: держит их только SAM, чужой процесс не гасится
$STAND_WATCH_HEALTH = "stand_watch.health"   # gateway/stand_watch_keeper.py HEALTH_NAME, папка шины
$DUP_CHECK_S    = 5                     # вкладка wt поднимает python за 1-2 с (10 окт, CreationDate); проверка дублей после
$LOG_DIR        = "$ROOT\logs"

# ─── Functions ────────────────────────────────────────────────────────────────

. "$ROOT\scripts\sam_process.ps1"
. "$ROOT\scripts\sam_lead.ps1"

$DOT_ENV       = Read-DotEnv "$ROOT\.env"
# Компоненты - скрытыми процессами с логом в logs/<компонент>.log (15 окт: шесть вкладок с непрерывным выводом вешали
# Windows Terminal вместе с сессией Claude Code); SAM_COMPONENT_TABS=1 - по-старому, вкладками wt
$COMPONENT_TABS = $(if ($env:SAM_COMPONENT_TABS) { $env:SAM_COMPONENT_TABS } else { $DOT_ENV["SAM_COMPONENT_TABS"] }) -eq '1'
# Порт передаётся сервису явно (--port): эмбеддер .env не читает, CosyVoice - без python-dotenv в своём venv
$SILERO_PORT   = Get-SamPortSetting "SILERO_PORT" $SILERO_PORT_DEFAULT $DOT_ENV
$COSY_PORT     = Get-SamPortSetting "COSY_PORT" $COSY_PORT_DEFAULT $DOT_ENV
$EMBEDDER_PORT = Get-SamPortSetting "SAM_EMBEDDER_PORT" $EMBEDDER_PORT_DEFAULT $DOT_ENV
# Все порты стенда: снимок держателей в Stop-SAM и ожидание при рестарте (умирающий Silero не сойдёт за новый)
$STAND_PORTS   = @($SAM_PORTS) + @($SILERO_PORT, $COSY_PORT, $EMBEDDER_PORT)
# Порты, которые поднимает этот запуск: чужой на них - стоп до старта; чужой на 10203 без -Cosy не мешает
$START_PORTS   = @($SAM_PORTS) + @($SILERO_PORT, $EMBEDDER_PORT) + @($(if ($Cosy) { $COSY_PORT }) | Where-Object { $_ })

function Assert-Command([string]$cmd) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "[SAM] Not found: $cmd" -ForegroundColor Red
        exit 1
    }
}

function Stop-SAM {
    Write-Host "[SAM] Stopping..." -ForegroundColor Yellow
    New-Item -ItemType File -Force -Path $STOPFLAG | Out-Null   # вкладки не поднимают убитое
    # Держатели портов - до остановки: дочерний процесс, чей родитель-компонент уже убит, остаётся своим
    $before = @(Get-PortSplit $STAND_PORTS)
    $script:OwnPortHolders = @($before | ForEach-Object { $_.Own })
    # Вкладки и процессы компонентов по командной строке, мост и надзор тоже; сессию Claude Code и тикер не трогаем
    Stop-SamComponents $STOPFLAG | Out-Null
    # Файл живости убитого надзора свеж ещё 10 с: рестарт в это окно счёл бы надзор живым и не поднял его
    Remove-Item (Join-Path (Split-Path -Parent (Get-LeadConfig $ROOT).Alive) $STAND_WATCH_HEALTH) -ErrorAction SilentlyContinue
    if (Test-Path $PIDFILE) {
        Get-Content $PIDFILE | ForEach-Object {
            $pid_ = [int]$_
            $p = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
            if ($p -and -not (Test-SamProcess $p)) {
                Write-Host "  Skipped: PID $pid_ is $($p.Name), not SAM" -ForegroundColor Yellow
            } elseif ($p) {
                # taskkill /T убивает всё дерево (wt → powershell → python)
                taskkill /F /T /PID $pid_ 2>$null | Out-Null
                Write-Host "  Stopped: $($p.Name) (PID $pid_)" -ForegroundColor Gray
            }
        }
        Remove-Item $PIDFILE -ErrorAction SilentlyContinue
    }
    $live = Get-SamProcessTable
    foreach ($s in $before) {
        Write-ForeignHolders $s "left running" | Out-Null
        foreach ($own in $s.Own) {
            $now = $live | Where-Object { [int]$_.ProcessId -eq [int]$own.ProcessId } | Select-Object -First 1
            if (Test-SameProcess $own $now) {   # номер умершего процесса мог достаться другому
                taskkill /F /T /PID $own.ProcessId 2>$null | Out-Null
                Write-Host "  Freed port $($s.Port): $($own.Name) (PID $($own.ProcessId))" -ForegroundColor Gray
            }
        }
    }
    docker compose -f $COMPOSE_DEV    down --remove-orphans 2>$null
    docker compose -f $COMPOSE_CLIENT down --remove-orphans 2>$null
    # Старые orphan-контейнеры (до добавления name: в compose)
    # piper - контейнер прежнего синтеза
    foreach ($c in @("whisper","piper","speaker-id")) {
        docker rm -f $c 2>$null | Out-Null
    }
    Write-Host "[SAM] Stopped." -ForegroundColor Green
}

# Держит порт только тот, кто его слушает (Listen). TIME_WAIT (PID 0) и FIN_WAIT_2 умершего процесса живут
# после закрытия сокета до 2 мин и порт не держат: новый процесс его слушать может (10 окт рестарт падал на TIME_WAIT,
# 15 окт - на FIN_WAIT_2 только что погашенного шлюза, чей PID уже не существует).
function Get-PortHolders([int]$port) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -ne 0 }
}

$script:OwnPortHolders = @()   # процессы SAM на портах (номер и время создания), снятые Stop-SAM до остановки

# Держатели портов по одному снимку таблицы процессов: Port, Own, Foreign (Split-PortHolders, scripts/sam_process.ps1).
# Таблица не прочиталась - все держатели чужие: под taskkill не идёт то, чего не видно.
function Get-PortSplit([int[]]$Ports) {
    $table = Get-SamProcessTable
    foreach ($port in $Ports) {
        $ids = @(Get-PortHolders $port | ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
        $split = Split-PortHolders $ids $table
        [pscustomobject]@{ Port = $port; Own = @($split.Own); Foreign = @($split.Foreign) }
    }
}

# Одна строка на чужой процесс: чей порт и что с ним; $true - чужой есть
function Write-ForeignHolders($Split, [string]$what) {
    foreach ($p in $Split.Foreign) {
        $cmd = if ($p.CommandLine) { $p.CommandLine } else {
            "command line unavailable - stop PID $($p.ProcessId) by hand or rerun start_sam.ps1 elevated" }
        Write-Host "[SAM] Port $($Split.Port) is held by $($p.Name) (PID $($p.ProcessId)), not SAM - ${what}: $cmd" -ForegroundColor Red
    }
    return [bool]$Split.Foreign
}

# Чужой процесс на нужном порту - строка на каждый и выход с кодом 1, до запуска чего-либо
function Assert-NoForeignHolders([int[]]$Ports) {
    if (-not $Ports) { return }
    $busy = $false
    foreach ($s in @(Get-PortSplit $Ports)) {
        if (Write-ForeignHolders $s "SAM not started") { $busy = $true }
    }
    if ($busy) { exit 1 }
}

function Test-SAMRunning {
    if (Test-Path $PIDFILE) {
        foreach ($line in (Get-Content $PIDFILE)) {
            if (Test-SamProcess (Get-Process -Id ([int]$line) -ErrorAction SilentlyContinue)) {
                return $true
            }
        }
    }
    foreach ($s in @(Get-PortSplit $SAM_PORTS)) {
        if ($s.Own) { return $true }
    }
    return $false
}

function Wait-PortFree([int]$port, [int]$TimeoutSec = 15) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $s = Get-PortSplit @($port)
        if (-not $s.Own -and -not $s.Foreign) { return $true }
        # Добиваем свои процессы из того же снимка (дочерние node/python могут пережить taskkill /T); чужой - стоп
        $targets = Select-PortKillTargets $s $script:OwnPortHolders
        if ($null -eq $targets) {
            Write-ForeignHolders $s "restart aborted" | Out-Null
            return $false
        }
        $targets | ForEach-Object { taskkill /F /T /PID $_ 2>$null | Out-Null }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "[SAM] Port $port still busy after ${TimeoutSec}s" -ForegroundColor Red
    return $false
}

function Wait-ForService([string]$Url, [string]$Name, [int]$TimeoutSec = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    Write-Host "  Waiting $Name " -NoNewline
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                $body = $r.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
                if (-not $body -or $body.status -eq "ok") {
                    Write-Host " OK" -ForegroundColor Green
                    return $true
                }
            }
        } catch {}
        Write-Host "." -NoNewline
        Start-Sleep 1
    }
    Write-Host " TIMEOUT" -ForegroundColor Red
    return $false
}

function Wait-WyomingService([string]$Hostname, [int]$Port, [string]$Name, [int]$TimeoutSec = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    Write-Host "  Waiting $Name " -NoNewline
    while ((Get-Date) -lt $deadline) {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect($Hostname, $Port)
            $tcp.Close()
            Write-Host " OK" -ForegroundColor Green
            return $true
        } catch {}
        Write-Host "." -NoNewline
        Start-Sleep 1
    }
    Write-Host " TIMEOUT" -ForegroundColor Red
    return $false
}

function Wait-MongoDB([int]$TimeoutSec = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    Write-Host "  Waiting MongoDB " -NoNewline
    while ((Get-Date) -lt $deadline) {
        docker exec sam-mongo mongosh --eval "db.runCommand({ping:1})" --quiet 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host " OK" -ForegroundColor Green
            return $true
        }
        Write-Host "." -NoNewline
        Start-Sleep 1
    }
    Write-Host " TIMEOUT" -ForegroundColor Red
    return $false
}

$_WT_QUEUE  = [System.Collections.Generic.List[string]]::new()
$_WT_PID    = $null

function Queue-Component([string]$Title, [string]$Command) {
    $log = if ($COMPONENT_TABS) { '' } else { Join-Path $LOG_DIR (Get-ComponentLogName $Title) }
    $tmp = New-ComponentScript $ROOT $Title $Command $STOPFLAG $RESTART_PAUSE $log
    $script:_WT_QUEUE.Add($tmp) | Out-Null
}

# Очередь компонентов: скрытыми процессами (по умолчанию) или вкладками одного окна wt. Возвращает PID для .sam_pids:
# скрытые - их powershell, вкладки - лаунчер wt
function Launch-WT([string]$FirstTitle) {
    if (-not $COMPONENT_TABS) { return Start-HiddenComponents }
    $files = $script:_WT_QUEUE
    # wt требует одну строку аргументов — массив ломает ; и кавычки
    # -w: все вызовы - вкладками одного окна; без него Silero и MCP (их ждут по одному до старта остальных)
    # открывались отдельными окнами
    $arg = "-w $WT_WINDOW --title `"$FirstTitle`" -- powershell -NoExit -File `"$($files[0])`""
    for ($i = 1; $i -lt $files.Count; $i++) {
        $arg += " `; new-tab -- powershell -NoExit -File `"$($files[$i])`""
    }
    $proc = Start-Process wt -ArgumentList $arg -PassThru
    $script:_WT_PID = $proc.Id
    $script:_WT_QUEUE.Clear()
    return $proc.Id
}

function Start-HiddenComponents {
    New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
    $ids = foreach ($f in $script:_WT_QUEUE) {
        (Start-Process powershell -WindowStyle Hidden -PassThru `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$f`"").Id
    }
    $script:_WT_QUEUE.Clear()
    return @($ids)
}

# ─── Single component ─────────────────────────────────────────────────────────

if ($Voice -or $Gateway -or $Mcp -or $Client -or $Tts -or $Embedder -or $Bridge -or ($Cosy -and $PSBoundParameters.Count -eq 1)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { Assert-Command "python" }
    $PYTHON = $cmd.Source
    $ROOT = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }
    Remove-Item $STOPFLAG -ErrorAction SilentlyContinue
    # Порт компонента держит чужой процесс - ничего не запускаем и не гасим
    $ports = @()
    if ($Gateway) { $ports += $GATEWAY_PORT }
    if ($Mcp)     { $ports += $MCP_PORT }
    if ($Cosy)    { $ports += $COSY_PORT }
    if ($Tts)     { $ports += $SILERO_PORT }
    if ($Embedder) { $ports += $EMBEDDER_PORT }
    Assert-NoForeignHolders $ports
    # Старая вкладка и процесс того же компонента гаснут до запуска нового: иначе два процесса (9 окт 23:12)
    $keys = @{ Voice = 'Voice'; Cosy = 'Cosy'; Gateway = 'Gateway'; Mcp = 'MCP'; Client = 'Client'; Tts = 'Silero'
               Embedder = 'Embedder'; Bridge = 'Bridge' }
    Stop-SamComponents $STOPFLAG -Keys @($keys.Keys | Where-Object { (Get-Variable $_).Value } | ForEach-Object { $keys[$_] }) | Out-Null
    if ($Voice)   { Queue-Component "SAM: Voice"   "$PYTHON -m voice.layer.voice" }
    if ($Cosy)    { Queue-Component "SAM: Cosy"    "$COSY_PYTHON -m voice.layer.tts_cosy_server --port $COSY_PORT" }
    if ($Gateway) { Queue-Component "SAM: Gateway" "$PYTHON -m uvicorn gateway.main:app --host 0.0.0.0 --port $GATEWAY_PORT" }
    if ($Mcp)     { Queue-Component "SAM: MCP"     "$PYTHON mcp/mcp_aggregator.py --port $MCP_PORT" }
    if ($Client)  { Queue-Component "SAM: Client"  "$PYTHON mcp/client_agent.py" }
    if ($Tts)     { Queue-Component "SAM: Silero"  "$PYTHON -m voice.layer.tts_silero_server --port $SILERO_PORT" }
    if ($Embedder) { Queue-Component "SAM: Embedder" "$PYTHON -m gateway.embedder_service --port $EMBEDDER_PORT" }
    if ($Bridge)  { Start-Bridge $ROOT $PYTHON }   # мост без вкладки: скрытый процесс, лог logs/bridge.log
    $first = @("Voice","Gateway","Mcp","Client","Cosy","Tts","Embedder") | Where-Object { (Get-Variable $_).Value } | Select-Object -First 1
    if ($first) { Launch-WT "SAM: $first" | Out-Null }
    Start-Sleep $DUP_CHECK_S
    Write-SamDuplicates $STOPFLAG
    exit
}

# ─── Stop ─────────────────────────────────────────────────────────────────────

if ($Stop) {
    Stop-SAM
    exit
}

# ─── Auto-restart if already running ─────────────────────────────────────────

# Порт стенда держит чужой процесс - до остановки работающего стенда: иначе он ляжет, а рестарт оборвётся.
# Порты синтеза и эмбеддера тоже: Wait-WyomingService и Wait-ForService засчитали бы чужой слушатель за свой сервис
Assert-NoForeignHolders $START_PORTS

if (Test-SAMRunning) {
    Write-Host "[SAM] Already running - restarting..." -ForegroundColor Yellow
    Stop-SAM
    Write-Host "[SAM] Waiting for ports to free..." -ForegroundColor Gray
    $ok = $true
    foreach ($port in $START_PORTS) { if ($ok) { $ok = Wait-PortFree $port } }
    if (-not $ok) {
        Write-Host "[SAM] Could not free ports. Aborting." -ForegroundColor Red
        exit 1
    }
    Write-Host ""
}

# ─── Check deps ───────────────────────────────────────────────────────────────

Assert-Command "docker"
Assert-Command "python"
$PYTHON = (Get-Command python -ErrorAction Stop).Source

# ─── Start ────────────────────────────────────────────────────────────────────

Write-Host "[SAM] Starting all components..." -ForegroundColor Cyan
Remove-Item $STOPFLAG -ErrorAction SilentlyContinue
$pids = @()

# 1. MongoDB + Qdrant + n8n
Write-Host "[1/6] MongoDB + Qdrant + n8n..." -ForegroundColor Yellow
docker compose -f $COMPOSE_DEV up -d --remove-orphans
if ($LASTEXITCODE -ne 0) {
    Write-Host "[SAM] docker-compose.dev failed. Aborting." -ForegroundColor Red
    exit 1
}
if (-not (Wait-MongoDB 30)) {
    Write-Host "[SAM] MongoDB not ready. Aborting." -ForegroundColor Red
    exit 1
}
# Образ песочницы (SAM-83): нет - собрать; не собрался - стенд работает, sandbox_run ответит строкой о сборке
docker image inspect $SANDBOX_IMAGE *> $null
if ($LASTEXITCODE -ne 0 -and $SANDBOX_IMAGE -eq $SANDBOX_OWN_IMAGE) {
    Write-Host "[SAM] Sandbox image $SANDBOX_IMAGE missing, building..." -ForegroundColor Yellow
    docker build -f $SANDBOX_DOCKERFILE -t $SANDBOX_IMAGE "$ROOT\docker"
    if ($LASTEXITCODE -ne 0) { Write-Host "[SAM] Sandbox image build failed: sandbox_run unavailable" -ForegroundColor Red }
}
# Без стража песочница работает, но network=internet ответит отказом со строкой о сборке
docker image inspect $NETGUARD_IMAGE *> $null
if ($LASTEXITCODE -ne 0 -and $NETGUARD_IMAGE -eq $NETGUARD_OWN_IMAGE) {
    Write-Host "[SAM] Sandbox network guard $NETGUARD_IMAGE missing, building..." -ForegroundColor Yellow
    docker build -f $NETGUARD_DOCKERFILE -t $NETGUARD_IMAGE "$ROOT\docker"
    if ($LASTEXITCODE -ne 0) { Write-Host "[SAM] Network guard build failed: sandbox network unavailable" -ForegroundColor Red }
}

# 2. Синтез речи: Silero всегда, CosyVoice по -Cosy (без venv лаборатории - голос идёт через Silero)
Write-Host "[2/6] TTS: Silero$(if ($Cosy) { ' + CosyVoice' })..." -ForegroundColor Yellow
Queue-Component "SAM: Silero" "$PYTHON -m voice.layer.tts_silero_server --port $SILERO_PORT"
if ($Cosy) {
    if (Test-Path $COSY_PYTHON) { Queue-Component "SAM: Cosy" "$COSY_PYTHON -m voice.layer.tts_cosy_server --port $COSY_PORT" }
    else { Write-Host "[SAM] CosyVoice не поднят: нет $COSY_PYTHON (COSY_PYTHON); голос пойдёт через Silero" -ForegroundColor Red }
}
Queue-Component "SAM: Embedder" "$PYTHON -m gateway.embedder_service --port $EMBEDDER_PORT"   # SAM-172: рестарт шлюза модель не грузит
$pids += Launch-WT "SAM: Silero"
if (-not (Wait-WyomingService "127.0.0.1" $SILERO_PORT "Silero" 120)) {
    Write-Host "[SAM] Silero not ready. Aborting." -ForegroundColor Red
    Stop-SAM; exit 1
}
if (-not (Wait-ForService "http://127.0.0.1:$EMBEDDER_PORT/health" "Embedder" $EMBEDDER_WAIT_S)) {
    Write-Host "[SAM] эмбеддер не ответил за $EMBEDDER_WAIT_S с; шлюз загрузит модель сам" -ForegroundColor Red
}
if ($Cosy -and (Test-Path $COSY_PYTHON) -and -not (Wait-WyomingService "127.0.0.1" $COSY_PORT "CosyVoice" $COSY_WAIT_S)) {
    Write-Host "[SAM] CosyVoice не ответил за $COSY_WAIT_S с; голос пойдёт через Silero" -ForegroundColor Red
}

# 3. MCP aggregator — запускаем отдельно, ждём health
Write-Host "[3/6] MCP :$MCP_PORT..." -ForegroundColor Yellow
Queue-Component "SAM: MCP" "$PYTHON mcp/mcp_aggregator.py --port $MCP_PORT"
$pids += Launch-WT "SAM: MCP"
if (-not (Wait-ForService "http://127.0.0.1:$MCP_PORT/health" "MCP" 60)) {
    Write-Host "[SAM] MCP timeout. Aborting." -ForegroundColor Red
    Stop-SAM; exit 1
}

# 4+5. Client + Voice + Gateway — одним вызовом wt (три вкладки)
Write-Host "[4/6] Client agent + Voice layer..." -ForegroundColor Yellow
Write-Host "[5/6] Gateway :$GATEWAY_PORT..." -ForegroundColor Yellow
Queue-Component "SAM: Client"  "$PYTHON mcp/client_agent.py"
Queue-Component "SAM: Voice"   "$PYTHON -m voice.layer.voice"
Queue-Component "SAM: Gateway" "$PYTHON -m uvicorn gateway.main:app --host 0.0.0.0 --port $GATEWAY_PORT"
$pids += Launch-WT "SAM: Client"

$pids | Set-Content $PIDFILE

# 6. Мост голоса к шлюзу и сессия Claude Code - основная модель (SAM-91); мост переподключается сам, шлюз не ждём
Write-Host "[6/6] Bridge$(if (-not $NoLead) { ' + Claude Code session' })..." -ForegroundColor Yellow
Start-Bridge $ROOT $PYTHON
# Надзор - той же проверкой, что у модулей (SAM-195): скрытый процесс, отвязанный от этого скрипта, лог logs/stand_watch.log
Push-Location $ROOT
& $PYTHON -m gateway.stand_watch_keeper
Pop-Location
if (-not $NoLead) { Start-LeadSession $ROOT }

Write-Host ""
Write-Host "[SAM] All systems go!" -ForegroundColor Green
Write-Host "  Gateway : http://localhost:$GATEWAY_PORT/health  (starts in a separate window)"
Write-Host "  Monitor : http://localhost:$GATEWAY_PORT/monitor"
Write-Host "  n8n     : http://localhost:5678"
Write-Host "  TTS     : Silero tcp://127.0.0.1:$SILERO_PORT$(if ($Cosy) { ", CosyVoice tcp://127.0.0.1:$COSY_PORT" })"
Write-Host "  STT     : faster-whisper local (bzikst/faster-whisper-large-v3-russian)"
Write-Host ""
Write-Host "Stop:    .\start_sam.ps1 -Stop" -ForegroundColor Gray
Write-Host "Restart: .\start_sam.ps1"       -ForegroundColor Gray
Start-Sleep $DUP_CHECK_S
Write-SamDuplicates $STOPFLAG
Start-Process "http://localhost:$GATEWAY_PORT/monitor"