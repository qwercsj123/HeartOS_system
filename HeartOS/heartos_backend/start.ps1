$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (!(Test-Path .\.venv)) {
  python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt

$env:APP_ENV = "dev"
$env:APP_HOST = "0.0.0.0"
$env:APP_PORT = "9000"
$env:APP_PUBLIC_BASE_URL = "http://127.0.0.1:9000"
$env:APP_CORS_ORIGINS = "http://127.0.0.1:8080,http://localhost:8080"

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 9000 --reload
