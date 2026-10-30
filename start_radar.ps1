# Радар присутствия отдельным процессом (SAM-105), без остального стенда SAM.
# Запуск: powershell -File start_radar.ps1   Остановка: powershell -File start_radar.ps1 -Stop
# Порт и хост - SAM_RADAR_PORT и SAM_RADAR_HOST в .env (по умолчанию 8010).
param([switch]$Stop)

$ROOT = $PSScriptRoot
$PYTHON = (Get-Command python).Source
$MODULE = "radar.presence_service"
# Прежнее имя модуля до переноса в radar/ (SAM-198): первый перезапуск после обновления гасит и процесс со старым
# именем, иначе новый не займёт порт, а старый останется со старым кодом
$OLD_MODULE = "gateway.presence_service"
$LOG = "logs\radar.log"

function Get-RadarProcess {
    Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and
        ($_.CommandLine -match "-m $([regex]::Escape($MODULE))" -or $_.CommandLine -match "-m $([regex]::Escape($OLD_MODULE))") }
}

$running = @(Get-RadarProcess)
if ($Stop -or $running.Count -gt 0) {
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    if ($Stop) { Write-Host "Radar stopped" -ForegroundColor Gray; return }
}
New-Item -ItemType Directory -Force -Path "$ROOT\logs" | Out-Null
Start-Process cmd -WindowStyle Hidden -WorkingDirectory $ROOT -ArgumentList "/c `"`"$PYTHON`" -u -m $MODULE >> $LOG 2>&1`""
Write-Host "Radar started, log $LOG" -ForegroundColor Green
