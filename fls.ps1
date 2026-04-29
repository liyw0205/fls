$ErrorActionPreference = "Stop"

$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDir = Split-Path -Parent $ScriptPath
$CurrentDir = (Get-Location).Path
$DefaultPort = "5700"
$ManagerDownloadUrl = "https://github.com/liyw0205/fls/raw/refs/heads/main/fls-manager.py"

function Say($msg) {
    Write-Host "[FLS] $msg"
}

function ErrMsg($msg) {
    Write-Host "[FLS][ERROR] $msg" -ForegroundColor Red
}

function Detect-BaseDir {
    if ($env:FLS_BASE_DIR) {
        return $env:FLS_BASE_DIR
    }

    if (Test-Path (Join-Path $ScriptDir "fls-manager.py")) {
        return $ScriptDir
    }

    if (Test-Path (Join-Path $CurrentDir "fls-manager.py")) {
        return $CurrentDir
    }

    if ($env:USERPROFILE) {
        return (Join-Path $env:USERPROFILE "fls")
    }

    return $ScriptDir
}

$BaseDir = Detect-BaseDir
$ManagerFile = Join-Path $BaseDir "fls-manager.py"
$DataDir = Join-Path $BaseDir "data"
$LogDir = Join-Path $BaseDir "log"
$PidFile = Join-Path $DataDir "fls-manager.pid"
$DaemonLog = Join-Path $LogDir "fls-manager-daemon.log"
$VenvDir = Join-Path $BaseDir ".venv"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Find-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return "python" }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return "py -3" }

    throw "未找到 python / py -3"
}

function Invoke-PythonCommand($CommandLine) {
    cmd /c $CommandLine
    return $LASTEXITCODE
}

function Validate-ManagerFile {
    if (-not (Test-Path $ManagerFile)) { return $false }
    $py = Find-Python
    $code = Invoke-PythonCommand "`"$py`" -m py_compile `"$ManagerFile`""
    return ($code -eq 0)
}

function Download-Manager {
    New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
    Say "未找到 fls-manager.py，准备自动下载到工作目录：$BaseDir"
    Say "下载地址：$ManagerDownloadUrl"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $ManagerDownloadUrl -OutFile $ManagerFile
    } catch {
        ErrMsg "下载失败：请检查网络，或手动下载 fls-manager.py 到 $BaseDir"
        ErrMsg "手动下载地址：$ManagerDownloadUrl"
        exit 1
    }

    if (-not (Validate-ManagerFile)) {
        ErrMsg "下载到的内容不是有效的 Python 脚本"
        exit 1
    }

    Say "fls-manager.py 下载完成：$ManagerFile"
}

function Need-Manager {
    if (-not (Test-Path $ManagerFile)) {
        Download-Manager
    }

    if (-not (Test-Path $ManagerFile)) {
        ErrMsg "未找到 $ManagerFile"
        exit 1
    }
}

function Try-InstallGit {
    Say "未找到 git，尝试自动安装..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        return $false
    }

    try {
        & winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
        return $true
    } catch {
        return $false
    }
}

function Ensure-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) { return }

    $ok = Try-InstallGit
    $git2 = Get-Command git -ErrorAction SilentlyContinue
    if ($ok -and $git2) {
        Say "git 自动安装成功"
        return
    }

    ErrMsg "未找到 git，且自动安装失败"
    ErrMsg "请手动安装 Git 后再启动 FLS Manager（脚本管理中的拉取仓库功能依赖 git）"
    exit 1
}

function Ensure-PythonEnv {
    Need-Manager
    $py = Find-Python
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"

    if (Test-Path $venvPython) {
        return $venvPython
    }

    try {
        cmd /c "`"$py`" -m venv `"$VenvDir`"" | Out-Null
    } catch {}

    if (Test-Path $venvPython) {
        try {
            & $venvPython -m pip install --upgrade pip | Out-Null
        } catch {}
        return $venvPython
    }

    return "python"
}

function Install-BasicDeps($PyBin) {
    try {
        & $PyBin -c "import importlib.util;mods=['flask','requests','apscheduler'];raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)" | Out-Null
        if ($LASTEXITCODE -eq 0) { return }
    } catch {}

    Say "检测到基础依赖可能缺失，尝试安装..."
    try { & $PyBin -m pip install flask requests apscheduler PySocks tzdata setproctitle | Out-Null } catch {}
}

function Parse-StartOpts([string[]]$ArgsList) {
    $result = @{
        Token = ""
        Port  = ""
    }

    for ($i = 0; $i -lt $ArgsList.Count; $i++) {
        $arg = $ArgsList[$i]
        switch -Regex ($arg) {
            '^(-t|--token)$' {
                $i++
                if ($i -ge $ArgsList.Count) { throw "-t / --token 后面需要填写密钥" }
                $result.Token = $ArgsList[$i]
            }
            '^(-p|--port)$' {
                $i++
                if ($i -ge $ArgsList.Count) { throw "-p / --port 后面需要填写端口" }
                if ($ArgsList[$i] -notmatch '^\d+$') { throw "端口必须是数字：$($ArgsList[$i])" }
                $result.Port = $ArgsList[$i]
            }
            '^(-h|--help)$' {
                Show-Usage
                exit 0
            }
            default {
                throw "未知参数：$arg"
            }
        }
    }

    return $result
}

