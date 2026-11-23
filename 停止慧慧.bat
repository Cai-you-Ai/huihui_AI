@echo off
taskkill /IM pythonw.exe /F >nul 2>&1
echo HuiHui stopped.
timeout /t 2 >nul
exit
