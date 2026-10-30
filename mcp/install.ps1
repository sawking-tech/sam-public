# SAM MCP Client Setup — Windows
# Запуск (от администратора): .\mcp\install.ps1
# Параметры: .\mcp\install.ps1 -Server http://$SAM_SERVER_IP:8000 -DeviceName workpc

param(
    [string]$Server    = $env:SAM_SERVER ?? "http://localhost:8000",
    [string]$DeviceName = $env:DEVICE_NAME ?? $env:COMPUTERNAME.ToLower(),
    [int]$McpPort      = 3000
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent

Write-Host "=== SAM MCP Client Setup ===" -ForegroundColor Cyan
Write-Host "Device : $DeviceName"
Write-Host "Server : $Server"
Write-Host "MCP    : port $McpPort"
Write-Host ""

# ── 1. Проверить Node.js ──────────────────────────────────────────────────────
try {
    $nodeVer = node --version 2>&1
    Write-Host "[OK] Node.js $nodeVer"
} catch {
    Write-Error "Node.js не найден. Установи с https://nodejs.org"
    exit 1
}

# ── 2. Установить npm пакеты ──────────────────────────────────────────────────
Write-Host "`n[NPM] Устанавливаю пакеты..."
npm install -g mcp-proxy @playwright/mcp @modelcontextprotocol/server-filesystem
if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed"; exit 1 }
Write-Host "[OK] npm пакеты установлены"

# ── 3. Включить Wake-on-LAN в Windows ────────────────────────────────────────
Write-Host "`n[WoL] Включаю Wake-on-LAN..."
try {
    $adapters = Get-NetAdapter | Where-Object { $_.Status -eq "Up" }
    foreach ($adapter in $adapters) {
        $pwr = Get-NetAdapterPowerManagement -Name $adapter.Name -ErrorAction SilentlyContinue
        if ($pwr) {
            Set-NetAdapterPowerManagement -Name $adapter.Name `
                -WakeOnMagicPacket Enabled `
                -ErrorAction SilentlyContinue
        }
    }
    Write-Host "[OK] WoL включён на сетевых адаптерах"
} catch {
    Write-Warning "Не удалось настроить WoL автоматически. Включи вручную в Device Manager."
}

# ── 4. Открыть порт 3000 в Firewall ──────────────────────────────────────────
Write-Host "`n[Firewall] Открываю порт $McpPort..."
try {
    $ruleName = "SAM-MCP-$McpPort"
    Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $McpPort `
        -Action Allow `
        -Profile Any | Out-Null
    Write-Host "[OK] Порт $McpPort открыт"
} catch {
    Write-Warning "Не удалось настроить Firewall. Открой порт $McpPort вручную."
}

# ── 5. Создать .env если нет ──────────────────────────────────────────────────
$envFile = "$RepoRoot\.env"
if (-not (Test-Path $envFile)) {
    Copy-Item "$RepoRoot\.env.example" $envFile
    Write-Host "[OK] .env создан из .env.example"
}

# Прописать переменные устройства в .env
$envContent = Get-Content $envFile -Raw
if ($envContent -notmatch "DEVICE_NAME=") {
    Add-Content $envFile "`nDEVICE_NAME=$DeviceName"
}
if ($envContent -notmatch "SAM_SERVER=") {
    Add-Content $envFile "`nSAM_SERVER=$Server"
}
if ($envContent -notmatch "MCP_PORT=") {
    Add-Content $envFile "`nMCP_PORT=$McpPort"
}

# ── 6. Создать задачу автозапуска (Task Scheduler) ────────────────────────────
Write-Host "`n[Task] Создаю задачу автозапуска..."
$taskName = "SAM-ClientAgent-$DeviceName"
$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source ?? "python"
$scriptPath = "$RepoRoot\mcp\client_agent.py"

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$scriptPath`" --server $Server --name $DeviceName" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # без лимита

try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -RunLevel Highest `
        -Force | Out-Null
    Write-Host "[OK] Задача '$taskName' создана (запуск при входе в систему)"
} catch {
    Write-Warning "Не удалось создать задачу. Запускай вручную: python mcp\client_agent.py"
}

# ── 7. Запустить прямо сейчас ─────────────────────────────────────────────────
Write-Host "`n[Start] Запускаю client_agent сейчас..."
Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "=== Готово! ===" -ForegroundColor Green
Write-Host "  Устройство '$DeviceName' будет регистрироваться на $Server при каждом входе в Windows"
Write-Host "  MCP endpoint: http://$(hostname):$McpPort"
Write-Host "  Проверка: curl http://localhost:8000/sam/catalog/devices/online"
Write-Host ""
Write-Host "Для второго ПК — запусти этот же скрипт с параметрами:"
Write-Host "  .\mcp\install.ps1 -Server http://$SAM_SERVER_IP:8000 -DeviceName pc2"
