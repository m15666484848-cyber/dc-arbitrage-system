@echo off
chcp 65001 >nul
title DC套利监控系统

echo 📂 当前目录: %cd%
echo ✅ 启动服务器，端口8080
echo.
echo 🌐 访问地址：
echo    本地：http://localhost:8080
echo    公网：http://103.174.97.87:8080
echo.

python -m http.server 8080

pause
