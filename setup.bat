@echo off
REM One-shot local setup: venv + dependencies + .env wizard (paper defaults).
REM Double-click or run from cmd. Safe to re-run.
cd /d "%~dp0"
if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo FAILED: python not found on PATH. Install Python 3.11+ first.
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo FAILED: dependency install error.
    pause
    exit /b 1
)
if exist .env (
    echo .env already exists - skipping wizard to preserve your settings.
    echo To redo it: python -m trading_bot.main setup
) else (
    python -m trading_bot.main setup
)
echo.
echo Setup finished. Next steps:
echo   .venv\Scripts\activate
echo   python -m pytest tests -q
echo   python -m trading_bot.main paper
pause
