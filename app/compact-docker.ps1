#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
  Возвращает Windows место, которое занимает диск Docker Desktop (docker_data.vhdx).
  Файл .vhdx только растёт: после удаления образов место остаётся занятым, пока диск не сжать.
  Проверено 30.09.2026 на Windows 11 Домашняя (без Hyper-V): 102 → 34 ГБ.

  Запуск — PowerShell от имени администратора (Docker Desktop скрипт запустит сам), приложение «Перевод книг» закрыто
  («Остановить всё и выйти»):
    powershell -ExecutionPolicy Bypass -File C:\book_translate\app\compact-docker.ps1

  Что делает (образы, модели и тома не удаляются — ни этого, ни других проектов):
    1. Снимает временные «аренды» хранилища образов (containerd, метка gc.expire). Docker ставит их на 8 часов
       при скачивании и удалении образов, и всё это время слои удалённых образов занимают место.
    2. fstrim — помечает свободные блоки диска Docker как свободные.
    3. Закрывает Docker Desktop, выключает WSL и сжимает файл .vhdx (diskpart compact vdisk).
    4. Снова запускает Docker Desktop (ключ -NoRestart — не запускать).
  Нужен интернет: вспомогательный образ alpine (≈ 8 МБ) и пакет containerd-ctr; образ после работы удаляется.
#>
param([string]$Path, [switch]$NoRestart)
$ErrorActionPreference = 'Stop'

# docker.exe без вывода; true, если код 0. В PowerShell 5.1 перенаправленный stderr внешней программы
# при ErrorActionPreference=Stop превращается в исключение — поэтому здесь Continue.
function Test-Docker([string[]]$DockerArgs) {
    $ErrorActionPreference = 'Continue'
    & docker.exe @DockerArgs *> $null
    return ($LASTEXITCODE -eq 0)
}

function Say([string]$t, [string]$c = 'Cyan') { Write-Host ''; Write-Host "== $t" -ForegroundColor $c }
function FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 1) }
function SizeGB($p) { [math]::Round((Get-Item $p).Length / 1GB, 1) }

if (-not $Path) {
    $Path = @("$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx", "$env:LOCALAPPDATA\Docker\wsl\data\ext4.vhdx") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Path -or -not (Test-Path $Path)) {
    Write-Host 'Не найден диск Docker (docker_data.vhdx). Укажите путь: -Path "C:\Users\<имя>\AppData\Local\Docker\wsl\disk\docker_data.vhdx"' -ForegroundColor Red
    exit 1
}
$DockerDesktop = @("$env:ProgramFiles\Docker\Docker\Docker Desktop.exe", "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not (Test-Docker @('info', '--format', '{{.ServerVersion}}'))) {
    if (-not $DockerDesktop) { Write-Host 'Docker не отвечает. Запустите Docker Desktop и повторите.' -ForegroundColor Red; exit 1 }
    Write-Host 'Docker Desktop не запущен — запускаю и жду (до 4 минут)…'
    Start-Process explorer.exe -ArgumentList "`"$DockerDesktop`""   # через explorer — от обычного пользователя, не от администратора
    $deadline = (Get-Date).AddMinutes(4)
    while (-not (Test-Docker @('info', '--format', '{{.ServerVersion}}'))) {
        if ((Get-Date) -gt $deadline) { Write-Host 'Docker Desktop не запустился за 4 минуты.' -ForegroundColor Red; exit 1 }
        Start-Sleep -Seconds 5
    }
}

$running = @(& docker.exe ps -q)
if ($running.Count -gt 0) {
    Write-Host "Работают контейнеры: $($running.Count). Docker Desktop будет закрыт, они остановятся." -ForegroundColor Yellow
    Write-Host 'Если идёт перевод, сначала «Остановить всё и выйти» в значке «Перевод книг».' -ForegroundColor Yellow
    if ((Read-Host 'Продолжить? (y/n)') -ne 'y') { exit 0 }
}

$before = SizeGB $Path; $freeBefore = FreeGB
Write-Host "Диск Docker: $before ГБ, свободно на C: $freeBefore ГБ"
$hadAlpine = Test-Docker @('image', 'inspect', 'alpine')

Say '1/4 Снимаю временные аренды хранилища образов… (ниже — сколько аренд осталось)'
& docker.exe run --rm -v /run/containerd/containerd.sock:/run/containerd/containerd.sock alpine sh -c 'apk add -q containerd-ctr >/dev/null && ctr -n moby leases ls | grep gc.expire | while read id rest; do ctr -n moby leases rm --sync $id >/dev/null; done; ctr -n moby leases ls -q | wc -l'
if ($LASTEXITCODE -ne 0) { Write-Host 'Не получилось (нет интернета?). Продолжаю без этого шага.' -ForegroundColor Yellow }

Say '2/4 Помечаю свободное место (fstrim)…'
& docker.exe run --rm --privileged --pid=host alpine nsenter -t 1 -m -- sh -c 'df -h /mnt/docker-desktop-disk | tail -1; fstrim -av'
if (-not $hadAlpine) { $null = Test-Docker @('rmi', 'alpine') }

Say '3/4 Закрываю Docker Desktop и WSL, сжимаю диск (несколько минут)…'
Get-Process 'Docker Desktop', 'com.docker.backend', 'com.docker.build' -ErrorAction SilentlyContinue |
    ForEach-Object { try { $_ | Stop-Process -Force -ErrorAction Stop } catch { Write-Host "Не закрылся $($_.Name) — закройте Docker Desktop вручную (Quit)." -ForegroundColor Yellow } }
Start-Sleep -Seconds 5
& wsl.exe --shutdown
Start-Sleep -Seconds 5
$script = Join-Path $env:TEMP 'compact-docker.txt'
@"
select vdisk file="$Path"
attach vdisk readonly
compact vdisk
detach vdisk
"@ | Set-Content $script -Encoding ASCII
& diskpart.exe /s $script
Remove-Item $script -ErrorAction SilentlyContinue

$after = SizeGB $Path; $freeAfter = FreeGB
Say ("Готово: диск Docker $before → $after ГБ, свободно на C: $freeBefore → $freeAfter ГБ") 'Green'

if (-not $NoRestart) {
    Say '4/4 Запускаю Docker Desktop…'
    if ($DockerDesktop) { Start-Process explorer.exe -ArgumentList "`"$DockerDesktop`"" }
    else { Write-Host 'Docker Desktop не найден — запустите его сами.' -ForegroundColor Yellow }
}
