$ErrorActionPreference = "Stop"

Write-Host "[1/2] 启动 HeartOS Backend (Docker)" -ForegroundColor Cyan
Set-Location "E:\HeartOS\heartos_backend"
docker compose up -d --build

Write-Host "[2/2] 启动 HeartOS Frontend (http.server:8080)" -ForegroundColor Cyan
Start-Process powershell -ArgumentList '-NoExit','-Command','cd E:\HeartOS; python -m http.server 8080'

Write-Host "完成。请打开: http://127.0.0.1:8080/noteai_v4_0423.html" -ForegroundColor Green
Write-Host "后端健康检查: http://127.0.0.1:9000/health" -ForegroundColor Green
