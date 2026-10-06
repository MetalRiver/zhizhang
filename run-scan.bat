@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 智账 - 手动增量扫描

echo.
echo   智账 - 手动增量扫描（discover -^> scan -^> board -^> health）
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到项目 Runtime：.venv\Scripts\python.exe
    echo     正式运行禁止使用系统 Python。参见 docs\RUNTIME.md
    pause
    exit /b 2
)

.venv\Scripts\python.exe autopilot.py auto --trigger manual
echo.
echo 状态查看：.venv\Scripts\python.exe autopilot.py status
pause
endlocal
