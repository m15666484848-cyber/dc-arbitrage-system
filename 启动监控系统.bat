@echo off
chcp 65001 >nul
title DC全币种套利监控系统

echo ==============================================
echo DC全币种套利监控系统 启动器
echo ==============================================
echo.

:: 检查Python是否安装
python --version >nul 2>&1
if %errorlevel% equ 0 (
    echo ✅ Python已安装
    goto :start_server
)

:: 检查Python3是否安装
python3 --version >nul 2>&1
if %errorlevel% equ 0 (
    echo ✅ Python3已安装
    set "python=python3"
    goto :start_server
)

:: 没有Python，提示安装
echo ❌ 未检测到Python，请先安装Python
echo.
echo 下载地址：https://www.python.org/downloads/windows/
echo 安装时请勾选"Add Python to PATH"选项
echo.
pause
exit /b

:start_server
echo.
echo 🚀 正在启动Web服务器...
echo 🌐 访问地址：http://localhost:8888
echo 🌐 局域网访问：http://%computername%:8888 或者 http://你的IP:8888
echo 🛑 按 Ctrl+C 停止服务器
echo.

:: 启动HTTP服务器，端口8888
python -m http.server 8888

pause
