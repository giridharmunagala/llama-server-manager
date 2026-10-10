@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 app.py --open-browser %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finished

:use_python
where python >nul 2>nul
if errorlevel 1 goto python_missing
python app.py --open-browser %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finished

:python_missing
echo Error: Python 3 was not found. Install Python 3.10 or newer and try again.
set "EXIT_CODE=1"

:finished
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
