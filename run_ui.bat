@echo off
cd /d "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" (
    start "" http://localhost:8111
    "%~dp0venv\Scripts\python.exe" "%~dp0src\chaseup_ui.py"
) else (
    start "" http://localhost:8111
    python "%~dp0src\chaseup_ui.py"
)
