@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 智账 - 安装后台自动采集

echo.
echo ============================================================
echo   智账 - 一键安装后台自动采集
echo ============================================================
echo.
echo 本操作会向 Windows 任务计划程序注册一个周期任务，
echo 每 30 分钟自动执行一次「发现 -^> 采集 -^> 出 board」。
echo 这是你本次的显式授权；不会静默注册任何系统任务。
echo.
echo 数据全部留在本机，采集过程默认不联网。
echo.

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo [X] 没有找到 Python。请先安装 Python 3.10+ 并加入 PATH。
  echo     下载：https://www.python.org/downloads/
  echo.
  pause
  exit /b 3
)

echo [1/3] 检查 dsh 解析依赖 zstandard ...
%PY% -c "import zstandard" >nul 2>nul
if errorlevel 1 (
  echo     未安装，正在安装 ^(仅解析 dsh 会话需要^) ...
  %PY% -m pip install -q zstandard
) else (
  echo     已就绪。
)

echo.
echo [2/3] 注册计划任务并立即自检 ...
%PY% "%~dp0autopilot.py" install-auto --minutes 30
set RC=%errorlevel%

echo.
echo [3/3] 完成。退出码 %RC%  ^(0^=成功 / 2^=部分失败 / 3^=未发现数据源^)
if "%RC%"=="3" (
  echo.
  echo [!] 未发现受支持的数据源。请检查 discovery.json，
  echo     或在 sources.json 里为 dsh 等客户端指定路径。
)
echo.
echo 查看状态：%PY% "%~dp0autopilot.py" status
echo 运行日志：%~dp0auto.log
echo 状态文件：%~dp0run-state.json
echo.
pause
endlocal
