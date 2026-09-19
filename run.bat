@echo off
REM === Lance le site anime en LOCAL (sans Discord) et ouvre ta tier list ===
cd /d "%~dp0"
echo Demarrage du serveur local sur http://127.0.0.1:8030 ...
start "" http://127.0.0.1:8030/tierlist
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8030
pause
