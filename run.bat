@echo off
REM NetScrollOffline launcher (Windows)
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python app.py
