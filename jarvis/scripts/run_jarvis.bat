@echo off
rem Launch Jarvis with a tray icon and no console window.
rem Uses the project virtual environment if there is one, otherwise pythonw from PATH.
rem Extra arguments are passed through, e.g.  run_jarvis.bat --debug
setlocal
set "JARVIS_DIR=%~dp0.."
if exist "%JARVIS_DIR%\.venv\Scripts\activate.bat" (
    call "%JARVIS_DIR%\.venv\Scripts\activate.bat"
) else if exist "%JARVIS_DIR%\..\.venv\Scripts\activate.bat" (
    call "%JARVIS_DIR%\..\.venv\Scripts\activate.bat"
)
where pythonw >nul 2>nul
if errorlevel 1 (
    echo pythonw.exe not found. Install Python from python.org and tick "Add to PATH".
    pause
    exit /b 1
)
cd /d "%JARVIS_DIR%"
start "" pythonw main.py --wake --tray %*
endlocal
