param(
  [string]$LocalRoot = "E:\HeartOS\HeartOS_system\HeartOS",
  [string]$ServerRoot = "E:\HeartOS\service\heartOS_v3\HeartOS",
  [string]$OutputRoot = "E:\HeartOS\HeartOS_system\HeartOS_v3_for_server",
  [string]$BackendUrl = "http://219.147.100.43:18005",
  [string]$BackendHostPort = "",
  [switch]$Clean
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath {
  param([string]$Path)

  $executionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Assert-PathIsSafe {
  param(
    [string]$Path,
    [string]$Name
  )

  $fullPath = Resolve-FullPath $Path
  $root = [System.IO.Path]::GetPathRoot($fullPath)

  if ([string]::IsNullOrWhiteSpace($fullPath) -or $fullPath -eq $root) {
    throw "$Name points to an unsafe path: $fullPath"
  }

  return $fullPath
}

function Invoke-RobocopyChecked {
  param(
    [string]$From,
    [string]$To,
    [string[]]$ExtraArgs = @()
  )

  $args = @($From, $To, "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:2", "/W:1", "/NP") + $ExtraArgs
  & robocopy @args
  $exitCode = $LASTEXITCODE

  if ($exitCode -ge 8) {
    throw "robocopy failed from '$From' to '$To' with exit code $exitCode"
  }
}

function Copy-OverlayFile {
  param([string]$RelativePath)

  $from = Join-Path $ServerRoot $RelativePath
  $to = Join-Path $OutputRoot $RelativePath

  if (!(Test-Path $from)) {
    throw "Server overlay file is missing: $from"
  }

  New-Item -ItemType Directory -Force -Path (Split-Path $to -Parent) | Out-Null
  Copy-Item -LiteralPath $from -Destination $to -Force
}

function Replace-TextInFile {
  param(
    [string]$Path,
    [string]$OldText,
    [string]$NewText
  )

  if (!(Test-Path $Path)) {
    throw "Cannot patch missing file: $Path"
  }

  $content = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
  if ($content.Contains($OldText)) {
    $content = $content.Replace($OldText, $NewText)
    Set-Content -LiteralPath $Path -Value $content -NoNewline -Encoding UTF8
  }
}

$LocalRoot = Assert-PathIsSafe $LocalRoot "LocalRoot"
$ServerRoot = Assert-PathIsSafe $ServerRoot "ServerRoot"
$OutputRoot = Assert-PathIsSafe $OutputRoot "OutputRoot"

if (!(Test-Path $LocalRoot)) {
  throw "LocalRoot does not exist: $LocalRoot"
}

if (!(Test-Path $ServerRoot)) {
  throw "ServerRoot does not exist: $ServerRoot"
}

if ($OutputRoot -eq $LocalRoot -or $OutputRoot -eq $ServerRoot) {
  throw "OutputRoot must be a separate directory. This script does not modify LocalRoot or ServerRoot in place."
}

$excludedDirs = @(".git", ".venv", "__pycache__", "node_modules", "model_outputs", "data")
$excludedFiles = @("*.pyc")

Write-Host "[1/5] Preparing output: $OutputRoot" -ForegroundColor Cyan
if ((Test-Path $OutputRoot) -and $Clean) {
  $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $backupPath = "$OutputRoot.backup.$timestamp"
  Write-Host "Backing up existing output to: $backupPath" -ForegroundColor Yellow
  Move-Item -LiteralPath $OutputRoot -Destination $backupPath
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

Write-Host "[2/5] Copying local HeartOS code" -ForegroundColor Cyan
Invoke-RobocopyChecked `
  -From $LocalRoot `
  -To $OutputRoot `
  -ExtraArgs (@("/XD") + $excludedDirs + @("/XF") + $excludedFiles)

Write-Host "[3/5] Applying server configuration overlay from heartOS_v3" -ForegroundColor Cyan
Copy-OverlayFile "heartos_backend\.env"

Write-Host "[4/5] Applying fixed server patches" -ForegroundColor Cyan
$indexPath = Join-Path $OutputRoot "index.html"
Replace-TextInFile `
  -Path $indexPath `
  -OldText "http://127.0.0.1:9000" `
  -NewText $BackendUrl

Replace-TextInFile `
  -Path $indexPath `
  -OldText "http://219.147.100.43:18005" `
  -NewText $BackendUrl

$configPath = Join-Path $OutputRoot "heartos_backend\app\config.py"
Replace-TextInFile `
  -Path $configPath `
  -OldText 'ai_ecg_digitize_url: str = Field(default="")' `
  -NewText 'ai_ecg_digitize_url: str = Field(default="http://219.147.100.43:18004/digitize")'

$envPath = Join-Path $OutputRoot "heartos_backend\.env"
Replace-TextInFile `
  -Path $envPath `
  -OldText "APP_PUBLIC_BASE_URL=http://127.0.0.1:9000" `
  -NewText "APP_PUBLIC_BASE_URL=$BackendUrl"

if (![string]::IsNullOrWhiteSpace($BackendHostPort)) {
  $composePath = Join-Path $OutputRoot "heartos_backend\docker-compose.yml"

  Replace-TextInFile `
    -Path $composePath `
    -OldText "container_name: heartos-backend" `
    -NewText "container_name: heartos-backend-$BackendHostPort"

  Replace-TextInFile `
    -Path $composePath `
    -OldText '"9000:9000"' `
    -NewText "`"$BackendHostPort`:9000`""

  Replace-TextInFile `
    -Path $composePath `
    -OldText "APP_PUBLIC_BASE_URL: http://127.0.0.1:9000" `
    -NewText "APP_PUBLIC_BASE_URL: $BackendUrl"
}

$serverDataPath = Join-Path $ServerRoot "heartos_backend\data"
$outputDataPath = Join-Path $OutputRoot "heartos_backend\data"

if (Test-Path $serverDataPath) {
  Write-Host "Copying server data from heartOS_v3" -ForegroundColor Cyan
  Invoke-RobocopyChecked `
    -From $serverDataPath `
    -To $outputDataPath `
    -ExtraArgs (@("/XD") + @("__pycache__") + @("/XF") + @("*.pyc"))
} else {
  New-Item -ItemType Directory -Force -Path (Join-Path $outputDataPath "uploads") | Out-Null
}

Write-Host "[5/5] Verifying converted package" -ForegroundColor Cyan
$requiredFiles = @(
  "index.html",
  "ecg_digitizer_enhanced.html",
  "heartos_backend\.env",
  "heartos_backend\docker-compose.yml",
  "heartos_backend\app\main.py",
  "heartos_backend\app\config.py"
)

foreach ($file in $requiredFiles) {
  $full = Join-Path $OutputRoot $file
  if (!(Test-Path $full)) {
    throw "Required package file is missing: $full"
  }
}

$envText = Get-Content -LiteralPath (Join-Path $OutputRoot "heartos_backend\.env") -Raw -Encoding UTF8
if ($envText -notmatch "APP_AUTH_MODE=upstream") {
  throw "Server .env was not applied: APP_AUTH_MODE is not upstream."
}

$indexText = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8
if (!$indexText.Contains($BackendUrl)) {
  throw "index.html was not patched to the server backend address."
}

Write-Host "Converted HeartOS v3 package ready: $OutputRoot" -ForegroundColor Green
Write-Host "Upload this output directory to the server for offline function testing." -ForegroundColor Green
