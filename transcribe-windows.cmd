@echo off
rem Windows launcher: transcribe-windows "C:\Users\me\Videos\2026-09-24 14-05-02.mkv"
rem Works from cmd.exe and PowerShell; the work happens in windows\launcher.ps1. Going through
rem this .cmd file avoids the PowerShell execution policy, which blocks .ps1 scripts by default.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\launcher.ps1" %*
exit /b %ERRORLEVEL%
