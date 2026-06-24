@echo off
REM ====================================================================
REM  Delivery Toolbox - Windows launcher (waitress WSGI server)
REM ====================================================================
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%"

REM Prefer a local .venv; fall back to a parent .venv if present.
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "..\.venv\Scripts\activate.bat" (
    call "..\.venv\Scripts\activate.bat"
) else (
    echo [setup] Creating virtual environment...
    python -m venv .venv || goto :fail
    call ".venv\Scripts\activate.bat" || goto :fail
)

echo [setup] Installing/updating dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || goto :fail

if not exist ".env" (
    echo [setup] No .env found. Copying .env.example -> .env
    copy /Y ".env.example" ".env" >nul
    echo [setup] Edit .env to set FLASK_SECRET_KEY and ADMIN_PASSWORD before exposing to users.
)

echo [run] Starting Delivery Toolbox...
python -m waitress --listen=0.0.0.0:5000 app:app

popd
exit /b 0

:fail
echo [error] Startup failed.
popd
exit /b 1
