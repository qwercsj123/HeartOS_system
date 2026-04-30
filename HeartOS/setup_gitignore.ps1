$ErrorActionPreference = "Stop"

param(
  [switch]$Force
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$gitignorePath = Join-Path $root ".gitignore"

$content = @'
# ------------------------------
# HeartOS ignore rules
# ------------------------------

# OS / editor
.DS_Store
Thumbs.db
*.swp
*.swo
*.tmp
*.bak
*~
.idea/
.vscode/

# Env / secrets
.env
.env.*
!.env.example
*.pem
*.key
*.crt

# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd
.Python
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
.coverage.*
htmlcov/
dist/
build/
*.egg-info/

# Virtual envs (important)
.venv/
venv/
env/
heartos_backend/.venv/

# Node
node_modules/
npm-debug.log*
yarn-error.log*
pnpm-debug.log*
.next/

# Runtime / temp
*.log
logs/
temp/
tmp/
*.sqlite
*.sqlite3
*.db

# Project generated assets (optional; uncomment if needed)
# model_outputs/
# uploads/
# exports/
'@

if ((Test-Path $gitignorePath) -and -not $Force) {
  $existing = Get-Content -Path $gitignorePath -Raw
  if ($existing -match "HeartOS ignore rules") {
    Write-Host "[OK] .gitignore already configured by this script." -ForegroundColor Green
  } else {
    Add-Content -Path $gitignorePath -Value "`r`n$content"
    Write-Host "[OK] Appended HeartOS ignore block to existing .gitignore" -ForegroundColor Green
  }
} else {
  Set-Content -Path $gitignorePath -Value $content -Encoding UTF8
  Write-Host "[OK] Created .gitignore" -ForegroundColor Green
}

# Show tracked files that should now be ignored
$tracked = git ls-files 2>$null
if ($LASTEXITCODE -eq 0 -and $tracked) {
  $patterns = @(
    '^heartos_backend/\.venv/',
    '^\.env$',
    '^heartos_backend/\.env$',
    '\.pyc$',
    '\.pyo$',
    '\.log$',
    '\.sqlite3?$',
    '\.db$'
  )
  $bad = @()
  foreach ($f in $tracked) {
    foreach ($p in $patterns) {
      if ($f -match $p) { $bad += $f; break }
    }
  }
  if ($bad.Count -gt 0) {
    Write-Host "`n[WARN] These tracked files should be untracked:" -ForegroundColor Yellow
    $bad | Sort-Object -Unique | ForEach-Object { Write-Host "  $_" }
    Write-Host "`nRun this to untrack them (files stay on disk):" -ForegroundColor Cyan
    Write-Host "  git rm -r --cached heartos_backend/.venv .env heartos_backend/.env"
    Write-Host "  git commit -m `"chore: ignore local env/runtime files`""
  } else {
    Write-Host "[OK] No currently tracked files matched ignore-sensitive patterns." -ForegroundColor Green
  }
}

Write-Host "`nDone."
