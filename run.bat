@echo off
REM ====================================================================
REM  Delivery Toolbox - Windows launcher (waitress WSGI server)
REM ====================================================================
setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%"

where uv >nul 2>nul
if errorlevel 1 (
    echo [setup] uv not found -- installing ^(https://astral.sh/uv^)...
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" || goto :fail
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo [setup] Syncing dependencies ^(uv.lock^)...
uv sync || goto :fail

if not exist ".env" (
    echo [setup] No .env found. Copying .env.example -^> .env
    copy /Y ".env.example" ".env" >nul
    echo [setup] Edit .env to set FLASK_SECRET_KEY and ADMIN_PASSWORD before exposing to users.
)

set "PORT=5000"
set "HOST=0.0.0.0"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if "%%A"=="PORT" if not "%%B"=="" set "PORT=%%B"
    if "%%A"=="HOST" if not "%%B"=="" set "HOST=%%B"
)

echo [run] Starting Delivery Toolbox on %HOST%:%PORT%...
uv run python -m waitress --listen=%HOST%:%PORT% app:app

popd
exit /b 0

:fail
echo [error] Startup failed.
popd
exit /b 1
