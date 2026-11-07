@echo off
rem 一键启动（Windows）
cd /d "%~dp0"

if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q --upgrade pip
)
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
.venv\Scripts\python.exe -m sshdesk %*
pause
