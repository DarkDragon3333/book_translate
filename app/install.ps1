#Requires -Version 5.1
<#
  Создаёт ярлыки «Перевод книг» на рабочем столе и в меню «Пуск».
  Запуск (из C:\book_translate):  powershell -ExecutionPolicy Bypass -File app\install.ps1
  Удалить ярлыки:                  powershell -ExecutionPolicy Bypass -File app\install.ps1 -Remove
#>
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $PSScriptRoot 'BookTranslate.ps1'
$Icon = Join-Path $PSScriptRoot 'icon.ico'
$Name = 'Перевод книг.lnk'
$Places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Microsoft\Windows\Start Menu\Programs')
)
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $Places) {
    $path = Join-Path $dir $Name
    if ($Remove) {
        if (Test-Path $path) { Remove-Item $path; Write-Host "Удалён: $path" }
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
