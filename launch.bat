@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
if errorlevel 1 goto failed
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
python run_local.py
goto end
:failed
echo Setup failed. Install Python 3.12 and try again.
:end
pause
