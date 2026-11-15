@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Setting up Python environment, please wait...
  python -m venv .venv || (echo Python is not installed. Install Python 3.11+ and tick "Add Python to PATH" & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
python app.py
pause
