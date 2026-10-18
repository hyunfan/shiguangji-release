@echo off
setlocal
cd /d "%~dp0"
title Install optional video tools
echo Install optional FFmpeg tools for segmented video recovery.
echo Files are downloaded from www.gyan.dev and checked with SHA-256.
echo This only writes tools\ffmpeg in this application folder.
if exist "%~dp0python\python.exe" goto bundled
where py >nul 2>nul
if errorlevel 1 goto system_python
py -3 -X utf8 -u "%~dp0setup_ffmpeg.py"
goto finished
:bundled
"%~dp0python\python.exe" -X utf8 -u "%~dp0setup_ffmpeg.py"
goto finished
:system_python
python -X utf8 -u "%~dp0setup_ffmpeg.py"
:finished
set "component_exit=%errorlevel%"
if not "%component_exit%"=="0" echo Installation failed. See details above. Existing archives are unchanged.
echo Press any key to close.
pause >nul
exit /b %component_exit%
