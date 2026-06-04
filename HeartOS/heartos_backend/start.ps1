$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Get-EnvValue {
  param(
    [string]$Key,
    [string]$Default = ""
  )

  $current = [Environment]::GetEnvironmentVariable($Key)
  if (![string]::IsNullOrWhiteSpace($current)) {
    return $current
  }

  if (Test-Path .\.env) {
    $line = Get-Content .\.env | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1
    if ($line) {
      $value = $line.Substring($Key.Length + 1).Trim()
      if ($value.StartsWith('"') -and $value.EndsWith('"')) {
        $value = $value.Substring(1, $value.Length - 2)
      }
      if (![string]::IsNullOrWhiteSpace($value)) {
        return $value
      }
    }
  }

  return $Default
}

if (!(Test-Path .\.env) -and (Test-Path .\.env.example)) {
  Copy-Item .\.env.example .\.env
  Write-Host "未找到 .env，已从 .env.example 复制一份。请按部署环境修改其中配置。" -ForegroundColor Yellow
}

if (!(Test-Path .\.venv)) {
  python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt

$appHost = Get-EnvValue "APP_HOST" "0.0.0.0"
$appPort = Get-EnvValue "APP_PORT" "9010"
$reloadEnabled = Get-EnvValue "HEARTOS_RELOAD" "1"
$reloadArgs = @()
if ($reloadEnabled -eq "1") {
  $reloadArgs = @("--reload")
}

Write-Host "HeartOS Backend: http://127.0.0.1:$appPort" -ForegroundColor Green
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host $appHost --port $appPort @reloadArgs
