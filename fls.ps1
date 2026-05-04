param(
    [Parameter(Position = 0)]
    [string]$Cmd = "menu",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:FLS_REPO_URL) { $env:FLS_REPO_URL } else { "https://github.com/liyw0205/fls.git" }
$RepoBranch = if ($env:FLS_REPO_BRANCH) { $env:FLS_REPO_BRANCH } else { "main" }

$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDir = Split-Path -Parent $ScriptPath
$CurrentDir = (Get-Location).Path
$DefaultPort = "5700"

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

    if (
        (Test-Path (Join-Path $ScriptDir "fls-manager.py")) -and
        (Test-Path (Join-Path $ScriptDir "fls_manager"))
    ) {
        return $ScriptDir
    }

    if (
        (Test-Path (Join-Path $CurrentDir "fls-manager.py")) -and
        (Test-Path (Join-Path $CurrentDir "fls_manager"))
    ) {
        return $CurrentDir
    }

    if ($env:USERPROFILE) {
        return (Join-Path $env:USERPROFILE "fls")
    }

    return $ScriptDir
}

$BaseDir = Detect-BaseDir
$ManagerFile = Join-Path $BaseDir "fls-manager.py"
$PackageDir = Join-Path $BaseDir "fls_manager"
$DataDir = Join-Path $BaseDir "data"
$LogDir = Join-Path $BaseDir "log"
$PidFile = Join-Path $DataDir "fls-manager.pid"
$DaemonLog = Join-Path $LogDir "fls-manager-daemon.log"
$VenvDir = Join-Path $BaseDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Repo-Ready {
    return (
        (Test-Path $ManagerFile) -and
        (Test-Path $PackageDir)
    )
}

function Ensure-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        return
    }

    Say "未找到 Git，尝试使用 winget 安装..."

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
        } catch {}
    }

    $git2 = Get-Command git -ErrorAction SilentlyContinue
    if ($git2) {
        Say "Git 已安装"
        return
    }

    ErrMsg "未找到 Git，请手动安装 Git 后重试"
    exit 1
}

function Clone-Repo-ToDir($Target) {
    $Parent = Split-Path -Parent $Target
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null

    if (-not (Test-Path $Target)) {
        Say "准备克隆 FLS 仓库到：$Target"
        git clone --depth 1 -b $RepoBranch $RepoUrl $Target
        if ($LASTEXITCODE -ne 0) {
            throw "git clone 失败"
        }
        return
    }

    $Tmp = "$Target.clone.$PID"

    if (Test-Path $Tmp) {
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    }

    Say "工作目录已存在，先克隆到临时目录：$Tmp"

    git clone --depth 1 -b $RepoBranch $RepoUrl $Tmp
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
        throw "git clone 失败"
    }

    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    Copy-Item -Path (Join-Path $Tmp "*") -Destination $Target -Recurse -Force
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

function Ensure-Repo {
    Ensure-Git

    if (Repo-Ready) {
        return
    }

    Say "未检测到完整模块化 FLS 程序"
    "Branch        {
   not (Repo-Ready)) {
        ErrMsg "仓库拉取完成，但未找到 fls-manager.py 或 fls_manager 目录"
        exit 1
    }

    Say "FLS 仓库已就绪：$BaseDir"
}

function Find-PythonSpec {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            File = "python"
            Args = @()
            Text = "python"
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @{
            File = "py"
            Args = @("-3")
            Text = "py -3"
        }
    }

    throw "未找到 python 或 py -3"
}

function Run-PythonSpec($Spec, [string[]]$ArgsList) {
    & $Spec.File @($Spec.Args + $ArgsList)
    return $LASTEXITCODE
}

function Ensure-PythonEnv {
    Ensure-Repo

    if (Test-Path $VenvPython) {
        return @{
            File = $VenvPython
            Args = @()
            Text = $VenvPython
        }
    }

    try {
        $spec = Find-PythonSpec
    } catch {
        ErrMsg $_.Exception.Message
        exit 1
    }

    try {
        Run-PythonSpec $spec @("-m", "venv", $VenvDir) | Out-Null
    } catch {}

    if (Test-Path $VenvPython) {
        try {
            & $VenvPython -m pip install --upgrade pip | Out-Null
        } catch {}

        return @{
            File = $VenvPython
            Args = @()
            Text = $VenvPython
        }
    }

    return $spec
}

function Validate-PythonFiles($PySpec) {
    if (-not (Test-Path $ManagerFile)) {
        ErrMsg "未找到 $ManagerFile"
        exit 1
    }

    try {
        Run-PythonSpec $PySpec @("-m", "py_compile", $ManagerFile) | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "py_compile failed"
        }
    } catch {
        ErrMsg "fls-manager.py 语法检查失败"
        exit 1
    }
}

function Install-BasicDeps($PySpec) {
    try {
        Run-PythonSpec $PySpec @(
            "-c",
            "import importlib.util;mods=['flask','requests','apscheduler','socks'];raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)"
        ) | Out-Null

        if ($LASTEXITCODE -eq 0) {
            return
        }
    } catch {}

    Say "检测到基础依赖缺失，尝试安装..."

    try {
        Run-PythonSpec $PySpec @(
            "-m", "pip", "install",
            "flask", "requests", "apscheduler", "PySocks", "tzdata", "setproctitle"
        ) | Out-Null
    } catch {}
}

