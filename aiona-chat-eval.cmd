@echo off
rem Run on Windows: aiona-chat-eval start (installer adds this folder to PATH). Install first with install.ps1
setlocal
set "HERE=%~dp0"
set "MODELGRADE_HOME=%HERE:~0,-1%"
if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" -X utf8 "%HERE%tools\modelgrade.py" %*
) else (
  py -3 -X utf8 "%HERE%tools\modelgrade.py" %*
)
exit /b %ERRORLEVEL%
