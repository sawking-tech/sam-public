# Освободить место на C: убрать то, что человек разрешил удалить (SAM-107). По умолчанию только показывает, что уберёт;
# удаляет с ключом -Go. Запускает человек: powershell -File scripts\free_c_disk.ps1 -Go
# Список не вшит в код: правится в блоках ниже или передаётся параметрами.

param(
    [switch]$Go,
    # Файлы, отмеченные ненужными (фильмы в загрузках)
    [string[]]$Files = @(
        "$env:USERPROFILE\Documents\Zona Downloads\*Холодная гора*",
        "$env:USERPROFILE\Documents\Zona Downloads\*I, Robot*"
    ),
    # Папки моделей LM Studio, кроме джемм и квена: они нужны основной и быстрой моделям
    [string]$ModelsRoot = "$env:USERPROFILE\.lmstudio\models",
    [string[]]$ModelFolders = @(
        'lmstudio-community\GLM-4.7-Flash-GGUF',
        'lmstudio-community\GLM-4.6V-Flash-GGUF',
        'lmstudio-community\gpt-oss-20b-GGUF',
        'lmstudio-community\DeepSeek-R1-Distill-Qwen-14B-GGUF',
        'mradermacher',
        'G17c21ds',
        'second-state'
    )
)

function Get-SizeGb([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return 0 }
    if ($item.PSIsContainer) {
        return (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB
    }
    return $item.Length / 1GB
}

$targets = @()
foreach ($pattern in $Files) { $targets += Get-ChildItem -Path $pattern -File -Force -ErrorAction SilentlyContinue | ForEach-Object FullName }
foreach ($folder in $ModelFolders) {
    $path = Join-Path $ModelsRoot $folder
    if (Test-Path -LiteralPath $path) { $targets += $path }
}

$total = 0
foreach ($t in $targets) {
    $gb = Get-SizeGb $t
    $total += $gb
    '{0,7:N1} GB  {1}' -f $gb, $t
}
'{0,7:N1} GB  всего' -f $total
'свободно на C: {0:N1} GB' -f ((Get-PSDrive C).Free / 1GB)

if (-not $Go) { 'Это только показ. Чтобы удалить: powershell -File scripts\free_c_disk.ps1 -Go'; return }
foreach ($t in $targets) { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction Continue }
'после чистки свободно на C: {0:N1} GB' -f ((Get-PSDrive C).Free / 1GB)
