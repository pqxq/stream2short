@echo off
:: Stream2Short launcher — https://github.com/pqxq/stream2short
:: Creates the virtual environment if needed, installs dependencies, then starts
:: the Streamlit studio.  Run from the repository root:
::   start.bat

setlocal enabledelayedexpansion

set "REPO_DIR=%~dp0"
set "VENV_DIR=%REPO_DIR%.venv"
set "PYTHON=python"

:: ---------------------------------------------------------------------------
:: 1. Create virtual environment
:: ---------------------------------------------------------------------------
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [stream2short] Creating virtual environment...
    "%PYTHON%" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment. Is Python 3.10+ installed?
        pause
        exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"

:: ---------------------------------------------------------------------------
:: 2. Install / update dependencies
:: ---------------------------------------------------------------------------
echo [stream2short] Installing dependencies...
pip install --quiet --upgrade pip
pip install --quiet -r "%REPO_DIR%requirements.txt"
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

:: ---------------------------------------------------------------------------
:: 3. Launch Streamlit
:: ---------------------------------------------------------------------------
echo [stream2short] Starting Streamlit studio...
streamlit run "%REPO_DIR%app.py" %*
