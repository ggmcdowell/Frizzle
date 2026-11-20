@echo off
rem Flight.cmd: double-click it on Windows (or run it from cmd or PowerShell) to start Flight.command in Git for Windows' bash.
rem Arguments go on to Flight.command, and from it to claude.
setlocal
set "GITBASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined GITBASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "GITBASH=%LocalAppData%\Programs\Git\bin\bash.exe"
rem Else derive it from git on the PATH: <Git>\cmd\git.exe is beside <Git>\bin\bash.exe.
if not defined GITBASH for /f "delims=" %%G in ('where git 2^>nul') do if not defined GITBASH if exist "%%~dpG..\bin\bash.exe" set "GITBASH=%%~dpG..\bin\bash.exe"
if not defined GITBASH (
  echo Git for Windows is not installed on this computer, or its bash.exe was not found.
  echo Install it with: winget install --id Git.Git -e
  echo Then double-click this file again.
  pause
  exit /b 1
)
cd /d "%~dp0"
"%GITBASH%" ./Flight.command %*
exit /b %ERRORLEVEL%
