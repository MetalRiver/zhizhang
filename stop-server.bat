@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 智账 - 停止本地服务

echo.
echo   智账 - 停止本地服务
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到项目 Runtime：.venv\Scripts\python.exe
    pause
    exit /b 2
)

.venv\Scripts\python.exe serve.py --port 8787 --stop
if errorlevel 2 (
    echo.
    echo [!] 停止失败（详见上方原因）。服务状态可看 server-state.json。
) else (
    echo.
    echo [OK] 已处理。
)
pause
endlocal
