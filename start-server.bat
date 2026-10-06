@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 智账 - 启动本地服务

echo.
echo ============================================================
echo   智账 - 启动本地服务（Web + API）
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [X] 未找到项目 Runtime：.venv\Scripts\python.exe
    echo     请先按 docs\RUNTIME.md 创建 .venv 并安装 requirements-core.txt
    echo     正式运行禁止使用系统 Python。
    pause
    exit /b 2
)

set PY=.venv\Scripts\python.exe

echo [1/3] Runtime 检查 ...
%PY% scripts\check_runtime.py
if errorlevel 1 (
    echo [X] Runtime 检查未通过，已停止。
    pause
    exit /b 2
)

echo.
echo [2/3] 单实例检测 ...
%PY% serve.py --port 8787 --check-only
if errorlevel 3 (
    echo [X] 端口 8787 被其它程序占用（未强杀）。可用：
    echo     .venv\Scripts\python.exe serve.py --port 8788
    pause
    exit /b 3
)
if errorlevel 2 (
    echo [i] 服务已在运行（浏览器打开 http://127.0.0.1:8787/ 即可）。
    pause
    exit /b 0
)

echo.
echo [3/3] 后台启动（pythonw，无长期控制台窗口）...
start "智账 Server" ".venv\Scripts\pythonw.exe" serve.py --port 8787
timeout /t 2 /nobreak >nul
%PY% serve.py --port 8787 --check-only
if errorlevel 2 (
    echo [OK] 服务已在后台运行：http://127.0.0.1:8787/
) else (
    echo [!] 服务未能确认启动，请查看 server.log 与 server-state.json
)
echo.
echo 停止服务请运行：stop-server.bat
pause
endlocal
