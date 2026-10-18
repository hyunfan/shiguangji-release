@echo off
setlocal
title X Wayback Library
cd /d "%~dp0"
echo X Wayback Library
echo The launcher will detect the active local address.
echo Saving progress and stopping the background server. Please wait.
echo.
if not exist "%~dp0app.py" goto missing_files
if not exist "%~dp0launcher.py" goto missing_files
if exist "%~dp0portable.json" goto bundled_python
if exist "%~dp0python" goto bundled_python
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto try_python
py -3 -X utf8 -u "%~dp0launcher.py" --stop %*
set "app_exit=%errorlevel%"
goto finished

:bundled_python
if not exist "%~dp0python\python.exe" goto broken_bundle
if not exist "%~dp0python\pythonw.exe" goto broken_bundle
"%~dp0python\python.exe" -X utf8 -c "import sys, ssl, sqlite3, ctypes; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto broken_bundle
"%~dp0python\python.exe" -X utf8 -u "%~dp0launcher.py" --stop %*
set "app_exit=%errorlevel%"
goto finished

:broken_bundle
echo The bundled Python runtime is missing or could not start.
echo Extract the entire Windows x64 portable ZIP to a writable local folder.
echo Keep the python folder beside this launcher. Do not run from inside the ZIP.
set "app_exit=1"
goto finished

:try_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto missing_python
python -X utf8 -u "%~dp0launcher.py" --stop %*
set "app_exit=%errorlevel%"
goto finished

:missing_python
echo Python 3.10 or newer was not found or could not run.
echo Install Python from https://www.python.org/downloads/windows/
echo Enable "Add python.exe to PATH", then run this launcher again.
set "app_exit=1"
goto finished

:missing_files
echo app.py or launcher.py was not found beside this launcher.
echo Extract the entire ZIP before starting the tool.
set "app_exit=1"

:finished
if "%app_exit%"=="0" exit /b 0
echo.
echo Startup failed. The error details are shown above.
echo Startup logs are in the runtime folder beside this launcher.
echo The active port is recorded in runtime\active-port.json.
echo Press any key to close this window.
pause >nul
exit /b %app_exit%