function Start-Fls([string[]]$ArgsList) {
    try {
        $opts = Parse-StartOpts $ArgsList
    } catch {
        ErrMsg $_.Exception.Message
        exit 1
    }

    Need-Manager
    Ensure-Git
    $py = Ensure-PythonEnv
    Install-BasicDeps $py

    $env:FLS_BASE_DIR = $BaseDir
    $env:FLS_PYTHON = $py

    if ($opts.Token) {
        $env:FLS_TOKEN = $opts.Token
        Say "本次启动使用临时密钥：已设置"
    } else {
        Remove-Item Env:FLS_TOKEN -ErrorAction SilentlyContinue
    }

    if ($opts.Port) {
        $env:FLS_PORT = $opts.Port
        Say "本次启动使用临时端口：$($opts.Port)"
    } else {
        Remove-Item Env:FLS_PORT -ErrorAction SilentlyContinue
    }

    Say "启动 FLS Manager..."
    Say "日志文件：$DaemonLog"

    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $ManagerFile
    )

    $proc = Start-Process -FilePath $py `
        -ArgumentList $argList `
        -WorkingDirectory $BaseDir `
        -RedirectStandardOutput $DaemonLog `
        -RedirectStandardError $DaemonLog `
        -PassThru `
        -WindowStyle Hidden

    Set-Content -Path $PidFile -Value $proc.Id

    Start-Sleep -Seconds 2

    try {
        $check = Get-Process -Id $proc.Id -ErrorAction Stop
        Say "启动成功，PID: $($proc.Id)"
        if ($opts.Port) {
            Say "访问地址：http://服务器IP:$($opts.Port)"
        } else {
            Say "访问地址：http://服务器IP:$DefaultPort"
        }
    } catch {
        ErrMsg "启动失败，请查看日志：$DaemonLog"
        if (Test-Path $DaemonLog) {
            Get-Content -Tail 80 $DaemonLog
        }
        exit 1
    }
}

function Stop-Fls {
    if (Test-Path $PidFile) {
        $pid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($pid) {
            try {
                Stop-Process -Id ([int]$pid) -Force -ErrorAction SilentlyContinue
            } catch {}
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Say "已停止"
        return
    }

    Say "FLS Manager 未运行"
}

function Restart-Fls([string[]]$ArgsList) {
    Stop-Fls
    Start-Fls $ArgsList
}

function Status-Fls {
    Write-Host "===================================================="
    Write-Host "FLS Manager 状态"
    Write-Host "工作目录：$BaseDir"
    Write-Host "PID 文件：$PidFile"
    Write-Host "日志文件：$DaemonLog"

    if (Test-Path $PidFile) {
        $pid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($pid) {
            try {
                Get-Process -Id ([int]$pid) -ErrorAction Stop | Out-Null
                Write-Host "运行状态：运行中"
                Write-Host "PID：$pid"
                Write-Host "===================================================="
                return
            } catch {}
        }
    }

    Write-Host "运行状态：未运行"
    Write-Host "===================================================="
}

function Tail-Log {
    if (-not (Test-Path $DaemonLog)) {
        New-Item -ItemType File -Force -Path $DaemonLog | Out-Null
    }
    Say "日志文件：$DaemonLog"
    Get-Content -Path $DaemonLog -Wait -Tail 200
}

function Show-Usage {
@"
用法：
  .\fls.ps1 start [-t 密钥] [-p 端口]
  .\fls.ps1 stop
  .\fls.ps1 restart [-t 密钥] [-p 端口]
  .\fls.ps1 status
  .\fls.ps1 log
  .\fls.ps1 menu
  .\fls.ps1 ensure-manager
"@ | Write-Host
}

function Show-Menu {
    while ($true) {
        Write-Host ""
        Write-Host "===================================================="
        Write-Host "FLS Manager 菜单"
        Write-Host "===================================================="
        Write-Host "1. 启动"
        Write-Host "2. 停止"
        Write-Host "3. 重启"
        Write-Host "4. 状态"
        Write-Host "5. 查看实时日志"
        Write-Host "0. 退出"
        Write-Host "===================================================="
        $choice = Read-Host "请选择"

        switch ($choice) {
            "1" { Start-Fls @() }
            "2" { Stop-Fls }
            "3" { Restart-Fls @() }
            "4" { Status-Fls }
            "5" { Tail-Log }
            "0" { break }
            default { Write-Host "无效选择" }
        }
    }
}

param(
    [Parameter(Position=0)]
    [string]$Cmd = "menu",

    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Rest
)

switch ($Cmd.ToLower()) {
    "start" { Start-Fls $Rest }
    "stop" { Stop-Fls }
    "restart" { Restart-Fls $Rest }
    "status" { Status-Fls }
    "log" { Tail-Log }
    "logs" { Tail-Log }
    "menu" { Show-Menu }
    "ensure-manager" { Need-Manager; Say "fls-manager.py 已就绪：$ManagerFile" }
    "download-manager" { Need-Manager; Say "fls-manager.py 已就绪：$ManagerFile" }
    "-h" { Show-Usage }
    "--help" { Show-Usage }
    "help" { Show-Usage }
    default {
        ErrMsg "未知命令：$Cmd"
        Show-Usage
        exit 1
    }
}