# Суточная чистка Docker (SAM-107): убирает неиспользуемое и старое, данные стенда не трогает.
# Запускается по расписанию, чтобы диск Docker не рос без присмотра.
#   powershell -File scripts\docker_hygiene.ps1        показать, что есть и сколько можно вернуть (ничего не удаляет)
#   powershell -File scripts\docker_hygiene.ps1 -Go    почистить
# Регистрация ежедневного запуска: scripts\register_docker_hygiene.ps1.
# Не трогает тома (в них Mongo и Qdrant), работающие контейнеры и образы, которые они используют.
# Сжатие самого файла диска Docker (docker_data.vhdx) требует прав администратора и остановки Docker: отдельный шаг -Compact.

param(
    [switch]$Go,
    [switch]$Compact,                          # только с правами администратора: останавливает Docker Desktop и сжимает vhdx
    [int]$KeepDays = 7,                        # образы и кэш сборки моложе этого срока остаются (откат без повторной загрузки)
    [string]$VhdxPath = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx",
    [string]$LogFile = "$PSScriptRoot\..\logs\docker_hygiene.log"
)

$UNTIL = "$($KeepDays * 24)h"                   # аргумент --filter until=...

function Write-Log([string]$Message) {
    $line = '[{0:yyyy-MM-dd HH:mm:ss}] {1}' -f (Get-Date), $Message
    $line
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Get-FreeGb { '{0:N1}' -f ((Get-PSDrive C).Free / 1GB) }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Log 'docker не найден в PATH: запусти Docker Desktop или добавь docker в PATH'
    exit 1
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Write-Log 'Docker не отвечает: запусти Docker Desktop и повтори'; exit 1 }

Write-Log "до: свободно на C: $(Get-FreeGb) GB"
docker system df
if (-not $Go) { Write-Log "Это только показ. Чтобы почистить (образы без контейнеров старше $KeepDays дн., кэш сборки, остановленные контейнеры): -Go"; exit 0 }

Write-Log "чищу: контейнеры, образы и кэш сборки старше $KeepDays дн.; тома не трогаю"
docker container prune -f --filter "until=$UNTIL" | ForEach-Object { Write-Log $_ }
docker image prune -a -f --filter "until=$UNTIL" | ForEach-Object { Write-Log $_ }
docker builder prune -a -f --filter "until=$UNTIL" | ForEach-Object { Write-Log $_ }
docker system df
Write-Log "после чистки внутри Docker: свободно на C: $(Get-FreeGb) GB (файл диска Docker сам не уменьшается)"

if ($Compact) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')
    if (-not $admin) { Write-Log 'сжатие vhdx требует прав администратора: запусти PowerShell от администратора'; exit 1 }
    Write-Log 'останавливаю Docker Desktop и сжимаю vhdx'
    Stop-Process -Name 'Docker Desktop' -Force -ErrorAction SilentlyContinue
    wsl --shutdown
    $script = "select vdisk file=`"$VhdxPath`"`nattach vdisk readonly`ncompact vdisk`ndetach vdisk`n"
    $tmp = New-TemporaryFile
    Set-Content -Path $tmp -Value $script -Encoding ascii
    diskpart /s $tmp | ForEach-Object { Write-Log $_ }
    Remove-Item $tmp -Force
    Write-Log "после сжатия: свободно на C: $(Get-FreeGb) GB; Docker Desktop запусти заново"
}
