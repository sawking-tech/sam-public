# Чей процесс записан в .sam_pids (SAM-64). PID из файла мог достаться чужому процессу:
# 9 окт PID 2212 из файла от 21 мая принадлежал svchost, и -Stop сделал бы ему taskkill /F /T.
# SAM запускает wt -> WindowsTerminal -> powershell -> python; остальное не трогаем.

$SAM_PROCESS_NAMES = @('wt', 'WindowsTerminal', 'OpenConsole', 'powershell', 'pwsh', 'python', 'pythonw')

function Test-SamProcess($Process) {
    return ($null -ne $Process) -and ($SAM_PROCESS_NAMES -contains $Process.Name)
}

# Вкладка держит компонент (SAM-80): упал или убит - поднимается заново через $RestartPause с, пока нет
# флага остановки; с флагом выходит с кодом 0.
# Лог компонента (15 окт): вкладки с непрерывным выводом вешали Windows Terminal, поэтому по умолчанию компонент пишет
# в logs/<компонент>.log; лог больше порога при старте скрипта уходит в .1 - один предыдущий, без бесконечного роста
$COMPONENT_LOG_MAX_BYTES = [int64]$(if ($env:SAM_COMPONENT_LOG_MAX_MB) { $env:SAM_COMPONENT_LOG_MAX_MB } else { 20 }) * 1MB

function Get-ComponentLogName([string]$Title) {
    return (($Title -replace '^SAM:\s*', '') -replace '[^A-Za-z0-9_-]', '').ToLowerInvariant() + '.log'
}

function New-ComponentScript([string]$Root, [string]$Title, [string]$Command, [string]$StopFlag,
                             [int]$RestartPause = 3, [string]$LogFile = '') {
    $tmp = [System.IO.Path]::GetTempFileName() -replace '\.tmp$', '.ps1'
    # С логом: вывод и ошибки процесса - в файл UTF-8, Python без буфера, чтобы строка появлялась сразу
    $run = if ($LogFile) { "& { $Command } 2>&1 | Out-File -FilePath '$LogFile' -Append -Encoding utf8" } else { $Command }
    $say = if ($LogFile) { "Out-File -FilePath '$LogFile' -Append -Encoding utf8 -InputObject" } else { "Write-Host -ForegroundColor Yellow" }
    $rotate = if ($LogFile) { @"
    if ((Test-Path '$LogFile') -and (Get-Item '$LogFile').Length -gt $COMPONENT_LOG_MAX_BYTES) {
        Move-Item -Force '$LogFile' '$LogFile.1'
    }
"@ } else { '' }
    Set-Content -Path $tmp -Encoding UTF8 -Value @"
Set-Location '$Root'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
`$env:PYTHONUNBUFFERED = '1'
`$Host.UI.RawUI.WindowTitle = '$Title'
`$restarts = 0
$rotate
while (-not (Test-Path '$StopFlag')) {
    $run
    if (Test-Path '$StopFlag') { break }
    `$restarts++
    $say "[SAM] $Title stopped (exit `$LASTEXITCODE), restart #`$restarts in $RestartPause s"
    Start-Sleep $RestartPause
}
exit 0
"@
    return $tmp
}

# Компоненты SAM для остановки и проверки дублей (10 окт). PID в .sam_pids - лаунчер wt, он выходит сразу, а вкладки
# живут под общим WindowsTerminal: рестарт 01:50 оставил вкладки и процессы прошлого запуска, вкладка слуха
# поднимала второй слух. Поэтому компонент ищется по командной строке, вкладка - по своему скрипту.
# Надзор (scripts/stand_watch.py) - компонент без вкладки: его поднимают модули стенда (SAM-195), -Stop гасит.
# Не компоненты: тикер и живость (voice.layer.ticker, scripts/lead_heartbeat.py) держит сессия Claude Code;
# python -m voice.layer.vmic - подписчик для ручной проверки, его запускают руками.
$SAM_COMPONENTS = @(
    [pscustomobject]@{ Key = 'Gateway'; Title = 'SAM: Gateway'; Pattern = 'uvicorn\s+gateway\.main' }
    [pscustomobject]@{ Key = 'MCP';     Title = 'SAM: MCP';     Pattern = 'mcp_aggregator\.py' }
    [pscustomobject]@{ Key = 'Client';  Title = 'SAM: Client';  Pattern = 'client_agent\.py' }
    [pscustomobject]@{ Key = 'Voice';   Title = 'SAM: Voice';   Pattern = 'voice\.layer\.voice\b' }      # \b: не voice_bank
    [pscustomobject]@{ Key = 'Silero';  Title = 'SAM: Silero';  Pattern = 'voice\.layer\.tts_silero_server' }
    [pscustomobject]@{ Key = 'Cosy';    Title = 'SAM: Cosy';    Pattern = 'voice\.layer\.tts_cosy_server' }
    [pscustomobject]@{ Key = 'Embedder'; Title = 'SAM: Embedder'; Pattern = 'gateway\.embedder_service' }   # SAM-172
    [pscustomobject]@{ Key = 'Bridge';  Title = $null;          Pattern = 'voice\.layer\.bridge' }       # без вкладки, sam_lead.ps1
    [pscustomobject]@{ Key = 'Watch';   Title = $null;          Pattern = 'stand_watch\.py' }           # без вкладки, gateway/stand_watch_keeper.py
)
$SAM_PYTHON_EXE = @('python.exe', 'pythonw.exe')
$SAM_SHELL_EXE  = @('powershell.exe', 'pwsh.exe')
$TAB_FILE_RE    = '-File\s+"?([^"]+?\.ps1)"?(\s|$)'       # как Launch-WT в start_sam.ps1 пишет вкладку
$TAB_TITLE_RE   = "WindowTitle = '([^']+)'"                # строка New-ComponentScript

