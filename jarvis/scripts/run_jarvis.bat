@echo off
rem Launch Jarvis with a tray icon and no console window, using the project virtual environment.
rem Extra arguments are passed through, e.g.  run_jarvis.bat --debug
setlocal
set "JARVIS_DIR=%~dp0.."
if exist "%JARVIS_DIR%\.venv\Scripts\activate.bat" (
    call "%JARVIS_DIR%\.venv\Scripts\activate.bat"
) else if exist "%JARVIS_DIR%\..\.venv\Scripts\activate.bat" (
    call "%JARVIS_DIR%\..\.venv\Scripts\activate.bat"
) else (
    echo Virtual environment not found. See README: Install.
    pause
    exit /b 1
)
cd /d "%JARVIS_DIR%"
start "" pythonw main.py --wake --tray %*
endlocal
