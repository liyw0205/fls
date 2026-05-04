@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "PS1_FILE=%SCRIPT_DIR%\fls.ps1"

if not exist "%PS1_FILE%" (
    echo [FLS][ERROR] 未找到 fls.ps1：%PS1_FILE%
    echo 请把 fls.bat 和 fls.ps1 放在同一个目录。
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_FILE%" %*

exit /b %ERRORLEVEL%