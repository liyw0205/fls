@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_PATH=%~f0"
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "CURRENT_DIR=%CD%"
set "DEFAULT_PORT=5700"
set "MANAGER_DOWNLOAD_URL=https://github.com/liyw0205/fls/raw/refs/heads/main/fls-manager.py"

call :detect_base_dir
set "MANAGER_FILE=%BASE_DIR%\fls-manager.py"
set "DATA_DIR=%BASE_DIR%\data"
set "LOG_DIR=%BASE_DIR%\log"
set "PID_FILE=%DATA_DIR%\fls-manager.pid"
set "DAEMON_LOG=%LOG_DIR%\fls-manager-daemon.log"
set "VENV_DIR=%BASE_DIR%\.venv"

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%" >nul 2>nul
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul

if "%~1"=="" goto menu
set "CMD=%~1"
shift

if /i "%CMD%"=="start" goto do_start
if /i "%CMD%"=="stop" goto do_stop
if /i "%CMD%"=="restart" goto do_restart
if /i "%CMD%"=="status" goto do_status
if /i "%CMD%"=="log" goto do_log
if /i "%CMD%"=="logs" goto do_log
if /i "%CMD%"=="menu" goto menu
if /i "%CMD%"=="ensure-manager" goto do_ensure_manager
if /i "%CMD%"=="download-manager" goto do_ensure_manager
if /i "%CMD%"=="-h" goto usage
if /i "%CMD%"=="--help" goto usage
if /i "%CMD%"=="help" goto usage

call :err 未知命令：%CMD%
goto usage

:say
echo [FLS] %*
exit /b 0

:err
echo [FLS][ERROR] %* 1>&2
exit /b 0

:detect_base_dir
if not "%FLS_BASE_DIR%"=="" (
  set "BASE_DIR=%FLS_BASE_DIR%"
  exit /b 0
)
if exist "%SCRIPT_DIR%\fls-manager.py" (
  set "BASE_DIR=%SCRIPT_DIR%"
  exit /b 0
)
if exist "%CURRENT_DIR%\fls-manager.py" (
  set "BASE_DIR=%CURRENT_DIR%"
  exit /b 0
)
if not "%USERPROFILE%"=="" (
  set "BASE_DIR=%USERPROFILE%\fls"
  exit /b 0
)
set "BASE_DIR=%SCRIPT_DIR%"
exit /b 0

:find_python
set "PY_SYS="
where python >nul 2>nul && (
  set "PY_SYS=python"
  exit /b 0
)
where py >nul 2>nul && (
  set "PY_SYS=py -3"
  exit /b 0
)
call :err 未找到 python / py -3
exit /b 1

:download_manager
if not exist "%BASE_DIR%" mkdir "%BASE_DIR%" >nul 2>nul
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%" >nul 2>nul
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
call :say 未找到 fls-manager.py，准备自动下载到工作目录：%BASE_DIR%
call :say 下载地址：%MANAGER_DOWNLOAD_URL%
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri '%MANAGER_DOWNLOAD_URL%' -OutFile '%MANAGER_FILE%' } catch { exit 1 }"
if errorlevel 1 (
  call :err 下载失败：请检查网络，或手动下载 fls-manager.py 到 %BASE_DIR%
  call :err 手动下载地址：%MANAGER_DOWNLOAD_URL%
  exit /b 1
)
call :validate_manager_file
if errorlevel 1 (
  call :err 下载到的内容不是有效的 Python 脚本
  exit /b 1
)
call :say fls-manager.py 下载完成：%MANAGER_FILE%
exit /b 0

:need_manager
if not exist "%MANAGER_FILE%" call :download_manager
if not exist "%MANAGER_FILE%" (
  call :err 未找到 %MANAGER_FILE%
  exit /b 1
)
exit /b 0

:validate_manager_file
call :find_python
if errorlevel 1 exit /b 1
cmd /c ""%PY_SYS%" -m py_compile "%MANAGER_FILE%"" >nul 2>nul
exit /b %ERRORLEVEL%

:try_install_git
call :say 未找到 git，尝试自动安装...
where winget >nul 2>nul || exit /b 1
winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
exit /b %ERRORLEVEL%

