@echo off
REM Double-click to start the local test server. Opens http://localhost:8000 automatically.
REM Uses only the local PostgreSQL database - never touches Render.
cd /d "%~dp0"
python scripts\run_local.py --pg-password 123456789
pause
