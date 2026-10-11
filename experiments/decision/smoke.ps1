# Проверка /v1/systemone: smoke.ps1 <модель>
param([string]$Model = "tev1:0.8b")
if (-not $Model) { $Model = "tev1:0.8b" }
"Модель: $Model"
foreach ($f in "smoke_good.json", "smoke_bad.json") {
  $req = Get-Content "$PSScriptRoot\$f" -Raw -Encoding UTF8 | ConvertFrom-Json
  $req.model = $Model
  $bytes = [Text.Encoding]::UTF8.GetBytes(($req | ConvertTo-Json -Depth 10 -Compress))
  $sw = [Diagnostics.Stopwatch]::StartNew()
  try {
    $r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:11435/v1/systemone `
         -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 600
    $out = $r | ConvertTo-Json -Depth 10 -Compress
  } catch {
    $out = "ОШИБКА: " + $_.ErrorDetails.Message + " " + $_.Exception.Message
  }
  $sw.Stop()
  "{0,-16} {1,6} мс  {2}" -f $f, $sw.ElapsedMilliseconds, $out
}
