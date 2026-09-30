#Requires -Version 5.1
<#
  Установка приложения «Перевод книг» для Windows (из папки проекта, Docker Desktop должен быть запущен):
    powershell -ExecutionPolicy Bypass -File app\install.ps1
  1. Скачивает образы: Ollama, bilingual_book_maker, Docling (≈ 6 ГБ загрузки, ≈ 17 ГБ на диске).
  2. Собирает образ интерфейса (book_translate/pdf-epub).
  3. Скачивает модели rosetta-ru и translategemma-gpu (≈ 6 ГБ).
  4. Создаёт ярлыки «Перевод книг» на рабочем столе и в меню «Пуск».
  Всё, что нужно для EPUB и PDF → EPUB, скачивается здесь, а не при первом переводе. PDF → PDF (pdf2zh) —
  отдельно, см. README. Повторный запуск докачивает недостающее и пересоздаёт ярлыки.

  Только ярлыки:   ... app\install.ps1 -ShortcutsOnly
  Удалить ярлыки:  ... app\install.ps1 -Remove
#>
param([switch]$Remove, [switch]$ShortcutsOnly)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $PSScriptRoot 'BookTranslate.ps1'
$Icon = Join-Path $PSScriptRoot 'icon.ico'
$Name = 'Перевод книг.lnk'
$Places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Microsoft\Windows\Start Menu\Programs')
)

# docker.exe без вывода; true, если код 0. В PowerShell 5.1 перенаправленный stderr внешней программы
# при ErrorActionPreference=Stop превращается в исключение — поэтому здесь Continue.
function Test-Docker([string[]]$DockerArgs) {
    $ErrorActionPreference = 'Continue'
    & docker.exe @DockerArgs *> $null
    return ($LASTEXITCODE -eq 0)
}

function Step([string]$text, [string[]]$dockerArgs) {
    Write-Host ''
    Write-Host "== $text" -ForegroundColor Cyan
    Push-Location $Root
    try { & docker.exe @dockerArgs; $rc = $LASTEXITCODE } finally { Pop-Location }
    if ($rc -ne 0) {
        Write-Host "Ошибка (docker, код $rc). Проверьте интернет и Docker Desktop и запустите установку ещё раз:" -ForegroundColor Red
        Write-Host '  powershell -ExecutionPolicy Bypass -File app\install.ps1' -ForegroundColor Red
        exit 1
    }
}

if (-not $Remove -and -not $ShortcutsOnly) {
    if (-not (Test-Docker @('info', '--format', '{{.ServerVersion}}'))) {
        Write-Host 'Docker не отвечает. Запустите Docker Desktop, дождитесь «Engine running» и повторите установку.' -ForegroundColor Red
        exit 1
    }
    Step '1/3 Скачиваю образы: Ollama, bilingual_book_maker, Docling (≈ 6 ГБ)…' `
        @('compose', '--profile', 'cli', '--profile', 'gui', '--profile', 'docling', 'pull', '--ignore-buildable')
    Step '2/3 Собираю образ интерфейса…' @('compose', '--profile', 'gui', 'build')
    Step '3/3 Скачиваю модели rosetta-ru и translategemma-gpu (≈ 6 ГБ)…' `
        @('compose', 'run', '--rm', 'ollama-init')
}

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $Places) {
    $path = Join-Path $dir $Name
    if ($Remove) {
        if (Test-Path $path) { Remove-Item $path; Write-Host "Удалён: $path" } else { Write-Host "Нет ярлыка: $path" }
        continue
    }
    $lnk = $shell.CreateShortcut($path)
    # conhost --headless: PowerShell без окна. Иначе Windows 11 открывает его в Терминале, а Терминал
    # не умеет прятать окно (-WindowStyle Hidden не действует), и пустое окно висит, пока работает значок в трее.
    $lnk.TargetPath = Join-Path $env:WINDIR 'System32\conhost.exe'
    $lnk.Arguments = "--headless `"$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""
    $lnk.WindowStyle = 7    # свёрнуто — на случай, если окно всё же появится
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = "$Icon,0"
    $lnk.Description = 'Локальный перевод технических книг EN → RU'
    $lnk.Save()
    Write-Host "Создан: $path"
}
if (-not $Remove) {
    Write-Host ''
    Write-Host 'Готово. Запуск — ярлык «Перевод книг» на рабочем столе.' -ForegroundColor Green
}