# --- решение -----------------------------------------------------------------

# Вкладка-надзиратель этого репозитория: её скрипт ждёт наш флаг остановки. Вкладка сессии Claude Code
# (sam_lead.ps1) флага не ждёт и сюда не попадает. Возвращает заголовок вкладки или $null.
function Get-ComponentTabTitle($Process, [string]$StopFlag, [scriptblock]$ReadText) {
    if ($SAM_SHELL_EXE -notcontains $Process.Name) { return $null }
    if ("$($Process.CommandLine)" -notmatch $TAB_FILE_RE) { return $null }
    $text = & $ReadText $Matches[1]
    if (-not $text -or $text.IndexOf("Test-Path '$StopFlag'", [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $null }
    if ($text -match $TAB_TITLE_RE) { return $Matches[1] }
    return $null
}

function Get-ComponentOfProcess($Process) {
    if ($SAM_PYTHON_EXE -notcontains $Process.Name) { return $null }
    foreach ($c in $SAM_COMPONENTS) {
        if ("$($Process.CommandLine)" -match $c.Pattern) { return $c }
    }
    return $null
}

# Что гасить: сначала вкладки (иначе цикл вкладки поднимет убитый процесс), потом процессы компонентов.
# $Keys - какие компоненты; пусто - все.
function Select-SamTargets($Processes, [string]$StopFlag, [scriptblock]$ReadText, [string[]]$Keys = @()) {
    $wanted = if ($Keys) { @($SAM_COMPONENTS | Where-Object { $Keys -contains $_.Key }) } else { $SAM_COMPONENTS }
    $wantedKeys = @($wanted | ForEach-Object { $_.Key })
    $tabs = foreach ($p in $Processes) {
        $title = Get-ComponentTabTitle $p $StopFlag $ReadText
        $c = $wanted | Where-Object { $_.Title -and $_.Title -eq $title } | Select-Object -First 1
        if ($c) { [pscustomobject]@{ ProcessId = $p.ProcessId; Kind = 'tab'; Component = $c.Key; CommandLine = $p.CommandLine } }
    }
    $procs = foreach ($p in $Processes) {
        $c = Get-ComponentOfProcess $p
        if ($c -and ($wantedKeys -contains $c.Key)) {
            [pscustomobject]@{ ProcessId = $p.ProcessId; Kind = 'process'; Component = $c.Key; CommandLine = $p.CommandLine }
        }
    }
    return @(@($tabs) + @($procs) | Where-Object { $_ })
}

# Компонент запущен больше одного раза - строки вида «Gateway: 2 processes (PID 1, 2)». Процесс, чей родитель -
# процесс того же компонента (лаунчер venv), не считается отдельно; client_agent поднимает свой mcp_aggregator,
# он дочерний и тоже не считается.
function Find-SamDuplicates($Processes, [string]$StopFlag, [scriptblock]$ReadText) {
    $targets = Select-SamTargets $Processes $StopFlag $ReadText
    $matched = @{}
    foreach ($t in ($targets | Where-Object Kind -eq 'process')) { $matched[[int]$t.ProcessId] = $true }
    $parent = @{}
    foreach ($p in $Processes) { $parent[[int]$p.ProcessId] = [int]$p.ParentProcessId }
    $roots = $targets | Where-Object { $_.Kind -eq 'tab' -or -not $matched.ContainsKey($parent[[int]$_.ProcessId]) }
    $lines = foreach ($g in ($roots | Group-Object Kind, Component | Where-Object { $_.Count -gt 1 })) {
        $first = $g.Group[0]
        $what = if ($first.Kind -eq 'tab') { 'tabs' } else { 'processes' }
        "$($first.Component): $($g.Count) $what (PID $(($g.Group.ProcessId) -join ', '))"
    }
    return @($lines)
}

# Глубина поиска компонента среди предков держателя порта: uvicorn --reload из venv - лаунчер venv, reloader,
# лаунчер spawn_main, spawn_main (держит порт)
$SAM_ANCESTOR_DEPTH = 4
$UNKNOWN_PROCESS    = '?'

# Свой ли процесс: он или предок до $SAM_ANCESTOR_DEPTH - компонент по командной строке. Процесса нет в таблице -
# не свой: защита не должна пускать под taskkill то, чего не видно (14 окт -Stop убил чужой mitmdump на 8000).
# Windows не обнуляет PID родителя: умер - номер достаётся другому; предок, созданный позже потомка, - не предок.
function Test-OwnProcess([int]$Id, [hashtable]$ById) {
    $seen = @{}
    $child = $null
    for ($depth = 0; $depth -le $SAM_ANCESTOR_DEPTH; $depth++) {
        $p = $ById[$Id]
        if (-not $p -or $seen.ContainsKey($Id)) { return $false }
        if ($child -and $p.CreationDate -and $child.CreationDate -and $p.CreationDate -gt $child.CreationDate) { return $false }
        if (Get-ComponentOfProcess $p) { return $true }
        $seen[$Id] = $true
        $child, $Id = $p, [int]$p.ParentProcessId
    }
    return $false
}

# Держатели портов стенда: Own - процессы SAM, Foreign - остальные (процесс из таблицы или заглушка с PID).
# Пустая таблица процессов - все чужие.
function Split-PortHolders([int[]]$HolderIds, $Processes) {
    $byId = @{}
    foreach ($p in $Processes) { $byId[[int]$p.ProcessId] = $p }
    $own, $foreign = @(), @()
    foreach ($id in ($HolderIds | Select-Object -Unique)) {
        $p = $byId[[int]$id]
        if (Test-OwnProcess $id $byId) { $own += $p; continue }
        $foreign += if ($p) { $p } else { [pscustomobject]@{ ProcessId = [int]$id; Name = $UNKNOWN_PROCESS; CommandLine = '' } }
    }
    return [pscustomobject]@{ Own = $own; Foreign = $foreign }
}

# Тот же ли это процесс, что в снимке: номер и время создания (номер умершего процесса достаётся другому)
function Test-SameProcess($Snapshot, $Live) {
    return ($null -ne $Snapshot) -and ($null -ne $Live) -and ([int]$Snapshot.ProcessId -eq [int]$Live.ProcessId) -and
        ($null -ne $Snapshot.CreationDate) -and ($Snapshot.CreationDate -eq $Live.CreationDate)
}

# Кого из держателей порта можно добить после остановки: свои сейчас и свои по снимку до остановки (тот же процесс).
# Есть держатель, который не свой ни так, ни так, - $null: ничего не убивать.
function Select-PortKillTargets($Split, $OwnSnapshot) {
    $targets = @($Split.Own | ForEach-Object { [int]$_.ProcessId })
    foreach ($f in $Split.Foreign) {
        $was = $OwnSnapshot | Where-Object { [int]$_.ProcessId -eq [int]$f.ProcessId } | Select-Object -First 1
        if (-not (Test-SameProcess $was $f)) { return $null }
        $targets += [int]$f.ProcessId
    }
    return ,$targets
}

# --- внешний мир -------------------------------------------------------------

$READ_TAB_SCRIPT = { param($Path) Get-Content -Raw -LiteralPath $Path -ErrorAction SilentlyContinue }

function Get-SamProcessTable {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId, Name, CommandLine, CreationDate)
}

# Гасит вкладки и процессы компонентов; -DryRun только перечисляет. Возвращает список целей.
function Stop-SamComponents([string]$StopFlag, [string[]]$Keys = @(), [switch]$DryRun) {
    $targets = Select-SamTargets (Get-SamProcessTable) $StopFlag $READ_TAB_SCRIPT $Keys
    foreach ($t in $targets) {
        $label = "$($t.Component) $($t.Kind) (PID $($t.ProcessId))"
        if ($DryRun) { Write-Host "  Would stop: $label  $($t.CommandLine)" -ForegroundColor Gray; continue }
        if (-not (Get-Process -Id $t.ProcessId -ErrorAction SilentlyContinue)) { continue }   # ушёл с деревом вкладки
        taskkill /F /T /PID $t.ProcessId 2>$null | Out-Null
        Write-Host "  Stopped: $label" -ForegroundColor Gray
    }
    return $targets
}

function Write-SamDuplicates([string]$StopFlag) {
    foreach ($line in (Find-SamDuplicates (Get-SamProcessTable) $StopFlag $READ_TAB_SCRIPT)) {
        Write-Host "[SAM] Duplicate component - ${line}; stop the extra one or rerun start_sam.ps1" -ForegroundColor Red
    }
}
