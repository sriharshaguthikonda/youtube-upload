@echo off
rem Launch the YouTube Upload GUI from a fresh clone, with or without a venv.
rem Looks for an interpreter in this order:
rem   1. .venv\ or venv\ inside this folder
rem   2. the legacy "youtube-upload\" venv
rem   3. the Windows "py" launcher
rem   4. python / pythonw on PATH
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

set "PY="
set "PYW="
for %%D in (.venv venv youtube-upload) do (
  if not defined PY if exist "%%D\Scripts\python.exe" (
    set "PY=%%D\Scripts\python.exe"
    set "PYW=%%D\Scripts\pythonw.exe"
  )
)
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 (
    set "PY=py -3"
    set "PYW=pyw -3"
  )
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    set "PY=python"
    set "PYW=pythonw"
  )
)
if not defined PY goto :nopython

rem pythonw hides every error, so check imports with the console interpreter first.
%PY% -c "import youtube_upload.gui_app" >nul 2>"%TEMP%\youtube_upload_launch.log"
if errorlevel 1 goto :missing

start "" %PYW% -m youtube_upload.gui_app
exit /b 0

:nopython
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then run:  py -3 -m pip install ".[gui]"
pause
exit /b 1

:missing
echo The GUI could not start. Install the dependencies once with:
echo.
echo     %PY% -m pip install ".[gui]"
echo.
echo Details:
type "%TEMP%\youtube_upload_launch.log"
pause
exit /b 1
