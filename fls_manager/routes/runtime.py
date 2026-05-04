import os
import sys
import time
import uuid
import shutil
import subprocess
from pathlib import Path

from flask import Blueprint, redirect, url_for

from ..paths import BASE_DIR, LOG_DIR
from ..state import DEPS_RUNNING
from ..utils import now_str, safe_name

bp = Blueprint("runtime", __name__)


def install_log_file(install_id, name):
    safe_pkg = safe_name(name or "runtime")
    return LOG_DIR / f"system-install-{safe_pkg}-{install_id}.log"


def runtime_windows_url(runtime):
    urls = {
        "python": "https://www.python.org/downloads/windows/",
        "bash": "https://gitforwindows.org/",
        "node": "https://nodejs.org/zh-cn/download",
        "typescript": "https://www.npmjs.com/package/tsx",
        "php": "https://windows.php.net/download/",
        "ruby": "https://rubyinstaller.org/downloads/",
        "perl": "https://strawberryperl.com/",
        "lua": "https://github.com/rjpcomputing/luaforwindows/releases",
        "java": "https://adoptium.net/zh-CN/temurin/releases/",
        "powershell": "https://github.com/PowerShell/PowerShell/releases",
    }
    return urls.get(runtime, "https://www.google.com/search?q=" + runtime + "+download")


def detect_pm():
    for pm in ["pkg", "apt", "apt-get", "dnf", "yum", "apk", "pacman"]:
        if shutil.which(pm):
            return pm
    return ""


def runtime_install_command(runtime):
    runtime = str(runtime or "").strip().lower()

    if runtime == "typescript":
        if not shutil.which("npm"):
            raise RuntimeError("npm 不可用，请先安装 Node.js")
        return "npm install -g tsx ts-node typescript"

    packages = {
        "python": {
            "pkg": "python",
            "apt": "python3 python3-pip python3-venv",
            "apt-get": "python3 python3-pip python3-venv",
            "dnf": "python3 python3-pip",
            "yum": "python3 python3-pip",
            "apk": "python3 py3-pip py3-virtualenv",
            "pacman": "python python-pip",
        },
        "bash": {
            "pkg": "bash",
            "apt": "bash",
            "apt-get": "bash",
            "dnf": "bash",
            "yum": "bash",
            "apk": "bash",
            "pacman": "bash",
        },
        "node": {
            "pkg": "nodejs",
            "apt": "nodejs npm",
            "apt-get": "nodejs npm",
            "dnf": "nodejs npm",
            "yum": "nodejs npm",
            "apk": "nodejs npm",
            "pacman": "nodejs npm",
        },
        "php": {
            "pkg": "php",
            "apt": "php-cli",
            "apt-get": "php-cli",
            "dnf": "php-cli",
            "yum": "php-cli",
            "apk": "php-cli",
            "pacman": "php",
        },
        "ruby": {
            "pkg": "ruby",
            "apt": "ruby",
            "apt-get": "ruby",
            "dnf": "ruby",
            "yum": "ruby",
            "apk": "ruby",
            "pacman": "ruby",
        },
        "perl": {
            "pkg": "perl",
            "apt": "perl",
            "apt-get": "perl",
            "dnf": "perl",
            "yum": "perl",
            "apk": "perl",
            "pacman": "perl",
        },
        "lua": {
            "pkg": "lua",
            "apt": "lua5.4",
            "apt-get": "lua5.4",
            "dnf": "lua",
            "yum": "lua",
            "apk": "lua5.4",
            "pacman": "lua",
        },
        "java": {
            "pkg": "openjdk-17",
            "apt": "openjdk-17-jre-headless",
            "apt-get": "openjdk-17-jre-headless",
            "dnf": "java-17-openjdk-headless",
            "yum": "java-17-openjdk-headless",
            "apk": "openjdk17-jre",
            "pacman": "jre-openjdk",
        },
    }

    pm = detect_pm()
    if not pm:
        raise RuntimeError("无法识别包管理器，请手动安装")

    pkg_map = packages.get(runtime)
    if not pkg_map or pm not in pkg_map:
        raise RuntimeError("暂不支持自动安装该运行环境")

    pkg_name = pkg_map[pm]

    if pm == "pkg":
        return f"pkg update -y || true; pkg install -y {pkg_name}"

    if pm == "apt":
        return f"apt update || true; apt install -y {pkg_name}"

    if pm == "apt-get":
        return f"apt-get update || true; apt-get install -y {pkg_name}"

    if pm == "dnf":
        return f"dnf install -y {pkg_name}"

    if pm == "yum":
        return f"yum install -y {pkg_name}"

    if pm == "apk":
        return f"apk add --no-cache {pkg_name}"

    if pm == "pacman":
        return f"pacman -Sy --noconfirm {pkg_name}"

    raise RuntimeError("无法生成安装命令")


@bp.route("/install/runtime/<runtime>")
def install_runtime(runtime):
    runtime = str(runtime or "").strip().lower()

    if os.name == "nt":
        return redirect(runtime_windows_url(runtime))

    install_id = uuid.uuid4().hex
    log_file = install_log_file(install_id, runtime)
    log_fp = open(log_file, "ab", buffering=0)

    try:
        cmd_text = runtime_install_command(runtime)
    except Exception as e:
        log_fp.write(f"生成安装命令失败: {e}\n".encode("utf-8"))
        log_fp.close()

        DEPS_RUNNING[install_id] = {
            "process": None,
            "package": runtime,
            "log_file": str(log_file),
            "log_fp": None,
            "start_time": time.time(),
        }

        return redirect(url_for("deps.deps_install_log", install_id=install_id))

    header = (
        f"===== 安装运行环境: {runtime} =====\n"
        f"时间: {now_str()}\n"
        f"命令: sh -lc {cmd_text}\n"
        f"日志文件: {log_file}\n"
        f"============================================================\n"
    )
    log_fp.write(header.encode("utf-8"))

    try:
        proc = subprocess.Popen(
            ["sh", "-lc", cmd_text],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
            env=os.environ.copy()
        )

        DEPS_RUNNING[install_id] = {
            "process": proc,
            "package": runtime,
            "log_file": str(log_file),
            "log_fp": log_fp,
            "start_time": time.time(),
        }

    except Exception as e:
        log_fp.write(f"启动安装失败: {e}\n".encode("utf-8"))
        log_fp.close()

    return redirect(url_for("deps.deps_install_log", install_id=install_id))


@bp.route("/install/node")
def install_node():
    return redirect(url_for("runtime.install_runtime", runtime="node"))
