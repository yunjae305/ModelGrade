@echo off
rem Old name of aiona-chat-eval.cmd (same program) - use: aiona-chat-eval start
setlocal
set "HERE=%~dp0"
set "MODELGRADE_HOME=%HERE:~0,-1%"
if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" -X utf8 "%HERE%tools\modelgrade.py" %*
) else (
  py -3 -X utf8 "%HERE%tools\modelgrade.py" %*
)
exit /b %ERRORLEVEL%
