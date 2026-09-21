[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

if (-not $InstallDir) {
    if (-not $env:LOCALAPPDATA) {
        throw "未找到 LOCALAPPDATA，请使用 -InstallDir 指定安装目录"
    }
    $InstallDir = Join-Path $env:LOCALAPPDATA "FLS"
}

$SourceDir = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

function Say($Message) {
    Write-Host "[FLS] $Message"
}

function Fail($Message) {
    throw "[FLS][ERROR] $Message"
}

function Test-ProgramTree($Path) {
    return (
        (Test-Path (Join-Path $Path "fls-manager.py")) -and
        (Test-Path (Join-Path $Path "fls_manager")) -and
        (Test-Path (Join-Path $Path "fls.ps1"))
    )
}

if (-not (Test-ProgramTree $SourceDir)) {
    Fail "安装脚本必须位于 FLS 发布包或源码根目录（缺少 fls-manager.py、fls_manager 或 fls.ps1）"
}

function Copy-ProgramFiles {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    $skip = @(
        ".git", ".github", ".venv", "data", "log", "logs", ".tmp",
        ".pytest_cache", "__pycache__", "tests", "tools", "packaging",
        "docs", "install.sh", "scripts"
    )

    Get-ChildItem -LiteralPath $SourceDir -Force | ForEach-Object {
        if ($skip -contains $_.Name) {
            return
        }

        $destination = Join-Path $InstallDir $_.Name
        if ($_.FullName -eq $destination) {
            return
        }

        Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force
    }

    # Keep user scripts on upgrades. The first install still receives the
    # built-in scripts shipped by the release package.
    $sourceScripts = Join-Path $SourceDir "scripts"
    $targetScripts = Join-Path $InstallDir "scripts"
    if ((Test-Path $sourceScripts) -and -not (Test-Path $targetScripts)) {
        Copy-Item -LiteralPath $sourceScripts -Destination $targetScripts -Recurse -Force
    }

    foreach ($directory in @("data", "log", "scripts")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir $directory) | Out-Null
    }
}

function Find-Python {
    $candidates = @(
        @{ File = "py"; Args = @("-3") },
        @{ File = "python"; Args = @() },
        @{ File = "python3"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.File -ErrorAction SilentlyContinue)) {
            continue
        }

        try {
            $version = (& $candidate.File @($candidate.Args + @(
                "-c", "import sys; print('%d.%d' % sys.version_info[:2])"
            )) | Out-String).Trim()
            if ([version]$version -ge [version]"3.10") {
                return $candidate
            }
        } catch {}
    }

    Fail "未找到 Python 3.10 或更高版本，请先安装 Python 并勾选 Add Python to PATH"
}

function Invoke-Python($Spec, [string[]]$Arguments) {
    & $Spec.File @($Spec.Args + $Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令执行失败（退出码 $LASTEXITCODE）"
    }
}

function Ensure-PythonEnv($Spec) {
    $venvDir = Join-Path $InstallDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (-not (Test-Path $venvPython)) {
        Say "创建 Python 虚拟环境：$venvDir"
        Invoke-Python $Spec @("-m", "venv", $venvDir)
    }

    if (-not (Test-Path $venvPython)) {
        Fail "虚拟环境创建失败：$venvPython"
    }

    $dependencyCheck = @(
        "import importlib.util; mods=['flask','requests','apscheduler','socks'];"
        "raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)"
    ) -join ""

    & $venvPython -c $dependencyCheck
    if ($LASTEXITCODE -ne 0) {
        Say "安装 FLS Python 依赖"
        & $venvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            throw "pip 更新失败"
        }
        & $venvPython -m pip install Flask requests APScheduler PySocks tzdata setproctitle
        if ($LASTEXITCODE -ne 0) {
            throw "FLS Python 依赖安装失败"
        }
    }

    return $venvPython
}

Say "源目录：$SourceDir"
Say "安装目录：$InstallDir"
Copy-ProgramFiles
$python = Find-Python
$venvPython = Ensure-PythonEnv $python

if (-not $NoStart) {
    Say "启动 FLS Manager"
    $entry = Join-Path $InstallDir "fls.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $entry start
    if ($LASTEXITCODE -ne 0) {
        throw "FLS 启动失败，请查看 $InstallDir\log\fls-manager-daemon.log"
    }
} else {
    Say "安装完成，未启动面板"
}

Say "安装完成"
Say "后续命令：powershell -ExecutionPolicy Bypass -File `"$InstallDir\fls.ps1`" status"
