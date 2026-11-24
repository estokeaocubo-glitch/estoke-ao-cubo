@echo off
chcp 65001 >nul 2>&1
setlocal

set "PY314=C:\Users\xavie\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if exist "%PY314%" (
    "%PY314%" "%~dp0build_local.py" %*
) else (
    python "%~dp0build_local.py" %*
)
exit /b %errorlevel%
