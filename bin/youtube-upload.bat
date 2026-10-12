@echo off
rem Run the command-line uploader from a source checkout.
setlocal
where py >nul 2>&1
if errorlevel 1 goto :usepython
py -3 "%~dp0youtube-upload" %*
exit /b %errorlevel%
:usepython
python "%~dp0youtube-upload" %*
exit /b %errorlevel%
