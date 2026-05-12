@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Convert-HeartOSSystemToV3.ps1" -Clean

if errorlevel 1 (
  echo.
  echo Convert failed. See the PowerShell error above.
  exit /b %errorlevel%
)

echo.
echo Done. Output: E:\HeartOS\HeartOS_system\HeartOS_v3_for_server