:ensure_git
where git >nul 2>nul && exit /b 0
call :try_install_git
where git >nul 2>nul && (
  call :say git 自动安装成功
  exit /b 0
)
call :err 未找到 git，且自动安装失败
call :err 请手动安装 Git 后再启动 FLS Manager（脚本管理中的拉取仓库功能依赖 git）
exit /b 1

:ensure_python_env
call :need_manager
if errorlevel 1 exit /b 1
call :find_python
if errorlevel 1 exit /b 1
if exist "%VENV_DIR%\Scripts\python.exe" (
  set "PY_BIN=%VENV_DIR%\Scripts\python.exe"
  exit /b 0
)
cmd /c ""%PY_SYS%" -m venv "%VENV_DIR%"" >nul 2>nul
if exist "%VENV_DIR%\Scripts\python.exe" (
  set "PY_BIN=%VENV_DIR%\Scripts\python.exe"
  call "%PY_BIN%" -m pip install --upgrade pip >nul 2>nul
  exit /b 0
)
for /f "delims=" %%i in ('where python 2^>nul') do (
  set "PY_BIN=%%i"
  goto ensure_python_env_done
)
set "PY_BIN=python"
:ensure_python_env_done
exit /b 0

:install_basic_deps
set "PY_TO_USE=%~1"
call "%PY_TO_USE%" -c "import importlib.util;mods=['flask','requests','apscheduler'];raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)" >nul 2>nul
if not errorlevel 1 exit /b 0
call :say 检测到基础依赖可能缺失，尝试安装...
call "%PY_TO_USE%" -m pip install flask requests apscheduler PySocks tzdata setproctitle >nul 2>nul
exit /b 0

:parse_start_opts
set "TEMP_TOKEN="
set "TEMP_PORT="
:parse_start_opts_loop
if "%~1"=="" exit /b 0
if /i "%~1"=="-t" goto parse_opt_token
if /i "%~1"=="--token" goto parse_opt_token
if /i "%~1"=="-p" goto parse_opt_port
if /i "%~1"=="--port" goto parse_opt_port
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
call :err 未知参数：%~1
goto usage_error

:parse_opt_token
shift
if "%~1"=="" (
  call :err -t / --token 后面需要填写密钥
  exit /b 1
)
set "TEMP_TOKEN=%~1"
shift
goto parse_start_opts_loop

:parse_opt_port
shift
if "%~1"=="" (
  call :err -p / --port 后面需要填写端口
  exit /b 1
)
set "TEMP_PORT=%~1"
echo %TEMP_PORT%| findstr /r "^[0-9][0-9]*$" >nul || (
  call :err 端口必须是数字：%TEMP_PORT%
  exit /b 1
)
shift
goto parse_start_opts_loop

:do_start
call :parse_start_opts %*
if errorlevel 1 exit /b 1
call :need_manager
if errorlevel 1 exit /b 1
call :ensure_git
if errorlevel 1 exit /b 1
call :ensure_python_env
if errorlevel 1 exit /b 1
call :install_basic_deps "%PY_BIN%"

set "FLS_BASE_DIR=%BASE_DIR%"
set "FLS_PYTHON=%PY_BIN%"
set "FLS_TOKEN="
set "FLS_PORT="
if not "%TEMP_TOKEN%"=="" (
  set "FLS_TOKEN=%TEMP_TOKEN%"
  call :say 本次启动使用临时密钥：已设置
)
if not "%TEMP_PORT%"=="" (
  set "FLS_PORT=%TEMP_PORT%"
  call :say 本次启动使用临时端口：%TEMP_PORT%
)

