@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=%USERPROFILE%\AppData\Local\Programs\Python\Python314\python.exe"
echo ============================================
echo  BR Data Preview
echo  http://127.0.0.1:8765/
echo  First run: migrate + seed legacy data
echo  Close this window or Ctrl+C to stop
echo ============================================
"%PY%" manage.py migrate --noinput
"%PY%" manage.py seed_preview
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765/'"
"%PY%" manage.py runserver 127.0.0.1:8765
pause
