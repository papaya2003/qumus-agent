@echo off
rem Shared body of every "Start Qumus.bat" / "Stop Qumus.bat" (Windows), the
rem counterpart of scripts/qumus_launcher.zsh. They work by double-click or in
rem any terminal; all of them run scripts\start_qumus.py of this repository.
setlocal
for %%I in ("%~dp0..") do set "QUMUS_REPO=%%~fI"
set "QUMUS_PYTHON=%QUMUS_REPO%\.venv\Scripts\python.exe"
rem Files, logs and the console are UTF-8 whatever the Windows code page (GBK).
set "PYTHONUTF8=1"
if not exist "%QUMUS_PYTHON%" (
  echo Qumus environment is missing. In the repository folder run:  py -3 scripts\setup_qumus.py
  pause
  exit /b 1
)
"%QUMUS_PYTHON%" "%QUMUS_REPO%\scripts\start_qumus.py" %*
set "QUMUS_EXIT=%ERRORLEVEL%"
if not "%QUMUS_EXIT%"=="0" (
  echo.
  echo Read the message above, then press a key to close.
  pause >nul
) else (
  rem leave the message readable for a moment after a double-click
  timeout /t 5 /nobreak >nul 2>&1
)
exit /b %QUMUS_EXIT%