call :say 启动 FLS Manager...
call :say 日志文件：%DAEMON_LOG%
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:FLS_BASE_DIR='%BASE_DIR%'; $env:FLS_PYTHON='%PY_BIN%'; if ('%TEMP_TOKEN%' -ne '') { $env:FLS_TOKEN='%TEMP_TOKEN%' } else { Remove-Item Env:FLS_TOKEN -ErrorAction SilentlyContinue }; if ('%TEMP_PORT%' -ne '') { $env:FLS_PORT='%TEMP_PORT%' } else { Remove-Item Env:FLS_PORT -ErrorAction SilentlyContinue }; $p = Start-Process -FilePath '%PY_BIN%' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%MANAGER_FILE%') -WorkingDirectory '%BASE_DIR%' -RedirectStandardOutput '%DAEMON_LOG%' -RedirectStandardError '%DAEMON_LOG%' -PassThru -WindowStyle Hidden; Set-Content -Path '%PID_FILE%' -Value $p.Id"
timeout /t 2 >nul
if exist "%PID_FILE%" (
  set /p RUN_PID=<"%PID_FILE%"
  tasklist /FI "PID eq %RUN_PID%" | findstr /I "%RUN_PID%" >nul && (
    call :say 启动成功，PID: %RUN_PID%
    if not "%TEMP_PORT%"=="" (
      call :say 访问地址：http://服务器IP:%TEMP_PORT%
    ) else (
      call :say 访问地址：http://服务器IP:%DEFAULT_PORT%
    )
    exit /b 0
  )
)
call :err 启动失败，请查看日志：%DAEMON_LOG%
if exist "%DAEMON_LOG%" powershell -NoProfile -Command "Get-Content -Tail 80 '%DAEMON_LOG%'"
exit /b 1

:do_stop
if exist "%PID_FILE%" (
  set /p OLD_PID=<"%PID_FILE%"
  if not "%OLD_PID%"=="" (
    taskkill /PID %OLD_PID% /T /F >nul 2>nul
  )
  del /f /q "%PID_FILE%" >nul 2>nul
  call :say 已停止
  exit /b 0
)
call :say FLS Manager 未运行
exit /b 0

:do_restart
call :parse_start_opts %*
if errorlevel 1 exit /b 1
set "SAVED_TOKEN=%TEMP_TOKEN%"
set "SAVED_PORT=%TEMP_PORT%"
call :do_stop
if not "%SAVED_TOKEN%"=="" (
  if not "%SAVED_PORT%"=="" (
    call :do_start -t "%SAVED_TOKEN%" -p "%SAVED_PORT%"
  ) else (
    call :do_start -t "%SAVED_TOKEN%"
  )
) else (
  if not "%SAVED_PORT%"=="" (
    call :do_start -p "%SAVED_PORT%"
  ) else (
    call :do_start
  )
)
exit /b %ERRORLEVEL%

:do_status
echo ====================================================
echo FLS Manager 状态
echo 工作目录：%BASE_DIR%
echo PID 文件：%PID_FILE%
echo 日志文件：%DAEMON_LOG%
if exist "%PID_FILE%" (
  set /p SPID=<"%PID_FILE%"
  tasklist /FI "PID eq %SPID%" | findstr /I "%SPID%" >nul && (
    echo 运行状态：运行中
    echo PID：%SPID%
    echo ====================================================
    exit /b 0
  )
)
echo 运行状态：未运行
echo ====================================================
exit /b 0

:do_log
if not exist "%DAEMON_LOG%" type nul > "%DAEMON_LOG%"
call :say 日志文件：%DAEMON_LOG%
powershell -NoProfile -Command "Get-Content -Path '%DAEMON_LOG%' -Wait -Tail 200"
exit /b 0

:do_ensure_manager
call :need_manager
if errorlevel 1 exit /b 1
call :say fls-manager.py 已就绪：%MANAGER_FILE%
exit /b 0

:menu
echo.
echo ====================================================
echo FLS Manager 菜单
echo ====================================================
echo 1. 启动
echo 2. 停止
echo 3. 重启
echo 4. 状态
echo 5. 查看实时日志
echo 0. 退出
echo ====================================================
set /p CHOICE=请选择：
if "%CHOICE%"=="1" call :do_start & goto menu
if "%CHOICE%"=="2" call :do_stop & goto menu
if "%CHOICE%"=="3" call :do_restart & goto menu
if "%CHOICE%"=="4" call :do_status & goto menu
if "%CHOICE%"=="5" call :do_log & goto menu
if "%CHOICE%"=="0" exit /b 0
echo 无效选择
goto menu

:usage
echo 用法：
echo   fls.bat start [-t 密钥] [-p 端口]
echo   fls.bat stop
echo   fls.bat restart [-t 密钥] [-p 端口]
echo   fls.bat status
echo   fls.bat log
echo   fls.bat menu
echo   fls.bat ensure-manager
exit /b 0

:usage_error
exit /b 1