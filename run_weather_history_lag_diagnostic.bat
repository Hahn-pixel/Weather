@echo off
setlocal

cd /d "C:\Users\Dmytro Govor\Documents\Weather"

py ".\weather_history_lag_diagnostic.py"
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [ERROR] Script exited with code %EXITCODE%.
) else (
    echo [DONE] Script finished with code 0.
)
pause
exit /b %EXITCODE%