#Requires -Version 5.1
<#
  Перевод книг — запуск одним щелчком (ярлык создаёт app\install.ps1).

  1. Запускает Docker Desktop, если он не запущен, и ждёт готовности.
  2. Поднимает сервисы: docker compose --profile gui up -d (Ollama, интерфейс; Docling — только по необходимости).
  3. Открывает интерфейс отдельным окном (Edge в режиме приложения; если Edge нет — браузер по умолчанию).
  4. Живёт значком в трее: прогресс в подсказке, уведомление по окончании перевода,
     отключает спящий режим, пока идёт перевод (закрытие крышки ноутбука это не отменяет).
  5. Включает Docling (разметка PDF), только когда в очереди есть PDF, и выключает через 2 минуты
     после того, как он стал не нужен: так он не держит память во время перевода.

  Выход из значка не останавливает перевод: он идёт в Docker. «Остановить всё и выйти» останавливает сервисы;
  начатый перевод потом продолжается кнопкой «Повторить».
  Журнал запуска: %LOCALAPPDATA%\BookTranslate\launcher.log
#>
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Url = 'http://127.0.0.1:8090'
$AppData = Join-Path $env:LOCALAPPDATA 'BookTranslate'
New-Item -ItemType Directory -Force -Path $AppData | Out-Null
$LogFile = Join-Path $AppData 'launcher.log'

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
Add-Type -Namespace BookTranslate -Name Power -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$ES_CONTINUOUS = [Convert]::ToUInt32('80000000', 16)
$ES_SYSTEM_REQUIRED = [uint32]1

function Log([string]$m) {
    Add-Content -Path $LogFile -Encoding UTF8 -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
}

function Open-Window {
    try {
        Start-Process -FilePath 'msedge' -ArgumentList "--app=$Url", '--window-size=1320,920' -ErrorAction Stop
    } catch {
        Start-Process $Url
    }
}

function Test-Gui {
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/ping" -UseBasicParsing -TimeoutSec 5
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# docker.exe без окна; вывод — в журнал. Start-Process, а не &: в PowerShell 5.1 предупреждения docker в stderr
# превращаются в ошибки PowerShell.
function Invoke-Docker([string[]]$DockerArgs, [int]$TimeoutSec = 900) {
    $out = Join-Path $AppData 'docker.out'
    $err = Join-Path $AppData 'docker.err'
    try {
        $p = Start-Process -FilePath 'docker.exe' -ArgumentList $DockerArgs -WorkingDirectory $Root -WindowStyle Hidden `
            -PassThru -RedirectStandardOutput $out -RedirectStandardError $err -ErrorAction Stop
    } catch {
        Log "docker.exe не найден: $_"
        return -1
    }
    $null = $p.Handle   # без этого PowerShell 5.1 иногда отдаёт ExitCode = $null (известная ошибка Start-Process -PassThru)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not $p.HasExited) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 200
        if ((Get-Date) -gt $deadline) { try { $p.Kill() } catch {}; Log "docker $($DockerArgs -join ' '): время вышло"; return -2 }
    }
    $p.WaitForExit()
    $script:DockerOut = Get-Content $out -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    $text = ((Get-Content $out -Raw -Encoding UTF8 -ErrorAction SilentlyContinue) + (Get-Content $err -Raw -Encoding UTF8 -ErrorAction SilentlyContinue))
    Log ("docker {0} -> {1}`n{2}" -f ($DockerArgs -join ' '), $p.ExitCode, $text)
    return $p.ExitCode
}

function Test-Docker {
    $rc = Invoke-Docker @('info', '--format', '{{.ServerVersion}}') 60
    return ($rc -eq 0) -and ("$script:DockerOut".Trim() -match '^\d')
}

# Docker Desktop ставится либо для всех (Program Files), либо для пользователя (AppData\Local\Programs)
function Find-DockerDesktop {
    $cands = @()
    $cli = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($cli) { $cands += Join-Path (Split-Path (Split-Path (Split-Path $cli.Source))) 'Docker Desktop.exe' }
    $cands += Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    $cands += Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    return $null
}

# ---------- заставка на время запуска ----------
$splash = New-Object System.Windows.Forms.Form
$splash.Text = 'Перевод книг'
$splash.FormBorderStyle = 'FixedDialog'
$splash.MaximizeBox = $false
$splash.MinimizeBox = $true
$splash.StartPosition = 'CenterScreen'
$splash.ClientSize = New-Object System.Drawing.Size(420, 110)
$iconPath = Join-Path $PSScriptRoot 'icon.ico'
if (Test-Path $iconPath) { $splash.Icon = New-Object System.Drawing.Icon($iconPath) }
$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(18, 18)
$label.Size = New-Object System.Drawing.Size(384, 44)
$label.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$splash.Controls.Add($label)
$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(18, 70)
$bar.Size = New-Object System.Drawing.Size(384, 18)
$bar.Style = 'Marquee'
$splash.Controls.Add($bar)

function Set-Step([string]$text) {
    $label.Text = $text
    Log $text
    [System.Windows.Forms.Application]::DoEvents()
}

function Wait-Until([scriptblock]$Check, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (& $Check) { return $true }
        for ($i = 0; $i -lt 10; $i++) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 200 }
    }
    return $false
}

