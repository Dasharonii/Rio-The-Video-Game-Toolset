@echo off
setlocal
if "%~1"=="" (
    echo.
    echo   Drag one or more .edb archives onto this file.
    echo.
    pause
    exit /b 1
)
set "PY=python"
where python >nul 2>&1 || set "PY=py -3"
%PY% "%~dp0tools\rio_extract.py" %*
echo.
pause
