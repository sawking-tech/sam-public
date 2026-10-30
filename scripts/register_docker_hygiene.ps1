# Регистрирует ежедневный запуск чистки Docker в планировщике Windows (SAM-107). Права администратора не нужны: задача на текущего
# пользователя. Пропущенный запуск (ПК был выключен) навёрстывается при включении. Снять: -Remove.
#   powershell -File scripts\register_docker_hygiene.ps1
#   powershell -File scripts\register_docker_hygiene.ps1 -At 05:15 -Remove

param(
    [string]$At = '04:40',                       # время суток по местному; до утра, когда Docker почти не нужен
    [string]$TaskName = 'SAM-DockerHygiene',
    [switch]$Remove
)

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    "задача $TaskName снята"
    return
}

$script = Join-Path $PSScriptRoot 'docker_hygiene.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Go"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'SAM-107: чистка Docker (образы, кэш сборки, остановленные контейнеры), данные не трогает' | Out-Null
"задача $TaskName зарегистрирована: ежедневно в $At, журнал logs\docker_hygiene.log"
(Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object NextRunTime | Format-List | Out-String).Trim()
