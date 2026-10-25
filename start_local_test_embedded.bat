@echo off
REM Double-click to start the local test server (no PostgreSQL install needed). Opens http://localhost:8020
REM Uses an embedded local database in .localdb - never touches Render.
cd /d "%~dp0"
python scripts\start_embedded.py
pause
