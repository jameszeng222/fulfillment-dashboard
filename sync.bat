@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo. >> sync.log
echo ================ %date% %time% ================ >> sync.log
"C:\Users\HT\AppData\Local\Programs\Python\Python312\python.exe" sync.py --push >> sync.log 2>&1
echo [exit %errorlevel%] >> sync.log