function Fail([string]$text) {
    Log "ОШИБКА: $text"
    $splash.Hide()
    [System.Windows.Forms.MessageBox]::Show("$text`n`nПодробности: $LogFile", 'Перевод книг', 'OK', 'Error') | Out-Null
    exit 1
}

# ---------- один экземпляр ----------
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\BookTranslateLauncher', [ref]$created)
if (-not $created) {
    Open-Window        # уже запущено — просто показать окно
    exit 0
}

# ---------- запуск ----------
Log "=== запуск, папка $Root"
if (Test-Gui) {
    Open-Window
} else {
    $splash.Show()
    Set-Step 'Проверка Docker…'
    if (-not (Test-Docker)) {
        $dd = Find-DockerDesktop
        if (-not $dd) { Fail 'Docker не отвечает, и Docker Desktop не найден. Запусти Docker Desktop вручную и открой приложение снова.' }
        Set-Step 'Запуск Docker Desktop…'
        Start-Process -FilePath $dd
        if (-not (Wait-Until { Test-Docker } 240)) { Fail 'Docker Desktop не запустился за 4 минуты.' }
    }
    Set-Step 'Запуск сервисов…'
    $rc = Invoke-Docker @('compose', '--profile', 'gui', 'up', '-d') 1800
    if ($rc -ne 0) { Fail "Сервисы не запустились (docker compose, код $rc)." }
    Set-Step 'Загрузка интерфейса…'
    if (-not (Wait-Until { Test-Gui } 120)) { Fail 'Интерфейс не ответил за 2 минуты.' }
    Log 'Интерфейс отвечает'
    $splash.Hide()
    Open-Window
}

# ---------- значок в трее ----------
$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = if (Test-Path $iconPath) { New-Object System.Drawing.Icon($iconPath) } else { [System.Drawing.SystemIcons]::Application }
$tray.Text = 'Перевод книг'
$tray.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$miOpen = $menu.Items.Add('Открыть')
$miOpen.Font = New-Object System.Drawing.Font($miOpen.Font, [System.Drawing.FontStyle]::Bold)
$miOpen.add_Click({ Open-Window })
$menu.Items.Add('Папка с книгами (source)').add_Click({ Start-Process explorer.exe (Join-Path $Root 'source') }) | Out-Null
$menu.Items.Add('Папка с результатами (books)').add_Click({ Start-Process explorer.exe (Join-Path $Root 'books') }) | Out-Null
$menu.Items.Add('-') | Out-Null
$miAwake = New-Object System.Windows.Forms.ToolStripMenuItem('Отключать спящий режим на время перевода')
$miAwake.CheckOnClick = $true
$miAwake.Checked = $true
$menu.Items.Add($miAwake) | Out-Null
$menu.Items.Add('-') | Out-Null
$menu.Items.Add('Выход (перевод продолжится в фоне)').add_Click({ Quit $false }) | Out-Null
$menu.Items.Add('Остановить всё и выйти').add_Click({ Quit $true }) | Out-Null
$tray.ContextMenuStrip = $menu
$tray.add_DoubleClick({ Open-Window })

$script:lastRunning = $null
$script:running = $null
$script:doclingIdleSince = $null
$script:doclingBusy = $false

