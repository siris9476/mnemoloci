@echo off
rem Mnemoloci plugin launcher for Windows programs that start it without a shell
rem (the MCP server). Hooks run in Git Bash and use mnemoloci-run instead.
setlocal
where python >nul 2>nul
if %errorlevel%==0 (
  python -P "%~dp0launch.py" %*
) else (
  py -3 -P "%~dp0launch.py" %*
)
exit /b %errorlevel%
