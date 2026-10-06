@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 智账 - 卸载后台自动采集

echo.
echo ============================================================
echo   智账 - 卸载后台自动采集
echo ============================================================
echo.
echo 本操作会删除 Windows 计划任务 UsageLedger-Auto。
echo 账本数据（usage.db）、board、日志与自动发现结果都**不会**被删除。
echo.
echo 数据保留在：%~dp0
echo.

set PY=
where py >nul 2>nul ^&^& set PY=py -3
if "%PY%"=="" where python >nul 2>nul ^&^& set PY=python
if "%PY%"=="" (
  echo [X] 没有找到 Python。改用系统自带命令手动删除：
  echo     schtasks /Delete /F /TN UsageLedger-Auto
  echo.
  pause
  exit /b 3
)

%PY% "%~dp0autopilot.py" uninstall-auto
echo.
echo 退出码 %errorlevel%  ^(0^=成功^)
echo 如需彻底清理，手动删除 usage.db / usage-board.json / run-state.json / auto.log / discovery.json。
echo.
pause
endlocal