function Read-ConfigValue($Key, $DefaultValue) {
    $ConfigFile = Join-Path $DataDir "config.json"

    if (-not (Test-Path $ConfigFile)) {
        return $DefaultValue
    }

    try {
        $json = Get-Content $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $value = $json.$Key
        if ($null -eq $value -or "$value" -eq "") {
            return $DefaultValue
        }
        return "$value"
    } catch {
        return $DefaultValue
    }
}

function Parse-StartOpts([string[]]$ArgsList) {
    $result = @{
        Token = ""
        Port = ""
    }

    for ($i = 0; $i -lt $ArgsList.Count; $i++) {
        $arg = $ArgsList[$i]

        switch ($arg.ToLower()) {
            "-t" {
                $i++
                if ($i -ge $ArgsList.Count) {
                    throw "-t 后面需要填写 Token"
                }
                $result.Token = $ArgsList[$i]
            }
            "--token" {
                $i++
                if ($i -ge $ArgsList.Count) {
                    throw "--token 后面需要填写 Token"
                }
                $result.Token = $ArgsList[$i]
            }
            "-p" {
                $i++
                if ($i -ge $ArgsList.Count) {
                    throw "-p 后面需要填写端口"
                }
                if ($ArgsList[$i] -notmatch '^\d+$') {
                    throw "端口必须是数字：$($ArgsList[$i])"
                }
                $port = [int]$ArgsList[$i]
                if ($port -lt 1 -or $port -gt 65535) {
                    throw "端口范围必须是 1-65535"
                }
                $result.Port = "$port"
            }
            "--port" {
                $i++
                if ($i -ge $ArgsList.Count) {
                    throw "--port 后面需要填写端口"
                }
                if ($ArgsList[$i] -notmatch '^\d+$') {
                    throw "端口必须是数字：$($ArgsList[$i])"
                }
                $port = [int]$ArgsList[$i]
                if ($port -lt 1 -or $port -gt 65535) {
                    throw "端口范围必须是 1-65535"
                }
                $result.Port = "$port"
            }
            "-h" {
                Show-Usage
                exit 0
            }
            "--help" {
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

function Quote-CmdArg($s) {
    return '"' + ($s -replace '"', '\"') + '"'
}

function Python-CmdLine($PySpec) {
    $items = @()
    $items += Quote-CmdArg $PySpec.File

    foreach ($a in $PySpec.Args) {
        $items += Quote-CmdArg $a
    }

    $items += Quote-CmdArg $ManagerFile

    return ($items -join " ")
}

function Get-PidFromFile {
    if (-not (Test-Path $PidFile)) {
        return ""
    }

    try {
        return ((Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim())
    } catch {
        return ""
    }
}

function Is-RunningPid($PidValue) {
    if (-not $PidValue) {
        return $false
    }

    try {
        Get-Process -Id ([int]$PidValue) -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Start-Fls([string[]]$ArgsList) {
    try {
        $opts = Parse-StartOpts $ArgsList
    } catch {
        ErrMsg $_.Exception.Message
        exit 1
    }

    Ensure-Repo

    $oldPid = Get-PidFromFile
    if (Is-RunningPid $oldPid) {
        Say "FLS Manager 已在运行，PID: $oldPid"
        Say "如需应用新的临时端口/Token，请执行：.\fls.ps1 restart -p 端口 -t Token"
        return
    }

    $py = Ensure-PythonEnv
    Validate-PythonFiles $py
    Install-BasicDeps $py

    $env:FLS_BASE_DIR = $BaseDir
    $env:FLS_PYTHON = $py.Text

    if ($opts.Token) {
        $env:FLS_TOKEN = $opts.Token
        Say "本次启动使用临时 Token：已设置"
    }

    if ($opts.Port) {
        $env:FLS_PORT = $opts.Port
        Say "本次启动使用临时端口：$($opts.Port)"
    }

    Say "启动 FLS Manager..."
    Say "工作目录：$BaseDir"
    Say "日志文件：$DaemonLog"

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

    $pyCmd = Python-CmdLine $py
    $cmdLine = "/c cd /d " + (Quote-CmdArg $BaseDir) + " && " + $pyCmd + " >> " + (Quote-CmdArg $DaemonLog) + " 2>>&1"

    $proc = Start-Process `
        -FilePath "cmd.exe" `
        -ArgumentList $cmdLine `
        -WorkingDirectory $BaseDir `
        -PassThru `
        -WindowStyle Hidden

    Set-Content -Path $PidFile -Value $proc.Id

    Start-Sleep -Seconds 2

    if (Is-RunningPid $proc.Id) {
        $portShow = if ($opts.Port) { $opts.Port } else { Read-ConfigValue "port" $DefaultPort }
        Say "启动成功，PID: $($proc.Id)"
        Say "访问地址：http://服务器IP:$portShow"
        Say "如首次使用，请访问面板完成 Token 设置"
        return
    }

    ErrMsg "启动失败，请查看日志：$DaemonLog"

    if (Test-Path $DaemonLog) {
        Get-Content -Tail 100 $DaemonLog
    }

    exit 1
}

function Stop-Fls {
    $pidValue = Get-PidFromFile

    if ($pidValue) {
        try {
            taskkill /PID $pidValue /T /F | Out-Null
        } catch {}

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
    $cfgPort = Read-ConfigValue "port" $DefaultPort
    $cfgToken = Read-ConfigValue "admin_token" ""
    $pidValue = Get-PidFromFile

    Write-Host "===================================================="
    Write-Host "FLS Manager 状态"
    Write-Host "仓库地址：$RepoUrl"
    Write-Host "工作目录：$BaseDir"
    Write-Host "主文件：$ManagerFile"
    Write-Host "模块目录：$PackageDir"
    Write-Host "PID 文件：$PidFile"
    Write-Host "日志文件：$DaemonLog"
    Write-Host "配置端口：$cfgPort"

    if ($cfgToken) {
        Write-Host "配置 Token：已设置"
    } else {
        Write-Host "配置 Token：未设置"
    }

    if (Repo-Ready) {
        Write-Host "程序文件：完整"
    } else {
        Write-Host "程序文件：不完整"
    }

    if (Is-RunningPid $pidValue) {
        Write-Host "运行状态：运行中"
        Write-Host "PID：$pidValue"
    } else {
        Write-Host "运行状态：未运行"
    }

    Write-Host "===================================================="
}

function Tail-Log {
    if (-not (Test-Path $DaemonLog)) {
        New-Item -ItemType File -Force -Path $DaemonLog | Out-Null
    }

    Say "日志文件：$DaemonLog"
    Get-Content -Path $DaemonLog -Wait -Tail 200
}

function Update-Repo {
    Ensure-Git

    if (-not (Test-Path (Join-Path $BaseDir ".git"))) {
        Say "当前目录不是 Git 仓库，将重新拉取覆盖程序文件"

        try {
            Clone-Repo-ToDir $BaseDir
            Say "更新完成"
            return
        } catch {
            ErrMsg $_.Exception.Message
            exit 1
        }
    }

    Say "准备更新 FLS 仓库..."
    Push-Location $BaseDir

    try {
        git fetch --all --prune
        git checkout $RepoBranch | Out-Null

        git pull --ff-only origin $RepoBranch
        if ($LASTEXITCODE -ne 0) {
            git pull origin $RepoBranch
        }

        Say "更新完成"
        Say "如果面板正在运行，请执行：.\fls.ps1 restart"
    } finally {
        Pop-Location
    }
}

function Ensure-RepoCommand {
    Ensure-Repo
    Say "FLS 仓库已就绪：$BaseDir"
}

function Show-Usage {
@"
FLS Manager

命令：
  start [-p 端口] [-t Token]   启动
  stop                         停止
  restart [-p 端口] [-t Token] 重启
  status                       状态
  log                          日志
  update                       更新
  ensure-repo                  检查/拉取程序
  menu                         主页菜单

示例：
  .\fls.ps1 start
  .\fls.ps1 restart -p 5701 -t 123456
"@ | Write-Host
}

function Show-Menu {
    while ($true) {
        Write-Host ""
        Write-Host "===================================================="
        Write-Host "FLS Manager"
        Write-Host "===================================================="
        Write-Host "1. 启动"
        Write-Host "2. 停止"
        Write-Host "3. 重启"
        Write-Host "4. 状态"
        Write-Host "5. 查看实时日志"
        Write-Host "6. 更新程序"
        Write-Host "7. 检查/拉取程序"
        Write-Host "0. 退出"
        Write-Host "===================================================="

        $choice = Read-Host "请选择"

        switch ($choice) {
            "1" { Start-Fls @() }
            "2" { Stop-Fls }
            "3" { Restart-Fls @() }
            "4" { Status-Fls }
            "5" { Tail-Log }
            "6" { Update-Repo }
            "7" { Ensure-RepoCommand }
            "0" { break }
            default { Write-Host "无效选择" }
        }
    }
}

switch ($Cmd.ToLower()) {
    "start" {
        Start-Fls $Rest
    }
    "stop" {
        Stop-Fls
    }
    "restart" {
        Restart-Fls $Rest
    }
    "status" {
        Status-Fls
    }
    "log" {
        Tail-Log
    }
    "logs" {
        Tail-Log
    }
    "update" {
        Update-Repo
    }
    "upgrade" {
        Update-Repo
    }
    "pull" {
        Update-Repo
    }
    "ensure-repo" {
        Ensure-RepoCommand
    }
    "install" {
        Ensure-RepoCommand
    }
    "menu" {
        Show-Menu
    }
    "-h" {
        Show-Usage
    }
    "--help" {
        Show-Usage
    }
    "help" {
        Show-Usage
    }
    default {
        ErrMsg "未知命令：$Cmd"
        Show-Usage
        exit 1
    }
}