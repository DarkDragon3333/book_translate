#Requires -Version 5.1
<#
  Скачивает большие тестовые книги (Pro Git и The API Book, свободные лицензии) в папку test_books.
  В репозиторий они не входят, чтобы не раздувать его на 45 МБ; testbook.* лежат в репозитории.
  Запуск из C:\book_translate:  powershell -ExecutionPolicy Bypass -File test_books\download.ps1
  Уже скачанные файлы с верной контрольной суммой не качаются заново.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # иначе Invoke-WebRequest в PowerShell 5.1 качает в разы медленнее
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Dir = $PSScriptRoot

$Books = @(
    @{ File = 'progit.epub';  Sha = 'ed3e5e9f5b7da9c05fae7ac2fcfaaa887e5df196a4a07b59ffed832818f73ae1'
       Url  = 'https://github.com/progit/progit2/releases/download/2.1.450/progit.epub' },
    @{ File = 'progit.pdf';   Sha = '403f4051cdca2c585361d85f6e87d5dd87d8c544fc91dced007711babe81e8ef'
       Url  = 'https://github.com/progit/progit2/releases/download/2.1.450/progit.pdf' },
    @{ File = 'apibook.epub'; Sha = '20a6f5b5e565195deca0fadb7f8516b6b728d23d27028fc87d6418aff23fcefe'
       Url  = 'https://raw.githubusercontent.com/twirl/The-API-Book/a86cdbaaeb5856b1489be1d4bd79d4681606638d/docs/API.en.epub' },
    @{ File = 'apibook.pdf';  Sha = '3fa1179b1e832e02b0417162d7cc5d05f49e0f13d61aa07f952ed41597db8c41'
       Url  = 'https://raw.githubusercontent.com/twirl/The-API-Book/a86cdbaaeb5856b1489be1d4bd79d4681606638d/docs/API.en.pdf' }
)

function Test-Sha([string]$Path, [string]$Sha) {
    (Test-Path $Path) -and ((Get-FileHash $Path -Algorithm SHA256).Hash -eq $Sha.ToUpper())
}

$failed = 0
foreach ($b in $Books) {
    $path = Join-Path $Dir $b.File
    if (Test-Sha $path $b.Sha) { Write-Host "$($b.File): уже есть"; continue }
    Write-Host "$($b.File): скачиваю…"
    try {
        Invoke-WebRequest -Uri $b.Url -OutFile "$path.part" -UseBasicParsing
        if (-not (Test-Sha "$path.part" $b.Sha)) { throw 'контрольная сумма не совпала' }
        Move-Item -Force "$path.part" $path
        Write-Host "$($b.File): готово"
    } catch {
        Remove-Item -Force -ErrorAction SilentlyContinue "$path.part"
        Write-Host "$($b.File): ОШИБКА — $_" -ForegroundColor Red
        $failed++
    }
}
if ($failed) { exit 1 }