# Docling нужен только для разметки PDF. Интерфейс сообщает, есть ли такие задачи (needs_docling);
# включаем при необходимости, выключаем через 2 минуты простоя — иначе он держит память во время перевода.
function Update-Docling($jobs) {
    try { $st = Invoke-RestMethod -Uri "$Url/api/status" -TimeoutSec 5 } catch { return }
    $need = @($jobs | Where-Object { $_.needs_docling }).Count -gt 0
    if ($script:doclingBusy) { return }   # запуск ещё идёт: Invoke-Docker крутит DoEvents, таймер может сработать внутри
    if ($need -and -not $st.docling) {
        Log 'Docling нужен для PDF — запускаю'
        $script:doclingBusy = $true
        # образ ставит app\install.ps1; если его нет (установка без install.ps1), up скачает ≈ 7,5 ГБ — ждём до 30 минут
        try { Invoke-Docker @('compose', '--profile', 'docling', 'up', '-d', '--no-deps', 'docling') 1800 | Out-Null }
        finally { $script:doclingBusy = $false }
        $script:doclingIdleSince = $null
    } elseif (-not $need -and $st.docling) {
        if (-not $script:doclingIdleSince) { $script:doclingIdleSince = Get-Date }
        elseif (((Get-Date) - $script:doclingIdleSince).TotalSeconds -gt 120) {
            Log 'Docling простаивает — останавливаю'
            Invoke-Docker @('compose', '--profile', 'docling', 'stop', 'docling') 120 | Out-Null
            $script:doclingIdleSince = $null
        }
    } else {
        $script:doclingIdleSince = $null
    }
}

function Quit([bool]$stopAll) {
    if ($stopAll) {
        if ($script:running) {
            $a = [System.Windows.Forms.MessageBox]::Show(
                "Сейчас переводится $($script:running.book). Остановить? Потом перевод можно продолжить кнопкой «Повторить».",
                'Перевод книг', 'YesNo', 'Warning')
            if ($a -ne 'Yes') { return }
        }
        $tray.Text = 'Останавливаю сервисы…'
        Invoke-Docker @('compose', '--profile', 'gui', '--profile', 'docling', 'stop') 180 | Out-Null
    }
    [BookTranslate.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    $timer.Stop()
    $tray.Visible = $false
    $tray.Dispose()
    [System.Windows.Forms.Application]::Exit()
}

function Short([string]$s, [int]$n) { if ($s.Length -le $n) { $s } else { $s.Substring(0, $n - 1) + '…' } }

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 15000
$timer.add_Tick({
    try {
        $jobs = Invoke-RestMethod -Uri "$Url/api/jobs" -TimeoutSec 5
    } catch {
        $tray.Text = 'Перевод книг: интерфейс не отвечает'
        [BookTranslate.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
        return
    }
    $run = @($jobs | Where-Object { $_.status -eq 'running' }) | Select-Object -First 1
    $script:running = $run
    Update-Docling $jobs
    if ($run) {
        $p = $run.progress
        $txt = if ($p -and $p.total -and ($null -ne $p.done)) { '{0}: {1}/{2} абз.' -f $run.name, $p.done, $p.total }
               elseif ($p -and $p.stage) { '{0}: {1}' -f $run.name, $p.stage } else { "$($run.name): идёт" }
        if ($p -and ($null -ne $p.eta_min)) { $txt += ", ~$($p.eta_min) мин" }
        $tray.Text = Short $txt 63
        $flags = if ($miAwake.Checked) { $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED } else { $ES_CONTINUOUS }
        [BookTranslate.Power]::SetThreadExecutionState([uint32]$flags) | Out-Null
    } else {
        $tray.Text = 'Перевод книг: ничего не переводится'
        [BookTranslate.Power]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    }
    # уведомление: задача, которая шла в прошлый раз, закончилась
    if ($script:lastRunning -and (-not $run -or $run.id -ne $script:lastRunning)) {
        $done = @($jobs | Where-Object { $_.id -eq $script:lastRunning }) | Select-Object -First 1
        if ($done) {
            switch ($done.status) {
                'done'    { $tray.ShowBalloonTip(15000, 'Книга готова', "$($done.book): перевод и проверка закончены.", 'Info') }
                'failed'  { $tray.ShowBalloonTip(15000, 'Ошибка перевода', "$($done.book): $(Short $done.note 180)", 'Error') }
                'stopped' { $tray.ShowBalloonTip(8000, 'Перевод остановлен', "$($done.book): продолжить — «Повторить».", 'Warning') }
            }
        }
    }
    $script:lastRunning = if ($run) { $run.id } else { $null }
})
$timer.Start()
$tray.ShowBalloonTip(5000, 'Перевод книг', 'Работаю в трее. Двойной щелчок — открыть окно.', 'Info')

$ctx = New-Object System.Windows.Forms.ApplicationContext
[System.Windows.Forms.Application]::Run($ctx)
$mutex.ReleaseMutex()
Log '=== выход'
