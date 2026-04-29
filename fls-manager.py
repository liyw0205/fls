#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import time
import uuid
import html
import shlex
import signal
import ctypes
import shutil
import tarfile
import zipfile
import tempfile
import subprocess
import importlib.metadata
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

def _fls_bootstrap_check_and_install_deps():
    import sys as _sys
    import subprocess as _subprocess
    import importlib.util as _importlib_util

    required = [
        ("flask", "flask"),
        ("requests", "requests"),
        ("apscheduler", "apscheduler"),
        ("socks", "PySocks"),
        ("tzdata", "tzdata"),
    ]

    optional = [
        ("setproctitle", "setproctitle"),
    ]

    all_packages = required + optional

    def missing_packages(items):
        result = []
        for module_name, package_name in items:
            try:
                if _importlib_util.find_spec(module_name) is None:
                    result.append(package_name)
            except Exception:
                result.append(package_name)
        return result

    missing = missing_packages(all_packages)

    # 检查 Asia/Shanghai 时区可用性，精简环境可能缺系统 zoneinfo。
    try:
        from zoneinfo import ZoneInfo as _ZoneInfo
        _ZoneInfo("Asia/Shanghai")
    except Exception:
        if "tzdata" not in missing:
            missing.append("tzdata")

    # 去重保持顺序。
    dedup = []
    for item in missing:
        if item not in dedup:
            dedup.append(item)
    missing = dedup

    if not missing:
        return

    py = _sys.executable

    print("====================================================")
    print("[FLS] 检测到 Python 依赖缺失：")
    print("      " + " ".join(missing))
    print("[FLS] 正在尝试自动安装依赖...")
    print("====================================================")

    try:
        _subprocess.run(
            [py, "-m", "ensurepip", "--upgrade"],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            timeout=300,
        )
    except Exception:
        pass

    attempts = [
        [py, "-m", "pip", "install"] + missing,
        [py, "-m", "pip", "install", "--break-system-packages"] + missing,
        [py, "-m", "pip", "install", "--user"] + missing,
    ]

    last_error = ""

    for cmd in attempts:
        try:
            print("[FLS] 执行：" + " ".join(cmd))
            r = _subprocess.run(cmd, timeout=900)
            if r.returncode == 0:
                still_missing = missing_packages(required)
                try:
                    from zoneinfo import ZoneInfo as _ZoneInfo2
                    _ZoneInfo2("Asia/Shanghai")
                except Exception:
                    if "tzdata" not in still_missing:
                        still_missing.append("tzdata")

                if not still_missing:
                    print("====================================================")
                    print("[FLS] 依赖自动安装完成，继续启动面板")
                    print("====================================================")
                    return

                last_error = "安装后仍缺少：" + " ".join(still_missing)
            else:
                last_error = "退出码：" + str(r.returncode)
        except Exception as e:
            last_error = str(e)

    all_pkg_names = []
    for _, pkg in all_packages:
        if pkg not in all_pkg_names:
            all_pkg_names.append(pkg)

    pkg_text = " ".join(all_pkg_names)

    print("====================================================")
    print("[FLS] 依赖自动安装失败或仍不完整")
    if last_error:
        print("[FLS] 最后错误：" + str(last_error))
    print("")
    print("请手动执行以下命令安装依赖：")
    print("")
    print("    " + py + " -m pip install " + pkg_text)
    print("")
    print("如果系统提示 externally-managed-environment，可尝试：")
    print("")
    print("    " + py + " -m pip install --break-system-packages " + pkg_text)
    print("")
    print("如果没有权限，可尝试：")
    print("")
    print("    " + py + " -m pip install --user " + pkg_text)
    print("")
    print("Termux 可先执行：")
    print("")
    print("    pkg update -y && pkg install -y python clang make openssl libffi")
    print("")
    print("Debian / Ubuntu 可先执行：")
    print("")
    print("    apt update && apt install -y python3 python3-pip python3-venv")
    print("")
    print("====================================================")
    _sys.exit(1)


_fls_bootstrap_check_and_install_deps()
import requests
from flask import Flask, request, redirect, url_for, jsonify, Response, abort
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


# ============================================================
# 基础配置
# ============================================================
MAIN_PROCESS_NAME = "fls-manager"
TASK_PROCESS_PREFIX = "fls-task-"

def detect_base_dir():
    """
    自动识别 FLS 工作目录：
    1. 优先使用环境变量 FLS_BASE_DIR；
    2. Windows 默认 C:/fls；
    3. Termux 默认 $HOME/fls；
    4. Linux / proot 默认 /root/fls；
    5. 如果 /root 不可写，则回退到 $HOME/fls。
    """
    env_dir = os.environ.get("FLS_BASE_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    if os.name == "nt":
        return Path("C:/fls")

    home = Path.home()
    prefix = os.environ.get("PREFIX", "")
    termux_version = os.environ.get("TERMUX_VERSION", "")

    is_termux = (
        "com.termux" in prefix
        or bool(termux_version)
        or str(home).startswith("/data/data/com.termux")
    )

    if is_termux:
        return (home / "fls").resolve()

    default_root = Path("/root/fls")
    try:
        default_root.mkdir(parents=True, exist_ok=True)
        return default_root.resolve()
    except Exception:
        return (home / "fls").resolve()


BASE_DIR = detect_base_dir()
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "log"
SCRIPT_DIR = BASE_DIR / "scripts"

TASK_FILE = DATA_DIR / "tasks.json"
GLOBAL_ENV_FILE = DATA_DIR / "global_env.json"
PROXY_FILE = DATA_DIR / "proxies.json"

PYTHON_BIN = os.environ.get("FLS_PYTHON") or sys.executable
NODE_BIN = os.environ.get("FLS_NODE", "node")
BASH_BIN = os.environ.get("FLS_BASH", "bash")

HOST = os.environ.get("FLS_HOST", "0.0.0.0")
PORT = int(os.environ.get("FLS_PORT", "5700"))
ADMIN_TOKEN = os.environ.get("FLS_TOKEN", "").strip()

# Termux / 精简 Linux 环境可能缺少系统 zoneinfo 数据。
# 优先使用系统或 Python tzdata 的 Asia/Shanghai；
# 如果仍不可用，则兜底为固定 UTC+8。
try:
    from zoneinfo import ZoneInfo
    FLS_TIMEZONE = ZoneInfo("Asia/Shanghai")
except Exception:
    FLS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

for d in (BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
scheduler = BackgroundScheduler(timezone=FLS_TIMEZONE)
scheduler.start()

# task_id -> process info
RUNNING = {}

# deps_install_id -> process info
DEPS_RUNNING = {}


# ============================================================
# 通用工具
# ============================================================
def h(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def set_current_process_name(name: str):
    name = str(name)[:128]
    try:
        from setproctitle import setproctitle
        setproctitle(name)
        return True
    except Exception:
        pass

    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(15, name[:15].encode("utf-8"), 0, 0, 0)
        return True
    except Exception:
        return False


def read_proc_text(pid: str, name: str):
    try:
        return Path(f"/proc/{pid}/{name}").read_bytes().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def kill_old_manager_processes():
    """
    启动当前程序前，结束旧 fls-manager 进程。
    """
    if not Path("/proc").exists():
        return

    current_pid = os.getpid()
    parent_pid = os.getppid()
    targets = []

    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue

        ipid = int(pid)
        if ipid in (current_pid, parent_pid):
            continue

        comm = read_proc_text(pid, "comm").strip()
        cmdline = read_proc_text(pid, "cmdline").replace("\x00", " ").strip()

        matched = False
        if comm == MAIN_PROCESS_NAME:
            matched = True
        if cmdline == MAIN_PROCESS_NAME or cmdline.startswith(MAIN_PROCESS_NAME + " "):
            matched = True
        if f" {MAIN_PROCESS_NAME} " in f" {cmdline} ":
            matched = True

        if matched:
            targets.append(ipid)

    if not targets:
        return

    print(f"[启动检查] 发现旧 {MAIN_PROCESS_NAME} 进程: {targets}，准备结束")

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    time.sleep(1.5)

    for pid in targets:
        try:
            os.kill(pid, 0)
        except Exception:
            continue

        try:
            os.kill(pid, signal.SIGKILL)
            print(f"[启动检查] 已强制结束旧进程: {pid}")
        except Exception:
            pass


kill_old_manager_processes()
set_current_process_name(MAIN_PROCESS_NAME)


# ============================================================
# 鉴权
# ============================================================


@app.before_request
def before_request():
    check_auth()


# ============================================================
# 数据读写
# ============================================================
def read_json(file_path, default):
    if not file_path.exists():
        return default

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(file_path, data):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_tasks():
    return read_json(TASK_FILE, [])


def save_tasks(tasks):
    write_json(TASK_FILE, tasks)


def load_global_env():
    return read_json(GLOBAL_ENV_FILE, {})


def save_global_env(env):
    write_json(GLOBAL_ENV_FILE, env)


def _fls_base_load_proxies():
    return read_json(PROXY_FILE, [])


def save_proxies(proxies):
    write_json(PROXY_FILE, proxies)


def get_task(task_id):
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def _fls_base_get_proxy(proxy_id):
    if not proxy_id:
        return None

    for p in load_proxies():
        if p.get("id") == proxy_id:
            return p

    return None


# ============================================================
# 字符处理
# ============================================================
def safe_name(name):
    name = str(name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:80] or "未命名任务"


def safe_process_name(name):
    name = safe_name(name)
    return (TASK_PROCESS_PREFIX + name)[:120]


def parse_env_text(text):
    env = {}

    for raw in str(text or "").splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        env[key] = value

    return env


def env_to_text(env):
    if not env:
        return ""
    return "\n".join([f'{k}="{str(v)}"' for k, v in env.items()])


# ============================================================
# 代理
# ============================================================
def build_proxy_url(proxy):
    if not proxy:
        return ""

    ptype = proxy.get("type", "http")

    if ptype == "github":
        return proxy.get("url", "").strip().rstrip("/")

    host = proxy.get("host", "").strip()
    port = str(proxy.get("port", "")).strip()
    username = proxy.get("username", "").strip()
    password = proxy.get("password", "").strip()

    if not host or not port:
        return ""

    auth = ""
    if username:
        auth = username
        if password:
            auth += f":{password}"
        auth += "@"

    return f"{ptype}://{auth}{host}:{port}"


def requests_proxy_dict(proxy_id):
    proxy = get_proxy(proxy_id)

    if not proxy or proxy.get("type") == "github":
        return None

    proxy_url = build_proxy_url(proxy)
    if not proxy_url:
        return None

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def apply_proxy_env(env, proxy_id):
    proxy = get_proxy(proxy_id)

    if not proxy or proxy.get("type") == "github":
        return env

    proxy_url = build_proxy_url(proxy)
    if not proxy_url:
        return env

    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url

    if proxy_url.startswith("socks"):
        env["ALL_PROXY"] = proxy_url
        env["all_proxy"] = proxy_url

    return env


def github_proxy_url(url, proxy_id):
    proxy = get_proxy(proxy_id)

    if not proxy:
        return url

    if proxy.get("type") != "github":
        return url

    prefix = build_proxy_url(proxy)
    if not prefix:
        return url

    if "github.com" not in url and "raw.githubusercontent.com" not in url:
        return url

    return prefix.rstrip("/") + "/" + url


# ============================================================
# 日志
# ============================================================
def log_file_for_task(task):
    display_name = task.get("name") or Path(task.get("command", "task")).stem or "未命名任务"
    name = safe_name(display_name)
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return LOG_DIR / f"{name}-{ts}.log"


def latest_log_for_task(task):
    display_name = task.get("name") or Path(task.get("command", "task")).stem or "未命名任务"
    name = safe_name(display_name)

    files = sorted(
        LOG_DIR.glob(f"{name}-*.log"),
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )
    return str(files[0]) if files else ""


def tail_file(file_path, lines=800):
    if not file_path or not Path(file_path).exists():
        return "暂无日志"

    p = Path(file_path)

    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""

            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data

            text = data.decode("utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except Exception as e:
        return f"读取日志失败: {e}"


def parse_task_name_from_log(file_path):
    try:
        with open(file_path, "rb") as f:
            data = f.read(8192).decode("utf-8", errors="ignore")

        m = re.search(r"===== 启动任务:\s*(.*?)\s*=====", data)
        if m:
            return m.group(1).strip() or None

        return None
    except Exception:
        return None


# ============================================================
# 日志清理策略
# ============================================================
# 每个任务最多保留多少个日志文件
LOG_KEEP_PER_TASK = 10

# 单个日志文件超过多少自动删除：10 MB
LOG_MAX_SIZE_BYTES = 10 * 1024 * 1024

# 日志清理间隔：30 分钟
LOG_CLEANUP_INTERVAL_MINUTES = 30


# ============================================================
# 任务命令
# ============================================================


def wrap_command_with_process_name(cmd_info, process_name):
    # Windows 不支持 bash exec -a，也没有 POSIX 进程名机制。
    # shell 命令直接交给 cmd/powershell 外壳执行；普通命令直接执行。
    if os.name == "nt":
        if cmd_info["shell"]:
            return {
                "cmd": cmd_info["cmd"],
                "shell": True,
                "cwd": cmd_info["cwd"],
            }

        return {
            "cmd": cmd_info["cmd"],
            "shell": False,
            "cwd": cmd_info["cwd"],
        }

    if cmd_info["shell"]:
        return {
            "cmd": [
                BASH_BIN,
                "-c",
                'exec -a "$0" bash -lc "$1"',
                process_name,
                cmd_info["cmd"],
            ],
            "shell": False,
            "cwd": cmd_info["cwd"],
        }

    cmd = cmd_info["cmd"]

    # Python 虚拟环境不要用 exec -a，避免 venv 识别异常
    if cmd and Path(str(cmd[0])).name.startswith("python"):
        return {
            "cmd": cmd,
            "shell": False,
            "cwd": cmd_info["cwd"],
        }

    return {
        "cmd": [
            BASH_BIN,
            "-c",
            'exec -a "$0" "$@"',
            process_name,
        ] + cmd,
        "shell": False,
        "cwd": cmd_info["cwd"],
    }


# ============================================================
# 运行状态
# ============================================================
def is_running(task_id):
    info = RUNNING.get(task_id)

    if not info:
        return False

    proc = info.get("process")
    if not proc:
        RUNNING.pop(task_id, None)
        return False

    if proc.poll() is None:
        return True

    try:
        fp = info.get("log_fp")
        if fp:
            fp.close()
    except Exception:
        pass

    RUNNING.pop(task_id, None)
    return False


def increase_run_count(task_id):
    try:
        tasks = load_tasks()
        for item in tasks:
            if item.get("id") == task_id:
                item["run_count"] = int(item.get("run_count", 0)) + 1
                item["last_run_at"] = now_str()
                item["updated_at"] = now_str()
                break
        save_tasks(tasks)
    except Exception as e:
        print(f"[RunCount] 更新运行次数失败: {e}")


def _fls_runner_version_for_log(executable):
    """
    获取运行器版本首行，用于任务启动日志。
    仅用于日志展示，不影响任务实际执行。
    """
    try:
        import subprocess as _subprocess
        import sys as _sys
        from pathlib import Path as _Path

        exe = str(executable or "").strip()
        if not exe:
            return ""

        base = _Path(exe).name.lower()

        if base.startswith("python"):
            return "Python " + _sys.version.split()[0]

        version_args = ["--version"]
        if base in ("java",):
            version_args = ["-version"]

        r = _subprocess.run(
            [exe] + version_args,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT,
            text=True,
            timeout=8,
        )

        out = (r.stdout or "").strip()
        if not out:
            return ""

        return out.splitlines()[0].strip()
    except Exception:
        return ""


def fls_runner_log_lines(cmd_info):
    """
    根据实际启动命令生成运行器日志行。

    旧日志固定输出：
    Python: ...
    Bash: ...
    Node: ...

    新日志按实际运行器输出，例如：
    运行器: PHP
    PHP: PHP 8.2.30 ...
    """
    try:
        from pathlib import Path as _Path
        import shlex as _shlex

        cmd = (cmd_info or {}).get("cmd")
        shell = bool((cmd_info or {}).get("shell", False))

        executable = ""

        if isinstance(cmd, (list, tuple)) and cmd:
            executable = str(cmd[0])
        elif isinstance(cmd, str):
            if shell:
                try:
                    parts = _shlex.split(cmd)
                    executable = parts[0] if parts else ""
                except Exception:
                    executable = ""
            else:
                executable = cmd.strip().split()[0] if cmd.strip() else ""

        base = _Path(executable).name.lower()

        runner_map = {
            "python": "Python",
            "python3": "Python",
            "python.exe": "Python",
            "bash": "Bash",
            "sh": "Shell",
            "node": "Node",
            "node.exe": "Node",
            "php": "PHP",
            "php.exe": "PHP",
            "ruby": "Ruby",
            "perl": "Perl",
            "lua": "Lua",
            "java": "Java",
            "tsx": "TypeScript",
            "ts-node": "TypeScript",
            "pwsh": "PowerShell",
            "powershell": "PowerShell",
            "powershell.exe": "PowerShell",
        }

        runner = runner_map.get(base)

        if not runner:
            if shell:
                runner = "Shell"
            else:
                runner = base or "未知"

        version = _fls_runner_version_for_log(executable)

        if version:
            return "运行器: {}\n{}: {}".format(runner, runner, version)

        if executable:
            return "运行器: {}\n{}: {}".format(runner, runner, executable)

        return "运行器: {}".format(runner)
    except Exception as e:
        return "运行器: 未知\n运行器检测失败: {}".format(e)


def _fls_base_run_task_now(task_id, source="manual"):
    task = get_task(task_id)

    if not task:
        return False, "任务不存在"

    if is_running(task_id):
        return False, "任务已在运行中"

    try:
        cmd_info = build_command(task)
    except Exception as e:
        return False, f"命令解析失败：{e}"

    task_display_name = task.get("name") or task.get("command") or task_id
    process_name = safe_process_name(task_display_name)
    wrapped = wrap_command_with_process_name(cmd_info, process_name)

    log_file = log_file_for_task(task)

    # 创建新日志前清理该任务旧日志，避免单任务日志无限增长
    cleanup_logs_for_task(task_display_name)

    log_fp = open(log_file, "ab", buffering=0)

    env = os.environ.copy()
    env.update(load_global_env())
    env.update(task.get("env", {}) or {})
    env = apply_proxy_env(env, task.get("proxy_id", ""))
    env["PYTHONUNBUFFERED"] = "1"
    env["FLS_TASK_ID"] = task_id
    env["FLS_TASK_NAME"] = task_display_name
    env["FLS_TASK_PROCESS_NAME"] = process_name

    proxy = get_proxy(task.get("proxy_id", ""))
    proxy_desc = proxy.get("name") if proxy else "不使用代理"
    runner_log = fls_runner_log_lines(cmd_info)

    header = (
        f"===== 启动任务: {task_display_name} =====\n"
        f"时间: {now_str()}\n"
        f"来源: {source}\n"
        f"进程名: {process_name}\n"
        f"命令: {task.get('command')}\n"
        f"代理: {proxy_desc}\n"
        f"{runner_log}\n"
        f"工作目录: {wrapped.get('cwd')}\n"
        f"实际启动命令: {wrapped.get('cmd')}\n"
        f"============================================================\n"
    )
    log_fp.write(header.encode("utf-8"))

    try:
        popen_kwargs = {
            "shell": wrapped.get("shell", False),
            "cwd": wrapped["cwd"],
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "env": env,
        }

        if os.name == "nt":
            try:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            except Exception:
                pass
        else:
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            wrapped["cmd"],
            **popen_kwargs
        )
    except Exception as e:
        log_fp.write(f"启动失败: {e}\n".encode("utf-8"))
        log_fp.close()
        return False, f"启动失败：{e}"

    RUNNING[task_id] = {
        "process": proc,
        "pid": proc.pid,
        "process_name": process_name,
        "log_file": str(log_file),
        "log_fp": log_fp,
        "start_time": time.time(),
    }

    increase_run_count(task_id)
    return True, "启动成功"


def stop_task_now(task_id):
    info = RUNNING.get(task_id)

    if not info:
        return False, "任务未运行"

    proc = info.get("process")
    log_fp = info.get("log_fp")

    try:
        if proc and proc.poll() is None:
            if os.name == "nt":
                try:
                    proc.terminate()
                except Exception:
                    pass

                time.sleep(1)

                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(1)

                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

        if log_fp:
            try:
                log_fp.write(f"\n===== 任务已结束: {now_str()} =====\n".encode("utf-8"))
                log_fp.close()
            except Exception:
                pass

        RUNNING.pop(task_id, None)
        return True, "已结束"

    except Exception as e:
        return False, f"结束失败：{e}"


# ============================================================
# 定时任务
# ============================================================
def cron_to_trigger(cron_expr):
    cron_expr = str(cron_expr or "").strip()

    if not cron_expr:
        return None

    fields = cron_expr.split()

    if len(fields) == 5:
        return CronTrigger.from_crontab(cron_expr, timezone=FLS_TIMEZONE)

    if len(fields) == 6:
        sec, minute, hour, day, month, dow = fields
        return CronTrigger(
            second=sec,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone=FLS_TIMEZONE,
        )

    raise ValueError("Cron 格式错误，仅支持 5 位或 6 位")


def scheduler_run(task_id):
    ok, msg = run_task_now(task_id, source="cron")

    if not ok:
        task = get_task(task_id)
        if not task:
            return

        log_file = log_file_for_task(task)
        with open(log_file, "ab") as f:
            f.write(f"[{now_str()}] 定时触发失败: {msg}\n".encode("utf-8"))


# ============================================================
# UI
# ============================================================
def _fls_base_layout(title, active, body):
    token = tq()

    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{h(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<style>
:root {{
    --bg:#f4f6fb;--card:#fff;--text:#1f2937;--muted:#6b7280;--border:#e5e7eb;
    --primary:#18a058;--blue:#2563eb;--red:#dc2626;--orange:#f59e0b;
    --sidebar:#111827;--side-text:#d1d5db;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif}}
a{{text-decoration:none}}
.app{{display:flex;min-height:100vh}}
.sidebar{{position:fixed;left:0;top:0;bottom:0;width:240px;background:var(--sidebar);color:var(--side-text);z-index:20;transition:.25s}}
.brand{{height:64px;display:flex;align-items:center;padding:0 20px;color:#fff;font-size:20px;font-weight:800;border-bottom:1px solid rgba(255,255,255,.08)}}
.brand span{{width:10px;height:10px;border-radius:50%;background:var(--primary);margin-right:10px}}
.nav{{padding:14px 10px}}
.nav a{{display:block;padding:12px 14px;color:var(--side-text);border-radius:10px;margin-bottom:6px;font-size:15px}}
.nav a.active{{background:var(--primary);color:#fff}}
.nav a:hover{{background:rgba(255,255,255,.08)}}
.main{{margin-left:240px;width:calc(100% - 240px);min-height:100vh}}
.topbar{{height:64px;background:var(--card);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 22px;position:sticky;top:0;z-index:10}}
.menu-btn{{display:none;border:0;background:transparent;font-size:24px;margin-right:12px}}
.title{{font-weight:800;font-size:18px}}
.content{{padding:22px}}
.card{{background:var(--card);border-radius:14px;padding:18px;margin-bottom:18px;box-shadow:0 4px 16px rgba(0,0,0,.04)}}
.card-title{{font-size:17px;font-weight:800;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:18px}}
.stat{{background:var(--card);border-radius:14px;padding:18px;box-shadow:0 4px 16px rgba(0,0,0,.04)}}
.stat .label{{color:var(--muted);font-size:14px}}
.stat .num{{margin-top:8px;font-size:28px;font-weight:900}}
.table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;min-width:980px}}
th,td{{border-bottom:1px solid var(--border);padding:12px 10px;text-align:left;font-size:14px;vertical-align:middle}}
th{{color:var(--muted);background:#fafafa}}
.badge{{display:inline-block;padding:4px 9px;border-radius:999px;font-size:12px;font-weight:700}}
.green{{background:#dcfce7;color:#166534}}.red{{background:#fee2e2;color:#991b1b}}.gray{{background:#f3f4f6;color:#4b5563}}.blue{{background:#dbeafe;color:#1d4ed8}}
.btn{{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px 11px;border-radius:8px;border:0;color:#fff;cursor:pointer;font-size:13px;margin:2px;white-space:nowrap}}
.btn-primary{{background:var(--primary)}}.btn-blue{{background:var(--blue)}}.btn-red{{background:var(--red)}}.btn-orange{{background:var(--orange)}}.btn-gray{{background:#6b7280}}
input,textarea,select{{width:100%;border:1px solid var(--border);border-radius:10px;padding:10px 12px;font-size:14px;outline:none}}
textarea{{min-height:220px;font-family:Consolas,Menlo,monospace;resize:vertical}}
input:focus,textarea:focus,select:focus{{border-color:var(--primary);box-shadow:0 0 0 3px rgba(24,160,88,.14)}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.form-item label{{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}}
.help{{color:var(--muted);font-size:13px;line-height:1.7}}
.code{{background:#f3f4f6;border-radius:10px;padding:12px;font-family:Consolas,Menlo,monospace;overflow-x:auto;line-height:1.7}}
pre.log{{background:#0b1020;color:#d1d5db;border-radius:12px;padding:16px;min-height:560px;white-space:pre-wrap;word-break:break-word;font-family:Consolas,Menlo,monospace;font-size:13px;line-height:1.55;overflow:auto}}
.action-row{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.mask{{display:none}}
@media(max-width:1280px){{.grid{{grid-template-columns:repeat(3,1fr)}}}}
@media(max-width:1024px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:768px){{
    .sidebar{{transform:translateX(-100%)}}
    .sidebar.open{{transform:translateX(0)}}
    .mask.show{{display:block;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:15}}
    .main{{margin-left:0;width:100%}}
    .menu-btn{{display:inline-block}}
    .content{{padding:14px}}
    .form-grid{{grid-template-columns:1fr}}
    table{{min-width:850px}}
    th,td{{font-size:13px;padding:10px 8px}}
    .card{{padding:14px}}
    pre.log{{min-height:500px;font-size:12px}}
}}
@media(max-width:600px){{
    .grid{{grid-template-columns:1fr}}
    .btn{{padding:8px 10px;font-size:12px;min-height:38px}}
    .btn{{width:auto}}
    form .btn{{margin-top:4px}}
    .topbar{{height:58px}}
    .title{{font-size:16px}}
    .brand{{height:58px;font-size:18px}}
    input,textarea,select{{font-size:16px}}
    .content{{padding:12px}}
    .card{{border-radius:12px;padding:12px}}
    table{{min-width:760px}}
}}
</style>
</head>
<body>
<div class="mask" id="mask" onclick="toggleMenu(false)"></div>
<div class="app">
    <aside class="sidebar" id="sidebar">
        <div class="brand"><span></span>FLS 面板</div>
        <div class="nav">
            <a class="{'active' if active == 'dashboard' else ''}" href="/{token}">📊 仪表盘</a>
            <a class="{'active' if active == 'tasks' else ''}" href="/tasks{token}">📜 任务管理</a>
            <a class="{'active' if active == 'env' else ''}" href="/env{token}">🌐 全局变量</a>
            <a class="{'active' if active == 'proxy' else ''}" href="/proxy{token}">🧩 代理管理</a>
            <a class="{'active' if active == 'pull' else ''}" href="/pull{token}">📂 脚本管理</a>
            <a class="{'active' if active == 'backup' else ''}" href="/backup{token}">💾 备份恢复</a>
            <a class="{'active' if active == 'deps' else ''}" href="/deps{token}">📦 依赖管理</a>
            <a class="{'active' if active == 'logs' else ''}" href="/logs{token}">📁 日志中心</a>
            <a class="{'active' if active == 'about' else ''}" href="/about{token}">⚙️ 关于</a>
        </div>
    </aside>
    <main class="main">
        <div class="topbar">
            <button class="menu-btn" onclick="toggleMenu(true)">☰</button>
            <div class="title">{h(title)}</div>
        </div>
        <div class="content">{body}</div>
    </main>
</div>
<script>
function toggleMenu(show){{
    const sidebar=document.getElementById("sidebar");
    const mask=document.getElementById("mask");
    if(show){{sidebar.classList.add("open");mask.classList.add("show");}}
    else{{sidebar.classList.remove("open");mask.classList.remove("show");}}
}}

function runInlineScripts(container){{
    const scripts = container.querySelectorAll("script");
    scripts.forEach(function(oldScript){{
        const script = document.createElement("script");
        for(const attr of oldScript.attributes){{
            script.setAttribute(attr.name, attr.value);
        }}
        script.textContent = oldScript.textContent;
        oldScript.parentNode.replaceChild(script, oldScript);
    }});
}}

async function ajaxLoadPage(url, push){{
    try{{
        const res = await fetch(url, {{
            headers: {{"X-Requested-With": "FLS-Ajax"}}
        }});

        if(!res.ok){{
            location.href = url;
            return;
        }}

        const text = await res.text();
        const doc = new DOMParser().parseFromString(text, "text/html");

        const newContent = doc.querySelector(".content");
        const newTitle = doc.querySelector(".title");
        const newNav = doc.querySelector(".nav");
        const oldContent = document.querySelector(".content");
        const oldTitle = document.querySelector(".title");
        const oldNav = document.querySelector(".nav");

        if(!newContent || !oldContent){{
            location.href = url;
            return;
        }}

        if (window.__FLS_ACTIVE_LOG_INTERVAL__) {{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}

        oldContent.innerHTML = newContent.innerHTML;
        if(newTitle && oldTitle) oldTitle.innerHTML = newTitle.innerHTML;
        if(newNav && oldNav) oldNav.innerHTML = newNav.innerHTML;

        document.title = doc.title || document.title;

        runInlineScripts(oldContent);

        if(push){{
            history.pushState({{url:url}}, "", url);
        }}

        window.scrollTo(0, 0);
        toggleMenu(false);
    }}catch(e){{
        location.href = url;
    }}
}}

document.addEventListener("click", function(e){{
    const a = e.target.closest(".nav a");
    if(!a) return;

    const href = a.getAttribute("href");
    if(!href || href.startsWith("http") || href.startsWith("#")) return;

    e.preventDefault();
    ajaxLoadPage(href, true);
}});

window.addEventListener("popstate", function(){{
    ajaxLoadPage(location.href, false);
}});

(function(){{
    if (window.__FLS_FAST_AJAX_PATCHED__) return;
    window.__FLS_FAST_AJAX_PATCHED__ = true;

    function shouldIgnoreLink(a){{
        if(!a) return true;

        const href = a.getAttribute("href") || "";
        if(!href) return true;
        if(href.startsWith("#")) return true;
        if(href.startsWith("javascript:")) return true;
        if(a.target && a.target !== "_self") return true;
        if(a.hasAttribute("download")) return true;

        const abs = new URL(href, location.href);
        if(abs.origin !== location.origin) return true;

        const path = abs.pathname;

        if(path.startsWith("/scripts/download/")) return true;
        if(path === "/backup/export") return true;
        if(path.startsWith("/api/")) return true;

        const onclick = a.getAttribute("onclick") || "";
        if(onclick.indexOf("confirm") >= 0) return true;

        return false;
    }}

    function setNavActiveByUrl(url){{
        try{{
            const u = new URL(url, location.href);
            const nav = document.querySelector(".nav");
            if(!nav) return;

            nav.querySelectorAll("a").forEach(function(a){{
                a.classList.remove("active");

                const au = new URL(a.href, location.href);
                const ap = au.pathname;
                const up = u.pathname;

                if(
                    ap === up ||
                    (ap === "/" && up === "/") ||
                    (ap !== "/" && up.startsWith(ap))
                ){{
                    a.classList.add("active");
                }}
            }});
        }}catch(e){{}}
    }}

    function showInstantLoading(url){{
        setNavActiveByUrl(url);

        const content = document.querySelector(".content");
        if(content){{
            content.style.opacity = "0.55";
            content.style.transition = "opacity .12s ease";
        }}
    }}

    function restoreOpacity(){{
        const content = document.querySelector(".content");
        if(content){{
            requestAnimationFrame(function(){{
                content.style.opacity = "1";
            }});
        }}
    }}

    async function replaceFromHtml(text, url, push){{
        const doc = new DOMParser().parseFromString(text, "text/html");

        const newContent = doc.querySelector(".content");
        const newTitle = doc.querySelector(".title");
        const newNav = doc.querySelector(".nav");

        const oldContent = document.querySelector(".content");
        const oldTitle = document.querySelector(".title");
        const oldNav = document.querySelector(".nav");

        if(!newContent || !oldContent){{
            location.href = url;
            return;
        }}

        if (window.__FLS_ACTIVE_LOG_INTERVAL__) {{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}

        oldContent.innerHTML = newContent.innerHTML;

        if(newTitle && oldTitle) oldTitle.innerHTML = newTitle.innerHTML;
        if(newNav && oldNav) oldNav.innerHTML = newNav.innerHTML;

        document.title = doc.title || document.title;

        if(typeof runInlineScripts === "function"){{
            runInlineScripts(oldContent);
        }}

        if(push){{
            history.pushState({{url:url}}, "", url);
        }}

        window.scrollTo(0, 0);

        if(typeof toggleMenu === "function"){{
            toggleMenu(false);
        }}

        restoreOpacity();
    }}

    async function fastLoad(url, push){{
        showInstantLoading(url);

        try{{
            const res = await fetch(url, {{
                headers: {{
                    "X-Requested-With": "FLS-Ajax"
                }}
            }});

            if(!res.ok){{
                location.href = url;
                return;
            }}

            const text = await res.text();
            await replaceFromHtml(text, res.url || url, push);
        }}catch(e){{
            location.href = url;
        }}
    }}

    document.addEventListener("click", function(e){{
        const a = e.target.closest("a");
        if(!a) return;

        if(!a.closest(".content") && !a.closest(".nav")) return;
        if(shouldIgnoreLink(a)) return;

        e.preventDefault();
        e.stopImmediatePropagation();

        fastLoad(a.href, true);
    }}, true);

    document.addEventListener("submit", async function(e){{
        const form = e.target;
        if(!form || !form.closest(".content")) return;

        const method = (form.getAttribute("method") || "GET").toUpperCase();
        const action = form.getAttribute("action") || location.href;
        const url = new URL(action, location.href);

        e.preventDefault();

        showInstantLoading(url.href);

        try{{
            let fetchUrl = url.href;
            let opts = {{
                method: method,
                headers: {{
                    "X-Requested-With": "FLS-Ajax"
                }}
            }};

            if(method === "GET"){{
                const fd = new FormData(form);
                for(const item of fd.entries()){{
                    fetchUrl.searchParams.set(item[0], item[1]);
                }}
            }}else{{
                const fd = (typeof FormData !== "undefined" && e.submitter)
                    ? new FormData(form, e.submitter)
                    : new FormData(form);

                if(e.submitter && e.submitter.name && !fd.has(e.submitter.name)){{
                    fd.append(e.submitter.name, e.submitter.value || "");
                }}

                opts.body = fd;
            }}

            const res = await fetch(fetchUrl, opts);

            if(!res.ok){{
                location.href = fetchUrl;
                return;
            }}

            const text = await res.text();
            await replaceFromHtml(text, res.url || fetchUrl, true);

        }}catch(err){{
            form.submit();
        }}
    }}, true);

    window.addEventListener("popstate", function(){{
        fastLoad(location.href, false);
    }});

    window.flsFastLoad = fastLoad;
}})();

</script>
</body>
</html>
"""


# ============================================================
# 任务表格
# ============================================================


# ============================================================
# 仪表盘
# ============================================================

# ============================================================
@app.route("/")
def dashboard():
    tasks = load_tasks()

    total = len(tasks)
    enabled = sum(1 for t in tasks if t.get("enabled", True))
    running = sum(1 for t in tasks if is_running(t["id"]))
    cron_count = sum(1 for t in tasks if str(t.get("cron", "")).strip())
    run_total = sum(int(t.get("run_count", 0)) for t in tasks)

    body = f"""
<div class="grid">
    <div class="stat"><div class="label">任务总数</div><div class="num">{total}</div></div>
    <div class="stat"><div class="label">已启用</div><div class="num" style="color:#18a058;">{enabled}</div></div>
    <div class="stat"><div class="label">运行中</div><div class="num" style="color:#2563eb;">{running}</div></div>
    <div class="stat"><div class="label">定时任务</div><div class="num" style="color:#f59e0b;">{cron_count}</div></div>
    <div class="stat"><div class="label">累计运行次数</div><div class="num" style="color:#7c3aed;">{run_total}</div></div>
</div>
<div class="card">
    <div class="card-title">任务状态</div>
    <div class="table-wrap">{tasks_table(tasks)}</div>
</div>
"""
    return layout("仪表盘", "dashboard", body)


# ============================================================
# 任务管理
# ============================================================
@app.route("/tasks")
def tasks_page():
    tasks = load_tasks()
    token = tq()

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务管理</div>
            <div class="help">Cron 留空表示手动任务，不会加入定时器。</div>
        </div>
        <a class="btn btn-primary" href="/task/new{token}">新建任务</a>
    </div>
</div>
<div class="card">
    <div class="table-wrap">{tasks_table(tasks)}</div>
</div>
"""
    return layout("任务管理", "tasks", body)
def task_form(task=None):
    token = tq()

    if task is None:
        task = {
            "id": "",
            "name": "",
            "command": "task ",
            "cron": "",
            "enabled": True,
            "proxy_id": "",
            "env": {},
        }

    checked = "checked" if task.get("enabled", True) else ""
    env_text = env_to_text(task.get("env", {}) or {})
    proxy_options = proxy_select_options(task.get("proxy_id", ""))

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">任务信息</div>
    <div class="form-grid">
        <div class="form-item">
            <label>任务名</label>
            <input name="name" value="{h(task.get('name', ''))}" placeholder="例如：中国联通">
        </div>
        <div class="form-item">
            <label>Cron 表达式</label>
            <input name="cron" value="{h(task.get('cron', ''))}" placeholder="例如：0 8 * * *">
            <div class="help">留空表示手动运行任务。支持 5 位：分 时 日 月 周；也支持 6 位：秒 分 时 日 月 周。</div>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>命令</label>
        <input name="command" value="{h(task.get('command', ''))}" placeholder="task 1.py">
        <div class="help">
            <b>task 1.py</b> = 运行 <b>/root/fls/scripts/1.py</b>。<br>
            不以 task 开头则作为系统命令执行，例如：<b>python3 /root/test.py</b>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>代理</label>
        <select name="proxy_id">{proxy_options}</select>
        <div class="help">任务运行时会自动注入 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。</div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用任务
    </label>
</div>

<div class="card">
    <div class="card-title">任务变量</div>
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
    <div class="help">任务变量仅对此任务生效，会覆盖同名全局变量。</div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/tasks{token}">返回</a>
</div>
</form>

<div class="card">
    <div class="card-title">示例</div>
    <div class="code">
task chinaUnicom.py<br>
task 移动云盘最新版.py<br>
task a/test.sh arg1 arg2<br>
task demo.js<br>
python3 /root/other.py
    </div>
</div>
"""
    return body


@app.route("/task/new", methods=["GET", "POST"])
def task_new():
    if request.method == "POST":
        tasks = load_tasks()

        task = {
            "id": uuid.uuid4().hex,
            "name": request.form.get("name", "").strip(),
            "command": request.form.get("command", "").strip(),
            "cron": request.form.get("cron", "").strip(),
            "enabled": request.form.get("enabled") == "1",
            "proxy_id": request.form.get("proxy_id", "").strip(),
            "env": fls_parse_task_env_from_form(),
            "run_count": 0,
            "created_at": now_str(),
            "updated_at": now_str(),
        }

        tasks.append(task)
        save_tasks(tasks)
        reload_scheduler()

        return redirect_to("tasks_page")

    return layout("新建任务", "tasks", task_form())


@app.route("/task/edit/<task_id>", methods=["GET", "POST"])
def task_edit(task_id):
    tasks = load_tasks()
    task = None

    for t in tasks:
        if t.get("id") == task_id:
            task = t
            break

    if not task:
        abort(404)

    if request.method == "POST":
        task["name"] = request.form.get("name", "").strip()
        task["command"] = request.form.get("command", "").strip()
        task["cron"] = request.form.get("cron", "").strip()
        task["enabled"] = request.form.get("enabled") == "1"
        task["proxy_id"] = request.form.get("proxy_id", "").strip()
        task["env"] = fls_parse_task_env_from_form()
        task["updated_at"] = now_str()

        if "run_count" not in task:
            task["run_count"] = 0

        save_tasks(tasks)
        reload_scheduler()

        return redirect_to("tasks_page")

    return layout("编辑任务", "tasks", task_form(task))


@app.route("/task/delete/<task_id>")
def task_delete(task_id):
    stop_task_now(task_id)
    tasks = [t for t in load_tasks() if t.get("id") != task_id]
    save_tasks(tasks)
    reload_scheduler()
    return redirect_to("tasks_page")


@app.route("/task/toggle/<task_id>")
def task_toggle(task_id):
    tasks = load_tasks()

    for task in tasks:
        if task.get("id") == task_id:
            task["enabled"] = not task.get("enabled", True)
            task["updated_at"] = now_str()
            break

    save_tasks(tasks)
    reload_scheduler()
    return redirect_to("tasks_page")


# ============================================================
# 运行、停止、日志
# ============================================================
@app.route("/run/<task_id>")
def run_task_route(task_id):
    ok, msg = run_task_now(task_id, source="manual")

    if not ok:
        return f"{h(msg)}<br><a href='{url_with_token('/tasks')}'>返回</a>", 400

    return redirect_to("log_view", task_id=task_id)


@app.route("/stop/<task_id>")
def stop_task_route(task_id):
    stop_task_now(task_id)
    return redirect_to("tasks_page")


@app.route("/log/<task_id>")
def log_view(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    running = is_running(task_id)

    if running:
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
        process_name = RUNNING.get(task_id, {}).get("process_name", "")
        pid = RUNNING.get(task_id, {}).get("pid", "")
    else:
        log_file = latest_log_for_task(task)
        process_name = safe_process_name(task.get("name") or task.get("command") or task_id)
        pid = ""

    token = tq()

    api = f"/api/log/{task_id}"
    if ADMIN_TOKEN:
        api += f"?token={ADMIN_TOKEN}&lines=1200"
    else:
        api += "?lines=1200"

    body = f"""
<div class="card">
    <div class="card-title">日志：{h(task.get('name') or task.get('command'))}</div>
    <div class="help">
        状态：<b>{"运行中" if running else "已停止"}</b><br>
        PID：{h(pid or "-")}<br>
        进程名：{h(process_name)}<br>
        日志文件：{h(log_file or "暂无")}
    </div>
    <br>
    <a class="btn btn-primary" href="/run/{h(task_id)}{token}">运行</a>
    <a class="btn btn-red" href="/stop/{h(task_id)}{token}" onclick="return confirm('确定结束该任务吗？')">结束</a>
    <a class="btn btn-gray" href="/tasks{token}">返回</a>
</div>

<pre class="log" id="log">加载中...</pre>

<script>
window.__FLS_LOG_USER_NEAR_BOTTOM__ = true;

function checkNearBottom(){{
    const distance = document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
    window.__FLS_LOG_USER_NEAR_BOTTOM__ = distance < 80;
}}

window.addEventListener("scroll", checkNearBottom);

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const res = await fetch("{api}");
        const text = await res.text();
        const el = document.getElementById("log");
        el.textContent = text;

        if (window.__FLS_LOG_USER_NEAR_BOTTOM__) {{
            window.scrollTo(0, document.documentElement.scrollHeight);
        }} else {{
            const afterHeight = document.documentElement.scrollHeight;
            const delta = afterHeight - beforeHeight;
            window.scrollTo(0, beforeScroll + Math.max(delta, 0));
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if (window.__FLS_ACTIVE_LOG_INTERVAL__) {{
    clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
    window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
}}

loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("任务日志", "logs", body)


@app.route("/api/log/<task_id>")
def api_log(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    lines = int(request.args.get("lines", "800"))

    if is_running(task_id):
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
    else:
        log_file = latest_log_for_task(task)

    return Response(
        tail_file(log_file, lines),
        mimetype="text/plain; charset=utf-8"
    )


# ============================================================
# 全局变量
# ============================================================
@app.route("/env", methods=["GET", "POST"])
def global_env_page():
    if request.method == "POST":
        env = parse_env_text(request.form.get("env_text", ""))
        save_global_env(env)
        return redirect_to("global_env_page")

    env_text = env_to_text(load_global_env())
    token = tq()

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">全局变量</div>
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
    <div class="help">
        全局变量对所有任务生效。<br>
        如果任务变量中有同名变量，则任务变量优先。
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存全局变量</button>
    <a class="btn btn-gray" href="/tasks{token}">返回任务</a>
</div>
</form>
"""
    return layout("全局变量", "env", body)


# ============================================================
# 代理页
# ============================================================
@app.route("/proxy")
def proxy_page():
    token = tq()
    proxies = load_proxies()

    test_api_prefix = "/api/proxy/test/"
    quality_api_prefix = "/api/proxy/quality/"

    if ADMIN_TOKEN:
        test_api_suffix = f"?token={ADMIN_TOKEN}"
        quality_api_suffix = f"?token={ADMIN_TOKEN}"
    else:
        test_api_suffix = ""
        quality_api_suffix = ""

    rows = ""

    if not proxies:
        rows = '<tr><td colspan="5">暂无代理，请点击新增代理</td></tr>'
    else:
        for p in proxies:
            proxy_id = p.get("id")
            ptype = p.get("type")

            if ptype == "github":
                addr = p.get("url", "")
            else:
                addr = f'{p.get("host", "")}:{p.get("port", "")}'

            rows += f"""
<tr>
    <td>{h(p.get("name", ""))}</td>
    <td>{h(ptype)}</td>
    <td>{h(addr)}</td>
    <td>{h(p.get("created_at", "-"))}</td>
    <td>
        <a class="btn btn-blue" href="/proxy/edit/{h(proxy_id)}{token}">编辑</a>
        <button class="btn btn-orange" type="button" onclick="testProxyById('{h(proxy_id)}')">测试</button>
        <button class="btn btn-primary" type="button" onclick="qualityProxyById('{h(proxy_id)}')">质量检测</button>
        <a class="btn btn-red" href="/proxy/delete/{h(proxy_id)}{token}" onclick="return confirm('确定删除代理吗？')">删除</a>
    </td>
</tr>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">代理管理</div>
            <div class="help">
                代理可用于任务运行、拉取脚本、GitHub 加速等。<br>
                SOCKS5 代理需要 PySocks 依赖，若不可用请到“依赖管理”安装 PySocks。
            </div>
        </div>
        <a class="btn btn-primary" href="/proxy/new{token}">新增代理</a>
    </div>
</div>

<div class="card">
    <div class="card-title">代理列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>地址</th>
                    <th>创建时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>

<div class="card" id="proxyListRealtimeResult" style="display:none;">
    <div class="card-title">实时检测结果</div>
    <div class="help" id="proxyListRealtimeText">等待操作</div>
</div>

<script>
function showProxyListResult(html){{
    document.getElementById("proxyListRealtimeResult").style.display = "block";
    document.getElementById("proxyListRealtimeText").innerHTML = html;
    document.getElementById("proxyListRealtimeResult").scrollIntoView({{
        behavior: "smooth",
        block: "nearest"
    }});
}}

async function testProxyById(proxyId){{
    showProxyListResult("正在测试代理，请稍候...");

    try {{
        const res = await fetch("{test_api_prefix}" + encodeURIComponent(proxyId) + "{test_api_suffix}");
        const json = await res.json();

        if(json.ok){{
            showProxyListResult(
                "代理：<b>" + escapeHtml(json.name || "") + "</b><br>" +
                "状态：<b style='color:#18a058'>成功</b><br>" +
                "状态码：" + escapeHtml(String(json.status_code)) + "<br>" +
                "耗时：" + escapeHtml(String(json.elapsed_ms)) + " ms"
            );
        }} else {{
            showProxyListResult(
                "代理：<b>" + escapeHtml(json.name || "") + "</b><br>" +
                "状态：<b style='color:#dc2626'>失败</b><br>" +
                "错误：" + escapeHtml(json.error || "未知错误")
            );
        }}
    }} catch(e) {{
        showProxyListResult("请求失败：" + escapeHtml(String(e)));
    }}
}}

async function qualityProxyById(proxyId){{
    showProxyListResult("正在进行质量检测，请稍候...");

    try {{
        const res = await fetch("{quality_api_prefix}" + encodeURIComponent(proxyId) + "{quality_api_suffix}");
        const json = await res.json();

        if(!json.ok){{
            showProxyListResult(
                "代理：<b>" + escapeHtml(json.name || "") + "</b><br>" +
                "检测失败：" + escapeHtml(json.error || "未知错误")
            );
            return;
        }}

        let html = "代理：<b>" + escapeHtml(json.name || "") + "</b><br><br>";
        html += "<div class='table-wrap'><table><thead><tr>" +
            "<th>测试地址</th><th>结果</th><th>状态码</th><th>耗时 / 错误</th>" +
            "</tr></thead><tbody>";

        for(const item of json.items){{
            html += "<tr>" +
                "<td>" + escapeHtml(item.url) + "</td>" +
                "<td>" + (item.ok ? "<span class='badge green'>成功</span>" : "<span class='badge red'>失败</span>") + "</td>" +
                "<td>" + escapeHtml(String(item.status_code)) + "</td>" +
                "<td>" + escapeHtml(String(item.elapsed)) + "</td>" +
                "</tr>";
        }}

        html += "</tbody></table></div>";
        showProxyListResult(html);

    }} catch(e) {{
        showProxyListResult("请求失败：" + escapeHtml(String(e)));
    }}
}}

function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{
            "&":"&amp;",
            "<":"&lt;",
            ">":"&gt;",
            '"':"&quot;",
            "'":"&#39;"
        }}[c];
    }});
}}
</script>
"""

    return layout("代理管理", "proxy", body)


def requests_proxy_dict_from_proxy(proxy):
    if not proxy or proxy.get("type") == "github":
        return None

    proxy_url = build_proxy_url(proxy)

    if not proxy_url:
        return None

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def github_proxy_url_from_proxy(url, proxy):
    if not proxy:
        return url

    if proxy.get("type") != "github":
        return url

    prefix = build_proxy_url(proxy)

    if not prefix:
        return url

    if "github.com" not in url and "raw.githubusercontent.com" not in url:
        return url

    return prefix.rstrip("/") + "/" + url
def test_proxy_object(proxy, test_url="https://www.google.com/generate_204"):
    start = time.time()

    if proxy.get("type") == "github":
        real_url = github_proxy_url_from_proxy("https://github.com", proxy)
        r = requests.get(real_url, timeout=15)
    else:
        r = requests.get(
            test_url,
            proxies=requests_proxy_dict_from_proxy(proxy),
            timeout=15
        )

    ms = int((time.time() - start) * 1000)

    return {
        "status_code": r.status_code,
        "elapsed_ms": ms,
    }


def parse_quality_urls(value=None):
    """
    解析自定义质量检测地址。
    支持换行、逗号、空格分隔。
    """
    default_urls = [
        "https://www.baidu.com",
        "https://www.github.com",
        "https://raw.githubusercontent.com",
    ]

    value = str(value or "").strip()
    if not value:
        return default_urls

    items = re.split(r"[\s,，]+", value)
    urls = []

    for item in items:
        item = item.strip()
        if not item:
            continue

        if not item.startswith(("http://", "https://")):
            item = "https://" + item

        if item not in urls:
            urls.append(item)

    return urls or default_urls


def quality_proxy_object(proxy, urls=None):
    """
    并发检测多个域名。
    每个域名独立超时，避免其中一个卡死拖住整体。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    urls = urls or parse_quality_urls()
    timeout = 8

    def check_one(u):
        start = time.time()

        try:
            real_url = github_proxy_url_from_proxy(u, proxy) if proxy.get("type") == "github" else u

            r = requests.get(
                real_url,
                proxies=requests_proxy_dict_from_proxy(proxy),
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"}
            )

            ms = int((time.time() - start) * 1000)

            return {
                "url": u,
                "ok": True,
                "status_code": r.status_code,
                "elapsed": f"{ms} ms",
            }

        except Exception as e:
            ms = int((time.time() - start) * 1000)

            return {
                "url": u,
                "ok": False,
                "status_code": "-",
                "elapsed": f"{ms} ms / {e}",
            }

    result_map = {}

    with ThreadPoolExecutor(max_workers=min(len(urls), 8) or 1) as pool:
        futures = {
            pool.submit(check_one, u): u
            for u in urls
        }

        for future in as_completed(futures):
            u = futures[future]

            try:
                result_map[u] = future.result()
            except Exception as e:
                result_map[u] = {
                    "url": u,
                    "ok": False,
                    "status_code": "-",
                    "elapsed": str(e),
                }

    return [result_map.get(u, {
        "url": u,
        "ok": False,
        "status_code": "-",
        "elapsed": "未知错误",
    }) for u in urls]


def _fls_base_proxy_form(proxy=None, mode="new"):

    token = tq()

    if proxy is None:
        proxy = {
            "id": "",
            "name": "",
            "type": "socks5",
            "host": "",
            "port": "",
            "username": "",
            "password": "",
            "url": "",
        }

    action = "/proxy/new" if mode == "new" else f"/proxy/edit/{proxy.get('id')}"

    if ADMIN_TOKEN:
        action += f"?token={ADMIN_TOKEN}"

    def selected(v):
        return "selected" if proxy.get("type") == v else ""

    body = f"""
<form method="post" id="proxyForm" action="{h(action)}">
<div class="card">
    <div class="card-title">{"新增代理" if mode == "new" else "编辑代理"}</div>

    <input type="hidden" name="id" value="{h(proxy.get('id', ''))}">

    <div class="form-grid">
        <div class="form-item">
            <label>代理名称</label>
            <input name="name" value="{h(proxy.get('name', ''))}" placeholder="例如：本地 SOCKS5">
        </div>

        <div class="form-item">
            <label>代理类型</label>
            <select name="type" id="proxyType" onchange="toggleGithubProxyBox()">
                <option value="socks5" {selected("socks5")}>SOCKS5</option>
                <option value="http" {selected("http")}>HTTP</option>
                <option value="https" {selected("https")}>HTTPS</option>
                <option value="github" {selected("github")}>GitHub 代理</option>
            </select>
        </div>

    </div>

    <br>

    <div class="form-grid" id="normalProxyBox">
        <div class="form-item">
            <label>Host</label>
            <input name="host" value="{h(proxy.get('host', ''))}" placeholder="127.0.0.1">
        </div>

        <div class="form-item">
            <label>Port</label>
            <input name="port" value="{h(proxy.get('port', ''))}" placeholder="1080">
        </div>

        <div class="form-item">
            <label>用户名，可空</label>
            <input name="username" value="{h(proxy.get('username', ''))}">
        </div>

        <div class="form-item">
            <label>密码，可空</label>
            <input name="password" value="{h(proxy.get('password', ''))}">
        </div>
    </div>

    <br>

    <div class="form-item" id="githubProxyBox" style="display:none;">
        <label>GitHub 代理地址</label>
        <input name="url" value="{h(proxy.get('url', ''))}" placeholder="例如：https://gh-proxy.com/">
        <div class="help">
            仅代理类型选择“GitHub 代理”时使用。<br>
            会把 GitHub URL 转为：代理地址/原始URL。
        </div>
    </div>
</div>

<div class="card">
    <div class="form-item">
        <label>自定义质量检测域名，可空</label>
        <textarea name="quality_urls" id="qualityUrls" style="min-height:110px;" placeholder="每行一个，例如：
https://www.baidu.com
https://www.github.com
https://raw.githubusercontent.com"></textarea>
        <div class="help">为空则使用默认检测域名。检测时会并发请求，每个域名单独显示结果。</div>
    </div>

    <br>

    <button class="btn btn-primary" type="submit">保存代理</button>
    <button class="btn btn-blue" type="button" onclick="testProxyRealtime()">测试</button>
    <button class="btn btn-orange" type="button" onclick="qualityProxyRealtime()">质量检测</button>
    <a class="btn btn-gray" href="/proxy{token}">返回</a>
</div>
</form>

<div class="card" id="proxyRealtimeResult" style="display:none;">
    <div class="card-title">实时结果</div>
    <div class="help" id="proxyRealtimeText">等待操作</div>
</div>

<script>
function toggleGithubProxyBox(){{
    const type = document.getElementById("proxyType").value;
    const box = document.getElementById("githubProxyBox");
    box.style.display = type === "github" ? "block" : "none";
}}

function showProxyResult(html){{
    document.getElementById("proxyRealtimeResult").style.display = "block";
    document.getElementById("proxyRealtimeText").innerHTML = html;
    document.getElementById("proxyRealtimeResult").scrollIntoView({{behavior:"smooth", block:"nearest"}});
}}

async function testProxyRealtime(){{
    const form = document.getElementById("proxyForm");
    const data = new FormData(form);
    showProxyResult("正在测试代理，请稍候...");

    try {{
        const res = await fetch("{url_with_token('/api/proxy/test-form')}", {{
            method: "POST",
            body: data
        }});
        const json = await res.json();

        if(json.ok){{
            showProxyResult(
                "状态：<b style='color:#18a058'>成功</b><br>" +
                "状态码：" + json.status_code + "<br>" +
                "耗时：" + json.elapsed_ms + " ms"
            );
        }} else {{
            showProxyResult(
                "状态：<b style='color:#dc2626'>失败</b><br>" +
                "错误：" + escapeHtml(json.error || "未知错误")
            );
        }}
    }} catch(e) {{
        showProxyResult("请求失败：" + escapeHtml(String(e)));
    }}
}}

async function qualityProxyRealtime(){{
    const form = document.getElementById("proxyForm");
    const data = new FormData(form);
    showProxyResult("正在进行质量检测，请稍候...");

    try {{
        const res = await fetch("{url_with_token('/api/proxy/quality-form')}", {{
            method: "POST",
            body: data
        }});
        const json = await res.json();

        if(!json.ok){{
            showProxyResult("检测失败：" + escapeHtml(json.error || "未知错误"));
            return;
        }}

        let html = "<div class='table-wrap'><table><thead><tr>" +
            "<th>测试地址</th><th>结果</th><th>状态码</th><th>耗时 / 错误</th>" +
            "</tr></thead><tbody>";

        for(const item of json.items){{
            html += "<tr>" +
                "<td>" + escapeHtml(item.url) + "</td>" +
                "<td>" + (item.ok ? "<span class='badge green'>成功</span>" : "<span class='badge red'>失败</span>") + "</td>" +
                "<td>" + escapeHtml(String(item.status_code)) + "</td>" +
                "<td>" + escapeHtml(String(item.elapsed)) + "</td>" +
                "</tr>";
        }}

        html += "</tbody></table></div>";
        showProxyResult(html);

    }} catch(e) {{
        showProxyResult("请求失败：" + escapeHtml(String(e)));
    }}
}}

function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{
            "&":"&amp;",
            "<":"&lt;",
            ">":"&gt;",
            '"':"&quot;",
            "'":"&#39;"
        }}[c];
    }});
}}

toggleGithubProxyBox();
</script>
"""

    return body


@app.route("/proxy/new", methods=["GET", "POST"])
def proxy_new():
    if request.method == "POST":
        proxies = load_proxies()

        p = proxy_from_form()
        p["id"] = uuid.uuid4().hex
        p["created_at"] = now_str()
        p["updated_at"] = now_str()

        proxies.append(p)
        save_proxies(proxies)

        return redirect_to("proxy_page")

    return layout("新增代理", "proxy", proxy_form(mode="new"))


@app.route("/proxy/edit/<proxy_id>", methods=["GET", "POST"])
def proxy_edit(proxy_id):
    proxies = load_proxies()
    proxy = None

    for p in proxies:
        if p.get("id") == proxy_id:
            proxy = p
            break

    if not proxy:
        abort(404)

    if request.method == "POST":
        form_proxy = proxy_from_form()

        proxy["name"] = form_proxy.get("name", "")
        proxy["type"] = form_proxy.get("type", "http")
        proxy["host"] = form_proxy.get("host", "")
        proxy["port"] = form_proxy.get("port", "")
        proxy["username"] = form_proxy.get("username", "")
        proxy["password"] = form_proxy.get("password", "")
        proxy["url"] = form_proxy.get("url", "")
        proxy["enabled"] = form_proxy.get("enabled", True)
        proxy["updated_at"] = now_str()

        save_proxies(proxies)

        return redirect_to("proxy_page")

    return layout("编辑代理", "proxy", proxy_form(proxy, mode="edit"))


@app.route("/api/proxy/test-form", methods=["POST"])
def api_proxy_test_form():
    proxy = proxy_from_form()

    try:
        result = test_proxy_object(proxy)
        return jsonify({
            "ok": True,
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        })


@app.route("/api/proxy/quality-form", methods=["POST"])
def api_proxy_quality_form():
    proxy = proxy_from_form()

    try:
        items = quality_proxy_object(
            proxy,
            parse_quality_urls(request.form.get("quality_urls") or request.form.get("urls"))
        )
        return jsonify({
            "ok": True,
            "items": items,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        })


@app.route("/proxy/test-form", methods=["POST"])
def proxy_test_form():
    return redirect_to("proxy_page")


@app.route("/proxy/quality-form", methods=["POST"])
def proxy_quality_form():
    return redirect_to("proxy_page")


@app.route("/proxy/add", methods=["POST"])
def proxy_add():
    proxies = load_proxies()

    p = proxy_from_form()
    p["id"] = uuid.uuid4().hex
    p["created_at"] = now_str()
    p["updated_at"] = now_str()

    proxies.append(p)
    save_proxies(proxies)

    return redirect_to("proxy_page")


@app.route("/proxy/delete/<proxy_id>")
def proxy_delete(proxy_id):
    proxies = [p for p in load_proxies() if p.get("id") != proxy_id]
    save_proxies(proxies)
    return redirect_to("proxy_page")


def _fls_get_proxy_for_test_20260429(proxy_id):
    """
    代理测试专用读取函数。

    说明：
    - get_proxy() 会过滤已禁用代理，适合任务运行时使用；
    - 代理列表/编辑页测试需要允许测试已禁用代理；
    - 因此测试接口使用本函数，直接从代理配置中读取，不判断 enabled。
    """
    if not proxy_id:
        return None

    for proxy in load_proxies():
        if proxy.get("id") == proxy_id:
            return proxy

    return None


@app.route("/api/proxy/test/<proxy_id>")
def api_proxy_test_saved(proxy_id):
    proxy = _fls_get_proxy_for_test_20260429(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    try:
        result = test_proxy_object(proxy)
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })


@app.route("/api/proxy/quality/<proxy_id>")
def api_proxy_quality_saved(proxy_id):
    proxy = _fls_get_proxy_for_test_20260429(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    try:
        items = quality_proxy_object(
            proxy,
            parse_quality_urls(request.args.get("urls"))
        )
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "items": items,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })


# ============================================================
# 脚本管理 / 拉取 / 导入
# ============================================================
def script_safe_path(rel_path=""):
    rel_path = str(rel_path or "").strip().lstrip("/")
    target = (SCRIPT_DIR / rel_path).resolve()
    base = SCRIPT_DIR.resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("路径非法")

    return target


def script_rel_path(path):
    return str(Path(path).resolve().relative_to(SCRIPT_DIR.resolve()))


def script_download_response(file_path, download_name):
    file_path = Path(file_path)

    def generate():
        with open(file_path, "rb") as f:
            while True:
                data = f.read(1024 * 1024)
                if not data:
                    break
                yield data

    return Response(
        generate(),
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"'
        }
    )


def make_dir_tar_response(dir_path, rel_name):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    tmp.close()

    dir_path = Path(dir_path)
    arcname = dir_path.name or "scripts"

    with tarfile.open(tmp.name, "w:gz") as tar:
        tar.add(dir_path, arcname=arcname)

    filename = safe_name(rel_name or arcname) + ".tar.gz"

    def generate():
        try:
            with open(tmp.name, "rb") as f:
                yield from f
        finally:
            try:
                os.remove(tmp.name)
            except Exception:
                pass

    return Response(
        generate(),
        mimetype="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


def render_script_rows():
    rows = ""

    if not SCRIPT_DIR.exists():
        SCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    items = []

    for item in SCRIPT_DIR.rglob("*"):
        try:
            rel = script_rel_path(item)
        except Exception:
            continue

        if any(part.startswith(".") for part in Path(rel).parts):
            pass

        items.append((item.is_file(), rel, item))

    # 文件夹在前，文件在后；按路径排序
    items.sort(key=lambda x: (x[0], x[1].lower()))

    if not items:
        return '<tr><td colspan="6">暂无脚本，请点击“拉取”或“导入”添加脚本</td></tr>'

    token = tq()

    for is_file, rel, item in items:
        typ = "文件" if item.is_file() else "文件夹"
        badge = '<span class="badge blue">文件</span>' if item.is_file() else '<span class="badge green">文件夹</span>'

        if item.is_file():
            size = item.stat().st_size / 1024
            size_text = f"{size:.1f} KB"
        else:
            size_text = "-"

        mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        download_url = "/scripts/download/" + rel
        delete_url = "/scripts/delete/" + rel

        if ADMIN_TOKEN:
            download_url += f"?token={ADMIN_TOKEN}"
            delete_url += f"?token={ADMIN_TOKEN}"

        rows += f"""
<tr>
    <td>{badge}</td>
    <td><b>{h(rel)}</b></td>
    <td>{h(size_text)}</td>
    <td>{h(mtime)}</td>
    <td>{h(str(item))}</td>
    <td>
        <a class="btn btn-blue" href="{h(download_url)}">下载</a>
        <a class="btn btn-red" href="{h(delete_url)}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
    </td>
</tr>
"""

    return rows


@app.route("/pull")
def scripts_page():
    token = tq()

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">脚本管理</div>
            <div class="help">
                管理目录：<b>{h(SCRIPT_DIR)}</b><br>
                可以拉取远程脚本 / 仓库，也可以导入本地脚本或压缩包。
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-primary" href="/pull/fetch{token}">拉取</a>
            <a class="btn btn-orange" href="/pull/import{token}">导入</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">脚本列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>类型</th>
                    <th>相对路径</th>
                    <th>大小</th>
                    <th>修改时间</th>
                    <th>绝对路径</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{render_script_rows()}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令示例</div>
    <div class="code">
task 1.py<br>
task folder/main.py<br>
task demo.sh arg1 arg2<br>
task demo.js
    </div>
</div>
"""
    return layout("脚本管理", "pull", body)


def guess_filename_from_url(url):
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if not name:
        name = f"script_{int(time.time())}.py"
    return name


@app.route("/pull/fetch", methods=["GET", "POST"])
def pull_fetch_page():
    msg = ""
    proxy_options = proxy_select_options("")

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        filename = request.form.get("filename", "").strip()
        pull_type = request.form.get("pull_type", "file").strip()
        proxy_id = request.form.get("proxy_id", "").strip()

        if not url:
            msg = "URL 不能为空"
        else:
            try:
                url_for_pull = github_proxy_url(url, proxy_id)

                if pull_type == "repo":
                    repo_name = filename.strip().strip("/")

                    if not repo_name:
                        repo_name = os.path.basename(urlparse(url).path).replace(".git", "") or f"repo_{int(time.time())}"

                    target = script_safe_path(repo_name)

                    if target.exists():
                        raise FileExistsError(f"目标目录已存在：{target}")

                    git_bin = shutil.which("git")
                    if not git_bin:
                        raise RuntimeError("未安装 git，请先安装 git")

                    env = os.environ.copy()
                    env = apply_proxy_env(env, proxy_id)

                    subprocess.check_call(
                        [git_bin, "clone", url_for_pull, str(target)],
                        cwd=str(SCRIPT_DIR),
                        env=env
                    )

                    msg = f"仓库拉取成功：{target}"

                else:
                    if not filename:
                        filename = guess_filename_from_url(url)

                    target = script_safe_path(filename)
                    target.parent.mkdir(parents=True, exist_ok=True)

                    r = requests.get(
                        url_for_pull,
                        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                        timeout=60,
                        proxies=requests_proxy_dict(proxy_id)
                    )
                    r.raise_for_status()

                    target.write_bytes(r.content)
                    msg = f"文件拉取成功：{target}"

            except Exception as e:
                msg = f"拉取失败：{e}"

    token = tq()

    body = f"""
<div class="card">
    <div class="card-title">拉取脚本 / 仓库</div>
    <form method="post">
        <div class="form-item">
            <label>拉取类型</label>
            <select name="pull_type">
                <option value="file">单文件</option>
                <option value="repo">Git 仓库</option>
            </select>
        </div>

        <br>

        <div class="form-item">
            <label>URL</label>
            <input name="url" placeholder="https://example.com/test.py 或 https://github.com/user/repo.git">
        </div>

        <br>

        <div class="form-item">
            <label>保存为，相对 /root/fls/scripts</label>
            <input name="filename" placeholder="文件：1.py；仓库：repo-name。不填则自动识别">
        </div>

        <br>

        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>

        <br>
        <button class="btn btn-primary" type="submit">开始拉取</button>
        <a class="btn btn-gray" href="/pull{token}">返回脚本管理</a>
    </form>
</div>

<div class="card">
    <div class="card-title">结果</div>
    <div class="help">{h(msg or "暂无操作")}</div>
</div>

<div class="card">
    <div class="card-title">说明</div>
    <div class="code">
单文件保存到 /root/fls/scripts/1.py 后，任务命令填写：<br>
task 1.py<br><br>
仓库保存到 /root/fls/scripts/demo 后，任务命令可以填写：<br>
task demo/main.py
    </div>
</div>
"""
    return layout("拉取脚本", "pull", body)


def safe_extract_zip(zip_obj, path):
    base = Path(path).resolve()

    for member in zip_obj.infolist():
        target = (base / member.filename).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise RuntimeError("压缩包包含非法路径")

    zip_obj.extractall(path)


@app.route("/pull/import", methods=["GET", "POST"])
def pull_import_page():
    msg = ""

    if request.method == "POST":
        upload = request.files.get("file")
        save_as = request.form.get("save_as", "").strip()

        if not upload or not upload.filename:
            msg = "请选择要导入的文件"
        else:
            try:
                original_name = os.path.basename(upload.filename)
                filename = save_as or original_name

                tmp_dir = tempfile.mkdtemp()
                tmp_file = Path(tmp_dir) / original_name
                upload.save(str(tmp_file))

                lower = original_name.lower()

                if lower.endswith((".tar.gz", ".tgz", ".tar")):
                    target_dir = script_safe_path(save_as) if save_as else SCRIPT_DIR
                    target_dir.mkdir(parents=True, exist_ok=True)

                    mode = "r:gz" if lower.endswith((".tar.gz", ".tgz")) else "r:"
                    with tarfile.open(tmp_file, mode) as tar:
                        safe_extract_tar(tar, target_dir)

                    msg = f"压缩包导入成功：{target_dir}"

                elif lower.endswith(".zip"):
                    target_dir = script_safe_path(save_as) if save_as else SCRIPT_DIR
                    target_dir.mkdir(parents=True, exist_ok=True)

                    with zipfile.ZipFile(tmp_file, "r") as z:
                        safe_extract_zip(z, target_dir)

                    msg = f"ZIP 导入成功：{target_dir}"

                else:
                    target = script_safe_path(filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tmp_file, target)
                    msg = f"文件导入成功：{target}"

            except Exception as e:
                msg = f"导入失败：{e}"
            finally:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

    token = tq()

    body = f"""
<div class="card">
    <div class="card-title">导入脚本 / 压缩包</div>
    <form method="post" enctype="multipart/form-data">
        <div class="form-item">
            <label>选择文件</label>
            <input type="file" name="file">
            <div class="help">
                支持普通脚本文件，也支持 .zip / .tar / .tar.gz / .tgz 压缩包。
            </div>
        </div>

        <br>

        <div class="form-item">
            <label>保存为 / 解压到，相对 /root/fls/scripts，可空</label>
            <input name="save_as" placeholder="普通文件：1.py；压缩包：folder-name；为空则使用原文件名或解压到 scripts 根目录">
        </div>

        <br>

        <button class="btn btn-primary" type="submit">开始导入</button>
        <a class="btn btn-gray" href="/pull{token}">返回脚本管理</a>
    </form>
</div>

<div class="card">
    <div class="card-title">结果</div>
    <div class="help">{h(msg or "暂无操作")}</div>
</div>
"""
    return layout("导入脚本", "pull", body)


@app.route("/scripts/download/", defaults={"rel_path": ""})
@app.route("/scripts/download/<path:rel_path>")
def scripts_download(rel_path):
    try:
        target = script_safe_path(rel_path)

        if not target.exists():
            abort(404)

        if target.is_dir():
            return make_dir_tar_response(target, rel_path or target.name)

        return script_download_response(target, target.name)

    except Exception as e:
        return f"下载失败：{h(e)}", 400


@app.route("/scripts/delete/", defaults={"rel_path": ""})
@app.route("/scripts/delete/<path:rel_path>")
def scripts_delete(rel_path):
    try:
        target = script_safe_path(rel_path)

        if target == SCRIPT_DIR.resolve():
            return "不允许删除 scripts 根目录", 400

        if not target.exists():
            abort(404)

        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

        return redirect_to("scripts_page")

    except Exception as e:
        return f"删除失败：{h(e)}", 400


# ============================================================
# 备份恢复
# ============================================================
@app.route("/backup")
def backup_page():
    token = tq()

    body = f"""
<div class="card">
    <div class="card-title">导出面板数据</div>
    <div class="help">会导出 data、scripts 目录中的任务配置、代理配置、全局变量和脚本。</div>
    <br>
    <a class="btn btn-primary" href="/backup/export{token}">导出备份</a>
</div>

<div class="card">
    <div class="card-title">导入面板数据</div>
    <form method="post" action="/backup/import{token}" enctype="multipart/form-data">
        <input type="file" name="file" accept=".tar.gz,.tgz">
        <br><br>
        <button class="btn btn-orange" type="submit" onclick="return confirm('导入会覆盖当前 data 和 scripts，确定继续吗？')">导入备份</button>
    </form>
</div>
"""

    return layout("备份恢复", "backup", body)


@app.route("/backup/export")
def backup_export():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    tmp.close()

    deps_tmp = None

    try:
        deps_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        deps_tmp.close()

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "freeze"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=120
            )

            deps_text = result.stdout or ""
            if result.returncode != 0:
                deps_text = "# pip freeze 执行失败，以下为输出：\n" + deps_text

            Path(deps_tmp.name).write_text(deps_text, encoding="utf-8")
        except Exception as e:
            Path(deps_tmp.name).write_text(
                "# pip freeze 执行失败：{}\n".format(e),
                encoding="utf-8"
            )

        with tarfile.open(tmp.name, "w:gz") as tar:
            if DATA_DIR.exists():
                tar.add(DATA_DIR, arcname="data")
            if SCRIPT_DIR.exists():
                tar.add(SCRIPT_DIR, arcname="scripts")

            # 自动备份当前 Python 环境依赖列表。
            # 同时放在根目录和 data 目录下，方便恢复逻辑和人工查看。
            if deps_tmp and Path(deps_tmp.name).exists():
                tar.add(deps_tmp.name, arcname="dependencies.txt")
                tar.add(deps_tmp.name, arcname="data/dependencies.txt")

    finally:
        if deps_tmp:
            try:
                os.remove(deps_tmp.name)
            except Exception:
                pass

    def generate():
        try:
            with open(tmp.name, "rb") as f:
                yield from f
        finally:
            try:
                os.remove(tmp.name)
            except Exception:
                pass

    filename = f"fls-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"

    return Response(
        generate(),
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

def safe_extract_tar(tar, path):
    base = Path(path).resolve()

    for member in tar.getmembers():
        target = (base / member.name).resolve()
        if not str(target).startswith(str(base)):
            raise RuntimeError("备份文件包含非法路径")

    tar.extractall(path)


@app.route("/backup/import", methods=["POST"])
def backup_import():
    f = request.files.get("file")

    if not f:
        return "未上传文件", 400

    tmp_dir = tempfile.mkdtemp()

    try:
        backup_file = Path(tmp_dir) / "backup.tar.gz"
        f.save(str(backup_file))

        extract_dir = Path(tmp_dir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(backup_file, "r:gz") as tar:
            safe_extract_tar(tar, extract_dir)

        data_src = extract_dir / "data"
        scripts_src = extract_dir / "scripts"

        if data_src.exists():
            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR)
            shutil.copytree(data_src, DATA_DIR)

        if scripts_src.exists():
            if SCRIPT_DIR.exists():
                shutil.rmtree(SCRIPT_DIR)
            shutil.copytree(scripts_src, SCRIPT_DIR)

        reload_scheduler()
        return redirect_to("backup_page")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# 依赖管理
# ============================================================
def pip_cmd(args):
    return subprocess.run(
        [sys.executable, "-m", "pip"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600
    )


def refresh_dependency_cache():
    """
    刷新 Python 依赖缓存，尽量避免安装依赖后必须重启面板。
    对 PySocks / setproctitle 等依赖尤其有用。
    """
    import importlib
    import sys

    importlib.invalidate_caches()

    for name in [
        "socks",
        "sockshandler",
        "urllib3.contrib.socks",
        "setproctitle",
    ]:
        sys.modules.pop(name, None)

    result = {
        "time": now_str(),
        "packages": {},
    }

    for pkg, module in [
        ("flask", "flask"),
        ("requests", "requests"),
        ("apscheduler", "apscheduler"),
        ("PySocks", "socks"),
        ("setproctitle", "setproctitle"),
    ]:
        try:
            importlib.import_module(module)
            version = get_python_package_version(pkg)
            result["packages"][pkg] = version or "已安装"
        except Exception as e:
            result["packages"][pkg] = f"不可用：{e}"

    return result


def deps_log_file(install_id, package_name):
    safe_pkg = safe_name(package_name or "package")
    return LOG_DIR / f"deps-install-{safe_pkg}-{install_id}.log"


def is_deps_install_running(install_id):
    info = DEPS_RUNNING.get(install_id)

    if not info:
        return False

    proc = info.get("process")

    if not proc:
        DEPS_RUNNING.pop(install_id, None)
        return False

    if proc.poll() is None:
        return True

    fp = info.get("log_fp")
    if fp:
        try:
            fp.write(f"\n===== 依赖安装结束: {now_str()}，退出码: {proc.returncode} =====\n".encode("utf-8"))
            fp.close()
        except Exception:
            pass

    DEPS_RUNNING.pop(install_id, None)
    return False


@app.route("/deps")
def deps_page():
    package = request.args.get("package", "")

    result = pip_cmd(["list", "--format=json"])

    try:
        packages = json.loads(result.stdout)
    except Exception:
        packages = []

    rows = ""

    for p in packages:
        name = p.get("name")
        version = p.get("version")
        uninstall_url = f"/deps/uninstall?name={name}"
        if ADMIN_TOKEN:
            uninstall_url += f"&token={ADMIN_TOKEN}"

        rows += f"""
<tr>
    <td>{h(name)}</td>
    <td>{h(version)}</td>
    <td>
        <a class="btn btn-red" href="{h(uninstall_url)}" onclick="return confirm('确定卸载 {h(name)} 吗？')">卸载</a>
    </td>
</tr>
"""

    token = tq()

    body = f"""
<div class="card">
    <div class="card-title">安装依赖</div>
    <form method="post" action="/deps/install{token}">
        <input name="name" value="{h(package)}" placeholder="例如：requests 或 PySocks">
        <br><br>
        <button class="btn btn-primary" type="submit">安装并查看日志</button>
        <a class="btn btn-blue" href="/deps/refresh{token}">刷新依赖</a>
    </form>
    <div class="help">
        安装依赖会进入实时日志页面，和运行脚本一样自动刷新。<br>
        安装 PySocks、setproctitle 等依赖后，可以点击“刷新依赖”检测是否可用。
    </div>
</div>

<div class="card">
    <div class="card-title">已安装依赖</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>包名</th>
                    <th>版本</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="3">暂无依赖</td></tr>'}</tbody>
        </table>
    </div>
</div>
"""

    return layout("依赖管理", "deps", body)


@app.route("/deps/install", methods=["POST"])
def deps_install():
    name = request.form.get("name", "").strip()

    if not name:
        return "依赖名不能为空", 400

    install_id = uuid.uuid4().hex
    log_file = deps_log_file(install_id, name)
    log_fp = open(log_file, "ab", buffering=0)

    header = (
        f"===== 安装依赖: {name} =====\n"
        f"时间: {now_str()}\n"
        f"Python: {sys.executable}\n"
        f"命令: {sys.executable} -m pip install {name}\n"
        f"日志文件: {log_file}\n"
        f"============================================================\n"
    )
    log_fp.write(header.encode("utf-8"))

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", name],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
            env=os.environ.copy()
        )
    except Exception as e:
        log_fp.write(f"启动安装失败: {e}\n".encode("utf-8"))
        log_fp.close()
        return f"启动安装失败：{h(e)}", 500

    DEPS_RUNNING[install_id] = {
        "process": proc,
        "package": name,
        "log_file": str(log_file),
        "log_fp": log_fp,
        "start_time": time.time(),
    }

    return redirect_to("deps_install_log", install_id=install_id)


@app.route("/deps/install-log/<install_id>")
def deps_install_log(install_id):
    info = DEPS_RUNNING.get(install_id)
    package = info.get("package") if info else "未知依赖"
    log_file = info.get("log_file") if info else ""

    running = is_deps_install_running(install_id)

    token = tq()

    api = f"/api/deps/install-log/{install_id}"
    if ADMIN_TOKEN:
        api += f"?token={ADMIN_TOKEN}&lines=1200"
    else:
        api += "?lines=1200"

    body = f"""
<div class="card">
    <div class="card-title">依赖安装日志：{h(package)}</div>
    <div class="help">
        状态：<b id="installStatus">{"安装中" if running else "已结束"}</b><br>
        日志文件：{h(log_file or "当前进程已结束，无法定位日志")}
    </div>
    <br>
    <a class="btn btn-gray" href="/deps{token}">返回依赖管理</a>
    <a class="btn btn-blue" href="/deps/refresh{token}">刷新依赖</a>
</div>

<pre class="log" id="log">加载中...</pre>

<script>
window.__FLS_LOG_USER_NEAR_BOTTOM__ = true;

function checkNearBottom(){{
    const distance = document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
    window.__FLS_LOG_USER_NEAR_BOTTOM__ = distance < 80;
}}

window.addEventListener("scroll", checkNearBottom);

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const res = await fetch("{api}");
        const json = await res.json();

        const el = document.getElementById("log");
        el.textContent = json.log || "暂无日志";

        const st = document.getElementById("installStatus");
        st.textContent = json.running ? "安装中" : "已结束";

        if (window.__FLS_LOG_USER_NEAR_BOTTOM__) {{
            window.scrollTo(0, document.documentElement.scrollHeight);
        }} else {{
            const afterHeight = document.documentElement.scrollHeight;
            const delta = afterHeight - beforeHeight;
            window.scrollTo(0, beforeScroll + Math.max(delta, 0));
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if (window.__FLS_ACTIVE_LOG_INTERVAL__) {{
    clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
    window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
}}

loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""

    return layout("依赖安装日志", "deps", body)


@app.route("/api/deps/install-log/<install_id>")
def api_deps_install_log(install_id):
    info = DEPS_RUNNING.get(install_id)

    if not info:
        return jsonify({
            "running": False,
            "log": "安装进程已结束或面板已重启，无法通过该 ID 继续追踪。请到日志中心查看 deps-install-*.log。",
        })

    running = is_deps_install_running(install_id)
    log_file = info.get("log_file", "")
    lines = int(request.args.get("lines", "800"))
    log_text = tail_file(log_file, lines)

    if not running and not info.get("failure_notified"):
        proc = info.get("process")
        returncode = getattr(proc, "returncode", None)
        if returncode not in (0, None):
            _fls_system_failure_notify(
                "依赖安装",
                f"依赖安装失败，退出码：{returncode}",
                log_file,
                log_text,
            )
            info["failure_notified"] = True

    return jsonify({
        "running": running,
        "log": log_text,
    })


@app.route("/deps/refresh")
def deps_refresh():
    result = refresh_dependency_cache()

    rows = ""

    for name, status in result["packages"].items():
        ok = not str(status).startswith("不可用")
        badge = '<span class="badge green">可用</span>' if ok else '<span class="badge red">异常</span>'

        rows += f"""
<tr>
    <td>{h(name)}</td>
    <td>{badge}</td>
    <td>{h(status)}</td>
</tr>
"""

    body = f"""
<div class="card">
    <div class="card-title">刷新依赖完成</div>
    <div class="help">
        刷新时间：{h(result["time"])}<br>
        如果某些依赖仍显示异常，请尝试重启 FLS Manager。
    </div>
</div>

<div class="card">
    <div class="card-title">核心依赖检测</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>依赖</th>
                    <th>状态</th>
                    <th>版本 / 错误</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    <br>
    <a class="btn btn-gray" href="{url_with_token('/deps')}">返回依赖管理</a>
</div>
"""

    return layout("刷新依赖", "deps", body)


@app.route("/deps/uninstall")
def deps_uninstall():
    name = request.args.get("name", "").strip()

    if not name:
        return "依赖名不能为空", 400

    result = pip_cmd(["uninstall", "-y", name])

    body = f"""
<div class="card">
    <div class="card-title">卸载结果</div>
    <pre class="log">{h(result.stdout)}</pre>
    <a class="btn btn-gray" href="{url_with_token('/deps')}">返回</a>
</div>
"""

    return layout("卸载依赖", "deps", body)


# ============================================================
# 日志中心
# ============================================================
@app.route("/logs")
def logs_page():
    token = tq()
    files = sorted(LOG_DIR.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)

    groups = {}
    others = []

    for f in files:
        if f.name.startswith("deps-install-"):
            groups.setdefault("依赖安装", []).append(f)
            continue

        name = parse_task_name_from_log(f)

        if name:
            groups.setdefault(name, []).append(f)
        else:
            others.append(f)

    content = ""

    for task_name, log_files in groups.items():
        rows = ""

        for f in log_files:
            size = f.stat().st_size / 1024
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

            rows += f"""
<tr>
    <td>{h(f.name)}</td>
    <td>{size:.1f} KB</td>
    <td>{h(mtime)}</td>
    <td>
        <a class="btn btn-orange" href="/logfile/{h(f.name)}{token}">查看</a>
        <a class="btn btn-red" href="/logfile/delete/{h(f.name)}{token}" onclick="return confirm('确定删除日志 {h(f.name)} 吗？')">删除</a>
    </td>
</tr>
"""

        content += f"""
<div class="card">
    <div class="card-title">任务：{h(task_name)}</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>日志文件</th>
                    <th>大小</th>
                    <th>修改时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>
"""

    if others:
        rows = ""

        for f in others:
            size = f.stat().st_size / 1024
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

            rows += f"""
<tr>
    <td>{h(f.name)}</td>
    <td>{size:.1f} KB</td>
    <td>{h(mtime)}</td>
    <td>
        <a class="btn btn-orange" href="/logfile/{h(f.name)}{token}">查看</a>
        <a class="btn btn-red" href="/logfile/delete/{h(f.name)}{token}" onclick="return confirm('确定删除日志 {h(f.name)} 吗？')">删除</a>
    </td>
</tr>
"""

        content += f"""
<div class="card">
    <div class="card-title">其他日志</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>日志文件</th>
                    <th>大小</th>
                    <th>修改时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>
"""

    if not content:
        content = """
<div class="card">
    <div class="card-title">日志中心</div>
    <div class="help">暂无日志</div>
</div>
"""

    return layout("日志中心", "logs", content)


@app.route("/logfile/<filename>")
def logfile_view(filename):
    filename = os.path.basename(filename)
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    content = tail_file(str(file_path), 1500)
    token = tq()

    body = f"""
<div class="card">
    <div class="card-title">日志文件：{h(filename)}</div>
    <a class="btn btn-gray" href="/logs{token}">返回</a>
    <a class="btn btn-red" href="/logfile/delete/{h(filename)}{token}" onclick="return confirm('确定删除日志 {h(filename)} 吗？')">删除此日志</a>
</div>
<pre class="log">{h(content)}</pre>
"""
    return layout("日志文件", "logs", body)


@app.route("/logfile/delete/<filename>")
def logfile_delete(filename):
    filename = os.path.basename(filename)
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    try:
        file_path.unlink()
    except Exception as e:
        return f"删除日志失败：{h(e)}", 400

    return redirect_to("logs_page")


@app.route("/api/task/action/<action>/<task_id>", methods=["POST", "GET"])
def api_task_action(action, task_id):
    """
    任务无感操作 API：
    run / stop / toggle / delete
    """
    try:
        if action == "run":
            ok, msg = run_task_now(task_id, source="manual")
            return jsonify({"ok": ok, "msg": msg})

        if action == "stop":
            ok, msg = stop_task_now(task_id)
            return jsonify({"ok": ok, "msg": msg})

        if action == "toggle":
            tasks = load_tasks()
            found = False

            for task in tasks:
                if task.get("id") == task_id:
                    task["enabled"] = not task.get("enabled", True)
                    task["updated_at"] = now_str()
                    found = True
                    break

            if not found:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            save_tasks(tasks)
            reload_scheduler()
            return jsonify({"ok": True, "msg": "已切换"})

        if action == "delete":
            stop_task_now(task_id)
            tasks = [t for t in load_tasks() if t.get("id") != task_id]
            save_tasks(tasks)
            reload_scheduler()
            return jsonify({"ok": True, "msg": "已删除"})

        return jsonify({"ok": False, "msg": "未知操作"}), 400

    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# API
# ============================================================
@app.route("/api/status")
def api_status():
    result = []

    for t in load_tasks():
        task_id = t["id"]
        running = is_running(task_id)

        result.append({
            "id": task_id,
            "name": t.get("name"),
            "command": t.get("command"),
            "cron": t.get("cron"),
            "enabled": t.get("enabled", True),
            "running": running,
            "run_count": int(t.get("run_count", 0)),
            "pid": RUNNING.get(task_id, {}).get("pid") if running else None,
            "process_name": RUNNING.get(task_id, {}).get("process_name") if running else safe_process_name(t.get("name") or t.get("command")),
        })

    return jsonify(result)


@app.route("/api/scheduler/jobs")
def api_scheduler_jobs():
    result = []

    try:
        for job in scheduler.get_jobs():
            result.append({
                "id": job.id,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
    except Exception as e:
        return jsonify({
            "ok": False,
            "msg": str(e),
            "jobs": [],
        }), 500

    return jsonify({
        "ok": True,
        "jobs": result,
    })


def get_task_next_run_time_text(task):
    """
    获取任务下次执行时间文本。
    - 无 cron：返回 -
    - 已禁用：返回 已禁用
    - cron 不合法：返回 Cron无效
    - 已启用但未加载到 scheduler：返回 未加载
    - 已加载：返回格式化时间
    """
    try:
        cron_expr = str((task or {}).get("cron", "") or "").strip()
        if not cron_expr:
            return "-"

        try:
            cron_to_trigger(cron_expr)
        except Exception:
            return "Cron无效"

        if not (task or {}).get("enabled", True):
            return "已禁用"

        task_id = (task or {}).get("id")
        if not task_id:
            return "未加载"

        job = scheduler.get_job(f"task_{task_id}")
        if not job:
            return "未加载"

        next_run = job.next_run_time
        if not next_run:
            return "未加载"

        try:
            return next_run.astimezone(FLS_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(next_run)
    except Exception:
        return "未加载"


# ============================================================
# 关于
# ============================================================
def get_python_package_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def get_cmd_version(cmd):
    path = shutil.which(cmd)

    if not path:
        return None

    try:
        r = subprocess.run(
            [cmd, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5
        )
        return r.stdout.strip().splitlines()[0]
    except Exception:
        return None


def version_row(name, version, install_package=None):
    if version:
        status = h(version)
        action = ""
    else:
        status = "None / 未安装"
        if install_package:
            url = f"/deps?package={install_package}"
            if ADMIN_TOKEN:
                url += f"&token={ADMIN_TOKEN}"
            action = f'<a class="btn btn-primary" href="{h(url)}">安装</a>'
        else:
            action = ""

    return f"""
<tr>
    <td>{h(name)}</td>
    <td>{status}</td>
    <td>{action}</td>
</tr>
"""


@app.route("/about")
def about():
    versions_html = ""
    versions_html += version_row("Flask", get_python_package_version("flask"), "flask")
    versions_html += version_row("Requests", get_python_package_version("requests"), "requests")
    versions_html += version_row("APScheduler", get_python_package_version("apscheduler"), "apscheduler")
    versions_html += version_row("setproctitle", get_python_package_version("setproctitle"), "setproctitle")
    versions_html += version_row("PySocks", get_python_package_version("PySocks"), "PySocks")
    versions_html += version_row("Python", sys.version.split()[0], None)
    versions_html += version_row("Node", get_cmd_version("node"), None)
    versions_html += version_row("Git", get_cmd_version("git"), None)
    versions_html += version_row("Bash", get_cmd_version("bash"), None)

    body = f"""
<div class="card">
    <div class="card-title">关于 FLS 面板</div>
    <div class="help">
        <p>主进程名：<b>{h(MAIN_PROCESS_NAME)}</b></p>
        <p>任务进程名前缀：<b>{h(TASK_PROCESS_PREFIX)}</b></p>
        <p>工作目录：<b>{h(BASE_DIR)}</b></p>
        <p>数据目录：<b>{h(DATA_DIR)}</b></p>
        <p>日志目录：<b>{h(LOG_DIR)}</b></p>
        <p>脚本目录：<b>{h(SCRIPT_DIR)}</b></p>
        <p>Python：<b>{h(PYTHON_BIN)}</b></p>
        <p>Bash：<b>{h(BASH_BIN)}</b></p>
        <p>Node：<b>{h(NODE_BIN)}</b></p>
        <p>鉴权：<b>{"已开启" if ADMIN_TOKEN else "未开启"}</b></p>
    </div>
</div>

<div class="card">
    <div class="card-title">功能依赖版本</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>功能 / 依赖</th>
                    <th>版本</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{versions_html}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">查看进程名</div>
    <div class="code">
ps -ef | grep fls<br>
ps -eo pid,ppid,comm,args | grep fls
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令规则</div>
    <div class="code">
task 1.py  => /root/fls/scripts/1.py<br>
task a/test.sh => /root/fls/scripts/a/test.sh<br>
task demo.js => /root/fls/scripts/demo.js<br><br>
不加 task 则作为系统命令：<br>
python3 /root/test.py
    </div>
</div>

<div class="card">
    <div class="card-title">Cron 说明</div>
    <div class="code">
留空：手动任务<br><br>
5 位：分 时 日 月 周<br>
0 8 * * *     每天 08:00<br>
*/10 * * * *  每 10 分钟<br><br>
6 位：秒 分 时 日 月 周<br>
0 0 8 * * *   每天 08:00:00
    </div>
</div>
"""
    return layout("关于", "about", body)


# ============================================================
# 脚本管理：文件管理器模式
# - /pull?p=目录
# - 文件夹可打开
# - 第一行 ".." 返回上级
# ============================================================
from urllib.parse import quote as _fls_quote

def _fls_url_token_join(path):
    if ADMIN_TOKEN:
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}token={ADMIN_TOKEN}"
    return path


def _fls_script_url(rel):
    rel = str(rel or "").strip().strip("/")
    if not rel:
        return _fls_url_token_join("/pull")
    return _fls_url_token_join("/pull?p=" + _fls_quote(rel))


def _fls_script_download_url(rel):
    rel = str(rel or "").strip().strip("/")
    url = "/scripts/download/" + _fls_quote(rel)
    return _fls_url_token_join(url)


def _fls_script_delete_url(rel):
    rel = str(rel or "").strip().strip("/")
    url = "/scripts/delete/" + _fls_quote(rel)
    return _fls_url_token_join(url)


def _fls_rel_or_empty(path):
    try:
        rel = script_rel_path(path)
        if rel == ".":
            return ""
        return rel
    except Exception:
        return ""


def _fls_breadcrumb(current_rel):
    parts = [p for p in Path(current_rel).parts if p not in ("", ".")]
    html_items = [f'<a href="{h(_fls_script_url(""))}">scripts</a>']

    acc = []
    for p in parts:
        acc.append(p)
        rel = "/".join(acc)
        html_items.append(f'<a href="{h(_fls_script_url(rel))}">{h(p)}</a>')

    return " / ".join(html_items)


def _fls_render_file_manager_rows(current_rel=""):
    current_rel = str(current_rel or "").strip().strip("/")

    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    if not current_dir.is_dir():
        current_dir = current_dir.parent
        current_rel = _fls_rel_or_empty(current_dir)

    rows = ""

    # 第一行：.. 返回上级
    if current_dir.resolve() != SCRIPT_DIR.resolve():
        parent = current_dir.parent
        parent_rel = _fls_rel_or_empty(parent)

        rows += f"""
<tr>
    <td><span class="badge gray">返回</span></td>
    <td>
        <a href="{h(_fls_script_url(parent_rel))}" style="font-weight:900;font-size:16px;">..</a>
    </td>
    <td>-</td>
    <td>-</td>
    <td>{h(str(parent))}</td>
    <td>
        <a class="btn btn-gray" href="{h(_fls_script_url(parent_rel))}">返回上级</a>
    </td>
</tr>
"""

    try:
        items = list(current_dir.iterdir())
    except Exception:
        items = []

    # 文件夹在前，文件在后
    items.sort(key=lambda x: (x.is_file(), x.name.lower()))

    if not items and not rows:
        return '<tr><td colspan="6">暂无脚本，请点击“拉取”或“导入”添加脚本</td></tr>'

    if not items and rows:
        rows += '<tr><td colspan="6">当前目录为空</td></tr>'
        return rows

    for item in items:
        try:
            rel = script_rel_path(item)
        except Exception:
            continue

        is_dir = item.is_dir()

        badge = '<span class="badge green">文件夹</span>' if is_dir else '<span class="badge blue">文件</span>'

        if is_dir:
            size_text = "-"
            name_html = f'<a href="{h(_fls_script_url(rel))}" style="font-weight:800;">📁 {h(item.name)}</a>'
            open_btn = f'<a class="btn btn-primary" href="{h(_fls_script_url(rel))}">打开</a>'
        else:
            size = item.stat().st_size / 1024
            size_text = f"{size:.1f} KB"
            name_html = f'<b>📄 {h(item.name)}</b>'
            open_btn = f'<a class="btn btn-blue" href="{h(_fls_script_download_url(rel))}">下载</a>'

        mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        rows += f"""
<tr>
    <td>{badge}</td>
    <td>
        {name_html}
        <div class="help">{h(rel)}</div>
    </td>
    <td>{h(size_text)}</td>
    <td>{h(mtime)}</td>
    <td>{h(str(item))}</td>
    <td>
        {open_btn}
        <a class="btn btn-red" href="{h(_fls_script_delete_url(rel))}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
    </td>
</tr>
"""

    return rows
pass


# ============================================================
# 日志悬浮按钮 / 新日志提示 / 全局变量表格化管理
#
# ============================================================

def _fls_log_float_style():
    return """
<style>
.fls-log-float {
    position: fixed;
    right: 14px;
    bottom: 90px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.fls-log-float button,
.fls-log-new-tip {
    border: 0;
    border-radius: 999px;
    box-shadow: 0 6px 20px rgba(0,0,0,.18);
    cursor: pointer;
}
.fls-log-float button {
    width: 42px;
    height: 42px;
    background: #111827;
    color: #fff;
    font-size: 18px;
    font-weight: 900;
}
.fls-log-float button:hover {
    background: #18a058;
}
.fls-log-new-tip {
    position: fixed;
    right: 14px;
    bottom: 22px;
    z-index: 10000;
    display: none;
    background: #18a058;
    color: #fff;
    padding: 10px 14px;
    font-size: 14px;
    font-weight: 800;
}
@media(max-width:768px) {
    .fls-log-float {
        right: 10px;
        bottom: 82px;
    }
    .fls-log-float button {
        width: 40px;
        height: 40px;
    }
    .fls-log-new-tip {
        right: 10px;
        bottom: 18px;
        font-size: 13px;
    }
}
</style>
"""


def _fls_log_float_script_text(api_url):
    return f"""
<script>
(function(){{
    const apiUrl = "{h(api_url)}";
    const logEl = document.getElementById("log");
    const tipEl = document.getElementById("flsLogNewTip");

    if(!logEl) return;

    window.__FLS_LOG_USER_NEAR_BOTTOM__ = true;
    window.__FLS_LOG_LAST_TEXT__ = "";
    window.__FLS_LOG_LOADED_ONCE__ = false;

    function nearBottom(){{
        const distance = document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
        return distance < 90;
    }}

    function updateNearBottom(){{
        window.__FLS_LOG_USER_NEAR_BOTTOM__ = nearBottom();
    }}

    function goTop(){{
        window.scrollTo({{top: 0, behavior: "smooth"}});
    }}

    function goBottom(){{
        if(tipEl) tipEl.style.display = "none";
        window.__FLS_LOG_USER_NEAR_BOTTOM__ = true;
        window.scrollTo({{top: document.documentElement.scrollHeight, behavior: "smooth"}});
    }}

    window.flsLogGoTop = goTop;
    window.flsLogGoBottom = goBottom;

    window.addEventListener("scroll", updateNearBottom, {{passive:true}});

    async function loadLog(){{
        try {{
            const beforeScroll = window.scrollY;
            const beforeHeight = document.documentElement.scrollHeight;
            const wasNearBottom = nearBottom();

            const res = await fetch(apiUrl, {{
                cache: "no-store",
                headers: {{"X-Requested-With": "XMLHttpRequest"}}
            }});

            const text = await res.text();
            const oldText = window.__FLS_LOG_LAST_TEXT__ || "";
            const changed = text !== oldText;

            logEl.textContent = text || "暂无日志";
            window.__FLS_LOG_LAST_TEXT__ = text;

            if(!window.__FLS_LOG_LOADED_ONCE__){{
                window.__FLS_LOG_LOADED_ONCE__ = true;
                window.scrollTo(0, document.documentElement.scrollHeight);
                return;
            }}

            if(changed){{
                if(wasNearBottom || window.__FLS_LOG_USER_NEAR_BOTTOM__){{
                    if(tipEl) tipEl.style.display = "none";
                    window.scrollTo(0, document.documentElement.scrollHeight);
                }} else {{
                    const afterHeight = document.documentElement.scrollHeight;
                    const delta = afterHeight - beforeHeight;
                    window.scrollTo(0, beforeScroll + Math.max(delta, 0));
                    if(tipEl) tipEl.style.display = "block";
                }}
            }}
        }} catch(e) {{
            logEl.textContent = "日志读取失败: " + e;
        }}
    }}

    if(window.__FLS_ACTIVE_LOG_INTERVAL__){{
        clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
        window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    }}

    loadLog();
    window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
}})();
</script>
"""


def _fls_log_float_script_json(api_url):
    return f"""
<script>
(function(){{
    const apiUrl = "{h(api_url)}";
    const logEl = document.getElementById("log");
    const tipEl = document.getElementById("flsLogNewTip");
    const statusEl = document.getElementById("installStatus");

    if(!logEl) return;

    window.__FLS_LOG_USER_NEAR_BOTTOM__ = true;
    window.__FLS_LOG_LAST_TEXT__ = "";
    window.__FLS_LOG_LOADED_ONCE__ = false;

    function nearBottom(){{
        const distance = document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
        return distance < 90;
    }}

    function updateNearBottom(){{
        window.__FLS_LOG_USER_NEAR_BOTTOM__ = nearBottom();
    }}

    function goTop(){{
        window.scrollTo({{top: 0, behavior: "smooth"}});
    }}

    function goBottom(){{
        if(tipEl) tipEl.style.display = "none";
        window.__FLS_LOG_USER_NEAR_BOTTOM__ = true;
        window.scrollTo({{top: document.documentElement.scrollHeight, behavior: "smooth"}});
    }}

    window.flsLogGoTop = goTop;
    window.flsLogGoBottom = goBottom;

    window.addEventListener("scroll", updateNearBottom, {{passive:true}});

    async function loadLog(){{
        try {{
            const beforeScroll = window.scrollY;
            const beforeHeight = document.documentElement.scrollHeight;
            const wasNearBottom = nearBottom();

            const res = await fetch(apiUrl, {{
                cache: "no-store",
                headers: {{"X-Requested-With": "XMLHttpRequest"}}
            }});

            const json = await res.json();
            const text = json.log || "暂无日志";

            if(statusEl){{
                statusEl.textContent = json.running ? "安装中" : "已结束";
            }}

            const oldText = window.__FLS_LOG_LAST_TEXT__ || "";
            const changed = text !== oldText;

            logEl.textContent = text;
            window.__FLS_LOG_LAST_TEXT__ = text;

            if(!window.__FLS_LOG_LOADED_ONCE__){{
                window.__FLS_LOG_LOADED_ONCE__ = true;
                window.scrollTo(0, document.documentElement.scrollHeight);
                return;
            }}

            if(changed){{
                if(wasNearBottom || window.__FLS_LOG_USER_NEAR_BOTTOM__){{
                    if(tipEl) tipEl.style.display = "none";
                    window.scrollTo(0, document.documentElement.scrollHeight);
                }} else {{
                    const afterHeight = document.documentElement.scrollHeight;
                    const delta = afterHeight - beforeHeight;
                    window.scrollTo(0, beforeScroll + Math.max(delta, 0));
                    if(tipEl) tipEl.style.display = "block";
                }}
            }}
        }} catch(e) {{
            logEl.textContent = "日志读取失败: " + e;
        }}
    }}

    if(window.__FLS_ACTIVE_LOG_INTERVAL__){{
        clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
        window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    }}

    loadLog();
    window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
}})();
</script>
"""


def _fls_log_float_controls():
    return """
<div class="fls-log-float">
    <button type="button" onclick="flsLogCopyAll()" title="复制全部日志">⧉</button>
    <button type="button" onclick="flsLogGoTop()" title="回到顶部">↑</button>
    <button type="button" onclick="flsLogGoBottom()" title="到底部">↓</button>
</div>
<button type="button" class="fls-log-new-tip" id="flsLogNewTip" onclick="flsLogGoBottom()">有新日志，点击到底部</button>

<script>
function flsLogCopyAll(){
    var el = document.getElementById("log");

    if(!el){
        alert("未找到日志内容");
        return;
    }

    var text = el.textContent || el.innerText || "";

    if(!text){
        alert("暂无日志可复制");
        return;
    }

    function ok(){
        alert("已复制全部日志");
    }

    function fallbackCopy(){
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "readonly");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        ta.style.top = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();

        try{
            document.execCommand("copy");
            ok();
        }catch(e){
            alert("复制失败，请手动复制");
        }

        document.body.removeChild(ta);
    }

    if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(ok).catch(fallbackCopy);
    }else{
        fallbackCopy();
    }
}
</script>
"""


# ============================================================
# 任务日志页：悬浮按钮和新日志提示
# ============================================================
def _fls_patched_log_view(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    running = is_running(task_id)

    if running:
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
        process_name = RUNNING.get(task_id, {}).get("process_name", "")
        pid = RUNNING.get(task_id, {}).get("pid", "")
    else:
        log_file = latest_log_for_task(task)
        process_name = safe_process_name(task.get("name") or task.get("command") or task_id)
        pid = ""

    token = tq()

    api = f"/api/log/{task_id}"
    if ADMIN_TOKEN:
        api += f"?token={ADMIN_TOKEN}&lines=1200"
    else:
        api += "?lines=1200"

    body = f"""
{_fls_log_float_style()}
<div class="card">
    <div class="card-title">日志：{h(task.get('name') or task.get('command'))}</div>
    <div class="help">
        状态：<b>{"运行中" if running else "已停止"}</b><br>
        PID：{h(pid or "-")}<br>
        进程名：{h(process_name)}<br>
        日志文件：{h(log_file or "暂无")}
    </div>
    <br>
    <a class="btn btn-primary" href="/run/{h(task_id)}{token}">运行</a>
    <a class="btn btn-red" href="/stop/{h(task_id)}{token}" onclick="return confirm('确定结束该任务吗？')">结束</a>
    <a class="btn btn-gray" href="/tasks{token}">返回</a>
</div>

<pre class="log" id="log">加载中...</pre>
{_fls_log_float_controls()}
{_fls_log_float_script_text(api)}
"""
    return layout("任务日志", "logs", body)


app.view_functions["log_view"] = _fls_patched_log_view
# ============================================================
# 依赖安装日志页：悬浮按钮和新日志提示
# ============================================================
def _fls_patched_deps_install_log(install_id):
    info = DEPS_RUNNING.get(install_id)
    package = info.get("package") if info else "未知依赖"
    log_file = info.get("log_file") if info else ""

    running = is_deps_install_running(install_id)

    token = tq()

    api = f"/api/deps/install-log/{install_id}"
    if ADMIN_TOKEN:
        api += f"?token={ADMIN_TOKEN}&lines=1200"
    else:
        api += "?lines=1200"

    body = f"""
{_fls_log_float_style()}
<div class="card">
    <div class="card-title">依赖安装日志：{h(package)}</div>
    <div class="help">
        状态：<b id="installStatus">{"安装中" if running else "已结束"}</b><br>
        日志文件：{h(log_file or "当前进程已结束，无法定位日志")}
    </div>
    <br>
    <a class="btn btn-gray" href="/deps{token}">返回依赖管理</a>
    <a class="btn btn-blue" href="/deps/refresh{token}">刷新依赖</a>
</div>

<pre class="log" id="log">加载中...</pre>
{_fls_log_float_controls()}
{_fls_log_float_script_json(api)}
"""
    return layout("依赖安装日志", "deps", body)


app.view_functions["deps_install_log"] = _fls_patched_deps_install_log
# ============================================================
# 日志文件 API 与日志文件查看页
# 静态日志文件如果后续被追加，也会异步显示新内容提示
# ============================================================
@app.route("/api/logfile/<filename>")
def _fls_api_logfile_tail(filename):
    filename = os.path.basename(filename)
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    lines = int(request.args.get("lines", "1500"))

    return Response(
        tail_file(str(file_path), lines),
        mimetype="text/plain; charset=utf-8"
    )


def _fls_patched_logfile_view(filename):
    filename = os.path.basename(filename)
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    token = tq()

    api = f"/api/logfile/{filename}"
    if ADMIN_TOKEN:
        api += f"?token={ADMIN_TOKEN}&lines=1500"
    else:
        api += "?lines=1500"

    body = f"""
{_fls_log_float_style()}
<div class="card">
    <div class="card-title">日志文件：{h(filename)}</div>
    <a class="btn btn-gray" href="/logs{token}">返回</a>
    <a class="btn btn-red" href="/logfile/delete/{h(filename)}{token}" onclick="return confirm('确定删除日志 {h(filename)} 吗？')">删除此日志</a>
</div>
<pre class="log" id="log">加载中...</pre>
{_fls_log_float_controls()}
{_fls_log_float_script_text(api)}
"""
    return layout("日志文件", "logs", body)


app.view_functions["logfile_view"] = _fls_patched_logfile_view
# ============================================================
# 全局变量：列表 / 查看 / 编辑 / 删除 / 导入任务变量
# ============================================================


def fls_collapsible_text(value, limit=50):
    """
    长文本折叠显示：
    - 未展开时只显示前 limit 个字符 + ...
    - 展开后隐藏省略预览，只显示完整内容
    - 避免展开后复制时把预览片段和完整内容重复复制
    - 不注入 JavaScript
    """
    raw = str(value if value is not None else "")

    if len(raw) <= int(limit):
        return f"<code>{h(raw)}</code>"

    short = raw[:int(limit)] + "..."

    style = (
        "<style>"
        ".fls-collapsible-text[open] summary .fls-collapsed-preview{display:none;}"
        ".fls-collapsible-text[open] summary::after{content:'点击收起';color:#6b7280;font-size:12px;}"
        ".fls-collapsible-text summary{cursor:pointer;list-style:none;}"
        ".fls-collapsible-text summary::-webkit-details-marker{display:none;}"
        "</style>"
    )

    return (
        style
        + '<details class="fls-collapsible-text" style="display:inline-block;max-width:100%;">'
        + f'<summary><code class="fls-collapsed-preview">{h(short)}</code></summary>'
        + f'<code style="white-space:pre-wrap;word-break:break-all;">{h(raw)}</code>'
        + '</details>'
    )

def _fls_env_rows():
    env = load_global_env()
    token = tq()

    if not env:
        return '<tr><td colspan="3">暂无全局变量</td></tr>'

    rows = ""

    for k in sorted(env.keys()):
        v = env.get(k, "")
        edit_url = f"/env/edit/{k}"
        delete_url = f"/env/delete/{k}"

        if ADMIN_TOKEN:
            edit_url += f"?token={ADMIN_TOKEN}"
            delete_url += f"?token={ADMIN_TOKEN}"

        rows += f"""
<tr>
    <td><b>{h(k)}</b></td>
    <td>{fls_collapsible_text(v, 50)}</td>
    <td>
        <a class="btn btn-blue" href="{h(edit_url)}">编辑</a>
        <a class="btn btn-red" href="{h(delete_url)}" onclick="return confirm('确定删除变量 {h(k)} 吗？')">删除</a>
    </td>
</tr>
"""
    return rows


def _fls_patched_global_env_page():
    token = tq()

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">全局变量列表</div>
            <div class="help">
                全局变量对所有任务生效；任务变量中同名变量会覆盖全局变量。
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-blue" href="/env/view{token}">查看</a>
            <a class="btn btn-orange" href="/env/import{token}">导入</a>
            <a class="btn btn-primary" href="/env/new{token}">新增变量</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">变量列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>变量名</th>
                    <th>值</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{_fls_env_rows()}</tbody>
        </table>
    </div>
</div>
"""
    return layout("全局变量", "env", body)


app.view_functions["global_env_page"] = _fls_patched_global_env_page
@app.route("/env/view", methods=["GET", "POST"])
def _fls_env_view_all():
    if request.method == "POST":
        env = parse_env_text(request.form.get("env_text", ""))
        save_global_env(env)
        return redirect_to("global_env_page")

    env_text = env_to_text(load_global_env())
    token = tq()

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">查看全部全局变量</div>
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
    <div class="help">
        可一次性查看和编辑全部全局变量，保存后会整体覆盖。
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存全部</button>
    <a class="btn btn-gray" href="/env{token}">返回列表</a>
</div>
</form>
"""
    return layout("查看全部全局变量", "env", body)


@app.route("/env/new", methods=["GET", "POST"])
def _fls_env_new():
    if request.method == "POST":
        key = request.form.get("key", "").strip()
        value = request.form.get("value", "")

        if not key:
            return "变量名不能为空", 400

        env = load_global_env()
        env[key] = value
        save_global_env(env)

        return redirect_to("global_env_page")

    token = tq()

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">新增全局变量</div>
    <div class="form-grid">
        <div class="form-item">
            <label>变量名</label>
            <input name="key" placeholder="例如：TOKEN">
        </div>
        <div class="form-item">
            <label>变量值</label>
            <input name="value" placeholder="变量值">
        </div>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/env{token}">返回</a>
</div>
</form>
"""
    return layout("新增全局变量", "env", body)


@app.route("/env/edit/<key>", methods=["GET", "POST"])
def _fls_env_edit(key):
    env = load_global_env()

    if key not in env:
        abort(404)

    if request.method == "POST":
        new_key = request.form.get("key", "").strip()
        value = request.form.get("value", "")

        if not new_key:
            return "变量名不能为空", 400

        if new_key != key:
            env.pop(key, None)

        env[new_key] = value
        save_global_env(env)

        return redirect_to("global_env_page")

    token = tq()

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">编辑全局变量</div>
    <div class="form-grid">
        <div class="form-item">
            <label>变量名</label>
            <input name="key" value="{h(key)}">
        </div>
        <div class="form-item">
            <label>变量值</label>
            <input name="value" value="{h(env.get(key, ''))}">
        </div>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/env{token}">返回</a>
</div>
</form>
"""
    return layout("编辑全局变量", "env", body)


@app.route("/env/delete/<key>")
def _fls_env_delete(key):
    env = load_global_env()

    if key in env:
        env.pop(key, None)
        save_global_env(env)

    return redirect_to("global_env_page")


def _fls_collect_task_env_rows():
    tasks = load_tasks()
    global_env = load_global_env()
    rows = ""

    for task in tasks:
        task_name = task.get("name") or task.get("command") or task.get("id")
        task_env = task.get("env", {}) or {}

        if not task_env:
            continue

        for k in sorted(task_env.keys()):
            v = task_env.get(k, "")
            exists = k in global_env
            exists_badge = '<span class="badge orange">将覆盖</span>' if exists else '<span class="badge green">新增</span>'

            rows += f"""
<tr>
    <td><input type="checkbox" name="items" value="{h(task.get('id'))}::{h(k)}" checked style="width:auto;"></td>
    <td>{h(task_name)}</td>
    <td><b>{h(k)}</b></td>
    <td><code>{h(v)}</code></td>
    <td>{exists_badge}</td>
</tr>
"""

    if not rows:
        rows = '<tr><td colspan="5">所有任务都没有单独设置变量</td></tr>'

    return rows


@app.route("/env/import", methods=["GET", "POST"])
def _fls_env_import_from_tasks():
    if request.method == "POST":
        selected = request.form.getlist("items")
        overwrite = request.form.get("overwrite") == "1"

        tasks = load_tasks()
        task_map = {t.get("id"): t for t in tasks}

        env = load_global_env()
        imported = 0
        skipped = 0

        for item in selected:
            if "::" not in item:
                continue

            task_id, key = item.split("::", 1)
            task = task_map.get(task_id)

            if not task:
                continue

            task_env = task.get("env", {}) or {}

            if key not in task_env:
                continue

            if key in env and not overwrite:
                skipped += 1
                continue

            env[key] = task_env[key]
            imported += 1

        save_global_env(env)

        return redirect(url_for("global_env_page", token=ADMIN_TOKEN) if ADMIN_TOKEN else url_for("global_env_page"))

    token = tq()

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">从任务变量导入到全局变量</div>
    <div class="help">
        选择要导入的任务变量。默认勾选全部。<br>
        如果变量名已存在，勾选“允许覆盖”才会覆盖全局变量。
    </div>
    <br>
    <label>
        <input type="checkbox" name="overwrite" value="1" style="width:auto;">
        允许覆盖已有全局变量
    </label>
</div>

<div class="card">
    <div class="card-title">可导入变量</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>选择</th>
                    <th>任务</th>
                    <th>变量名</th>
                    <th>值</th>
                    <th>导入状态</th>
                </tr>
            </thead>
            <tbody>{_fls_collect_task_env_rows()}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">导入所选变量</button>
    <a class="btn btn-gray" href="/env{token}">返回</a>
</div>
</form>
"""
    return layout("导入全局变量", "env", body)


# ============================================================
# FLS 扩展补丁：
# - 登录页 / Token Cookie 登录
# - 配置页
# - task 支持 ts / ps1 / bat，并可配置启用
# - 备份恢复兼容 zip / gz / tgz / tar.gz / rar / 7z 等
# - 日志清理策略可配置
# - Node 安装入口
# - 面板状态页
# ============================================================

import platform
import gzip
import resource
from flask import session, make_response

CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "admin_token": ADMIN_TOKEN,
    "port": PORT,
    "log_cleanup_minutes": LOG_CLEANUP_INTERVAL_MINUTES,
    "log_max_size_mb": int(LOG_MAX_SIZE_BYTES / 1024 / 1024),
    "log_keep_per_task": LOG_KEEP_PER_TASK,
    "task_types": {}
}


def _fls_cmd_exists(cmd):
    try:
        return bool(shutil.which(cmd))
    except Exception:
        return False


def _fls_detect_default_task_types():
    is_win = os.name == "nt"
    return {
        "py": True,
        "sh": (not is_win) and _fls_cmd_exists(BASH_BIN),
        "js": _fls_cmd_exists(NODE_BIN),
        "ts": _fls_cmd_exists("tsx") or _fls_cmd_exists("ts-node"),
        "ps1": is_win and (_fls_cmd_exists("powershell") or _fls_cmd_exists("pwsh")),
        "bat": is_win,
        "php": _fls_cmd_exists("php"),
        "rb": _fls_cmd_exists("ruby"),
        "pl": _fls_cmd_exists("perl"),
        "lua": _fls_cmd_exists("lua"),
        "jar": _fls_cmd_exists("java"),
    }

def load_config():
    cfg = read_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}

    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)

    detected = _fls_detect_default_task_types()
    user_types = merged.get("task_types")
    if not isinstance(user_types, dict):
        user_types = {}

    for k, v in detected.items():
        if k not in user_types:
            user_types[k] = bool(v)

    # 非 Windows 默认不启用 ps1 / bat，除非用户手动保存启用且系统工具存在。
    if os.name != "nt":
        if not (_fls_cmd_exists("pwsh") or _fls_cmd_exists("powershell")):
            user_types["ps1"] = False
        user_types["bat"] = False

    # bash / node 不存在时强制禁用相关类型，避免恢复别的平台备份后误启用。
    if not _fls_cmd_exists(BASH_BIN):
        user_types["sh"] = False

    if not _fls_cmd_exists(NODE_BIN):
        user_types["js"] = False
        if not (_fls_cmd_exists("tsx") or _fls_cmd_exists("ts-node")):
            user_types["ts"] = False

    # 额外脚本类型依赖对应运行器；不存在时强制禁用。
    for _suffix, _runner in [
        ("php", "php"),
        ("rb", "ruby"),
        ("pl", "perl"),
        ("lua", "lua"),
        ("jar", "java"),
    ]:
        if not _fls_cmd_exists(_runner):
            user_types[_suffix] = False

    merged["task_types"] = user_types

    try:
        merged["port"] = max(1, min(65535, int(merged.get("port", PORT))))
    except Exception:
        merged["port"] = PORT

    try:
        merged["log_cleanup_minutes"] = max(1, min(1440, int(merged.get("log_cleanup_minutes", 30))))
    except Exception:
        merged["log_cleanup_minutes"] = 30

    try:
        merged["log_max_size_mb"] = max(1, int(merged.get("log_max_size_mb", 10)))
    except Exception:
        merged["log_max_size_mb"] = 10

    try:
        merged["log_keep_per_task"] = max(1, int(merged.get("log_keep_per_task", 10)))
    except Exception:
        merged["log_keep_per_task"] = 10

    return merged


def save_config(cfg):
    base = load_config()
    base.update(cfg or {})
    write_json(CONFIG_FILE, base)


def _fls_minutes_to_cron(minutes):
    try:
        minutes = max(1, min(1440, int(minutes)))
    except Exception:
        minutes = 30

    if minutes == 1440:
        return "0 0 * * *"

    if minutes >= 60 and minutes % 60 == 0:
        h = minutes // 60
        return f"0 */{h} * * *"

    if minutes <= 59:
        return f"*/{minutes} * * * *"

    # Cron 不能完美表达任意超过 60 且非整小时的分钟间隔，这里显示为说明文本。
    return f"interval:{minutes}min"
def tq():
    return ""


def url_with_token(path):
    return path


def redirect_to(endpoint, **kwargs):
    return redirect(url_for(endpoint, **kwargs))


app.secret_key = os.environ.get("FLS_SECRET_KEY") or str(uuid.uuid4())


@app.route("/login", methods=["GET", "POST"])
def _fls_login():
    token = fls_get_admin_token()

    if not token:
        return redirect(url_for("dashboard"))

    msg = ""

    if request.method == "POST":
        input_token = request.form.get("token", "").strip()
        if input_token == token:
            next_url = request.args.get("next") or url_for("dashboard")
            resp = make_response(redirect(next_url))
            resp.set_cookie("token", token, max_age=86400 * 365, httponly=True, samesite="Lax")
            session["token"] = token
            return resp
        msg = "Token 错误"

    body = f"""
<div class="card" style="max-width:520px;margin:8vh auto;">
    <div class="card-title">登录 FLS 面板</div>
    <form method="post">
        <div class="form-item">
            <label>Token</label>
            <input name="token" type="password" placeholder="请输入登录 Token" autofocus>
        </div>
        <br>
        <button class="btn btn-primary" type="submit">登录</button>
    </form>
    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
</div>
"""
    return layout("登录", "login", body)


@app.route("/logout")
def _fls_logout():
    session.pop("token", None)
    resp = make_response(redirect(url_for("_fls_login")))
    resp.delete_cookie("token")
    return resp


# 扩展导航：配置页 / 面板状态 / 退出登录
_fls_old_layout = _fls_base_layout


def layout(title, active, body):
    html_text = _fls_old_layout(title, active, body)

    extra = f"""
            <a class="{'active' if active == 'status' else ''}" href="/panel/status">🖥️ 环境状态</a>
            <a class="{'active' if active == 'config' else ''}" href="/config">🔧 配置</a>
            <a href="/logout">🚪 退出登录</a>
"""
    html_text = html_text.replace(
        '<a class="{\'active\' if active == \'about\' else \'\'}" href="/about{token}">⚙️ 关于</a>',
        '<a class="{\'active\' if active == \'about\' else \'\'}" href="/about{token}">⚙️ 关于</a>' + extra
    )

    # 如果上面的原始字符串因运行时已格式化而未命中，则匹配已渲染后的关于入口。
    html_text = html_text.replace(
        '<a class="active" href="/about">⚙️ 关于</a>',
        '<a class="active" href="/about">⚙️ 关于</a>' + extra
    ).replace(
        '<a class="" href="/about">⚙️ 关于</a>',
        '<a class="" href="/about">⚙️ 关于</a>' + extra
    )

    return html_text


# 日志配置读取
def _fls_log_keep():
    return int(load_config().get("log_keep_per_task", LOG_KEEP_PER_TASK))


def _fls_log_max_bytes():
    return int(load_config().get("log_max_size_mb", 10)) * 1024 * 1024


# 覆盖日志清理
def cleanup_logs_for_task(task_name, keep=None):
    try:
        keep = int(keep or _fls_log_keep())
        max_bytes = _fls_log_max_bytes()
        task_name = str(task_name or "").strip()
        if not task_name:
            return

        files = []
        for f in LOG_DIR.glob("*.log"):
            if not f.is_file():
                continue
            parsed_name = parse_task_name_from_log(f)
            if parsed_name == task_name:
                files.append(f)

        for f in list(files):
            try:
                if f.exists() and f.stat().st_size > max_bytes:
                    f.unlink()
                    files.remove(f)
                    print(f"[LogCleanup] 删除超大任务日志: {f}")
            except Exception as e:
                print(f"[LogCleanup] 删除超大任务日志失败 {f}: {e}")

        files = [f for f in files if f.exists()]
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        for old in files[keep:]:
            try:
                old.unlink()
                print(f"[LogCleanup] 删除旧任务日志: {old}")
            except Exception as e:
                print(f"[LogCleanup] 删除旧任务日志失败 {old}: {e}")

    except Exception as e:
        print(f"[LogCleanup] 清理任务日志失败: {e}")


def cleanup_logs():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        keep = _fls_log_keep()
        max_bytes = _fls_log_max_bytes()

        files = [f for f in LOG_DIR.glob("*.log") if f.is_file()]

        for f in list(files):
            try:
                if f.exists() and f.stat().st_size > max_bytes:
                    f.unlink()
                    print(f"[LogCleanup] 删除超大日志: {f}")
            except Exception as e:
                print(f"[LogCleanup] 删除超大日志失败 {f}: {e}")

        files = [f for f in LOG_DIR.glob("*.log") if f.is_file()]
        groups = {}

        for f in files:
            try:
                if f.name.startswith("deps-install-"):
                    key = "依赖安装"
                else:
                    key = parse_task_name_from_log(f)
                if not key:
                    continue
                groups.setdefault(key, []).append(f)
            except Exception:
                continue

        for group_name, group_files in groups.items():
            group_files = [f for f in group_files if f.exists()]
            group_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

            for old in group_files[keep:]:
                try:
                    old.unlink()
                    print(f"[LogCleanup] 删除旧日志[{group_name}]: {old}")
                except Exception as e:
                    print(f"[LogCleanup] 删除旧日志失败 {old}: {e}")

    except Exception as e:
        print(f"[LogCleanup] 全局日志清理失败: {e}")


def reload_scheduler():
    print(f"[Scheduler] 开始重载，pid={os.getpid()} time={now_str()}")

    try:
        scheduler.remove_all_jobs()
        print("[Scheduler] 已清空所有任务")
    except Exception as e:
        print(f"[Scheduler] 清空任务失败: {e}")

    for task in load_tasks():
        task_id = task.get("id")
        enabled = task.get("enabled", True)
        cron_expr = str(task.get("cron", "")).strip()

        if not enabled or not cron_expr:
            continue

        try:
            trigger = cron_to_trigger(cron_expr)
            scheduler.add_job(
                scheduler_run,
                trigger=trigger,
                args=[task_id],
                id=f"task_{task_id}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            print(f"[Scheduler] 已加载任务: {task.get('name') or task_id} cron={cron_expr}")
        except Exception as e:
            print(f"[Scheduler] 任务 {task.get('name') or task_id} cron 加载失败: {e}")

    try:
        minutes = int(load_config().get("log_cleanup_minutes", 30))
        minutes = max(1, min(1440, minutes))
        scheduler.add_job(
            cleanup_logs,
            trigger="interval",
            minutes=minutes,
            id="log_cleanup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        print(f"[Scheduler] 已加载日志清理任务: every {minutes} minutes")
    except Exception as e:
        print(f"[Scheduler] 日志清理任务加载失败: {e}")

    try:
        jobs = scheduler.get_jobs()
        if not jobs:
            print("[Scheduler] 当前无已注册任务")
        else:
            print("[Scheduler] 当前 jobs:")
            for job in jobs:
                print(f"[Scheduler] job={job.id}, next_run={job.next_run_time}, trigger={job.trigger}")
    except Exception as e:
        print(f"[Scheduler] 读取 jobs 失败: {e}")


def _fls_type_enabled(suffix):
    suffix = str(suffix or "").lower().lstrip(".")
    cfg = load_config()
    return bool(cfg.get("task_types", {}).get(suffix, False))


@app.route("/config", methods=["GET", "POST"])
def _fls_config_page():
    if request.method == "POST":
        task_types = {}
        for k in ["py", "sh", "js", "ts", "ps1", "bat", "php", "rb", "pl", "lua", "jar"]:
            task_types[k] = request.form.get(f"type_{k}") == "1"

        # 平台和命令不可用时强制修正。
        if os.name != "nt":
            task_types["bat"] = False
            if not (_fls_cmd_exists("pwsh") or _fls_cmd_exists("powershell")):
                task_types["ps1"] = False

        if not _fls_cmd_exists(BASH_BIN):
            task_types["sh"] = False

        if not _fls_cmd_exists(NODE_BIN):
            task_types["js"] = False
            if not (_fls_cmd_exists("tsx") or _fls_cmd_exists("ts-node")):
                task_types["ts"] = False

        for _suffix, _runner in [
            ("php", "php"),
            ("rb", "ruby"),
            ("pl", "perl"),
            ("lua", "lua"),
            ("jar", "java"),
        ]:
            if not _fls_cmd_exists(_runner):
                task_types[_suffix] = False

        cfg = {
            "admin_token": request.form.get("admin_token", "").strip(),
            "port": max(1, min(65535, int(request.form.get("port", str(PORT)) or PORT))),
            "log_cleanup_minutes": max(1, min(1440, int(request.form.get("log_cleanup_minutes", "30") or 30))),
            "log_max_size_mb": max(1, int(request.form.get("log_max_size_mb", "10") or 10)),
            "log_keep_per_task": max(1, int(request.form.get("log_keep_per_task", "10") or 10)),
            "task_types": task_types,
        }
        save_config(cfg)
        cleanup_logs()
        reload_scheduler()
        return redirect(url_for("_fls_config_page"))

    cfg = load_config()
    types = cfg.get("task_types", {})
    cron_text = _fls_minutes_to_cron(cfg.get("log_cleanup_minutes", 30))

    def checked(k):
        return "checked" if types.get(k) else ""

    def disabled_note(k):
        notes = []
        if k == "sh" and not _fls_cmd_exists(BASH_BIN):
            notes.append("bash 不可用，保存时会强制禁用")
        if k in ("js",) and not _fls_cmd_exists(NODE_BIN):
            notes.append("node 不可用，保存时会强制禁用")
        if k == "ts" and not (_fls_cmd_exists("tsx") or _fls_cmd_exists("ts-node")):
            notes.append("tsx / ts-node 不可用")
        if k == "ps1" and not (_fls_cmd_exists("pwsh") or _fls_cmd_exists("powershell")):
            notes.append("PowerShell 不可用")
        if k == "bat" and os.name != "nt":
            notes.append("非 Windows 强制禁用")
        if k == "php" and not _fls_cmd_exists("php"):
            notes.append("php 不可用")
        if k == "rb" and not _fls_cmd_exists("ruby"):
            notes.append("ruby 不可用")
        if k == "pl" and not _fls_cmd_exists("perl"):
            notes.append("perl 不可用")
        if k == "lua" and not _fls_cmd_exists("lua"):
            notes.append("lua 不可用")
        if k == "jar" and not _fls_cmd_exists("java"):
            notes.append("java 不可用")
        return "；".join(notes)

    rows = ""
    for k, name in [
        ("py", "Python .py"),
        ("sh", "Shell .sh"),
        ("js", "Node .js"),
        ("ts", "TypeScript .ts"),
        ("ps1", "PowerShell .ps1"),
        ("bat", "Windows Batch .bat"),
        ("php", "PHP .php"),
        ("rb", "Ruby .rb"),
        ("pl", "Perl .pl"),
        ("lua", "Lua .lua"),
        ("jar", "Java Jar .jar"),
    ]:
        rows += f"""
<tr>
    <td><b>{h(name)}</b></td>
    <td><input type="checkbox" name="type_{h(k)}" value="1" {checked(k)} style="width:auto;"></td>
    <td>{h(disabled_note(k) or "可配置")}</td>
</tr>
"""

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">登录配置</div>
    <div class="form-item">
        <label>登录 Token，留空表示不启用登录</label>
        <input name="admin_token" value="{h(cfg.get('admin_token', ''))}" placeholder="建议设置一个较长随机 Token">
    </div>
    <div class="help">
        如果访问链接带有 ?token=xxx，Token 正确时会自动登录并写入 Cookie。
    </div>
    <br>
    <div class="form-item">
        <label>面板端口，保存后重启生效</label>
        <input name="port" type="number" min="1" max="65535" value="{h(cfg.get('port', PORT))}" placeholder="默认 5700">
        <div class="help">
            当前进程实际监听端口：{h(PORT)}。如果启动时设置了环境变量 FLS_PORT，会优先使用环境变量端口。
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">日志清理配置</div>
    <div class="form-grid">
        <div class="form-item">
            <label>定时清理间隔，分钟，1 - 1440</label>
            <input name="log_cleanup_minutes" type="number" min="1" max="1440" value="{h(cfg.get('log_cleanup_minutes', 30))}">
            <div class="help">当前换算：{h(cron_text)}。1440 表示每天清理一次。</div>
        </div>
        <div class="form-item">
            <label>单个日志最大大小，MB</label>
            <input name="log_max_size_mb" type="number" min="1" value="{h(cfg.get('log_max_size_mb', 10))}">
        </div>
        <div class="form-item">
            <label>每个任务保留日志数量</label>
            <input name="log_keep_per_task" type="number" min="1" value="{h(cfg.get('log_keep_per_task', 10))}">
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">task 可执行脚本类型</div>
    <div class="help">
        task 运行时会检测脚本类型是否启用。bash / node / PowerShell 不存在时会强制禁用对应类型。
    </div>
    <br>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>类型</th>
                    <th>启用</th>
                    <th>说明</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存配置</button>
</div>
</form>
"""
    return layout("配置", "config", body)


def _fls_find_backup_root(root):
    root = Path(root)
    candidates = []

    for p in [root] + [x for x in root.rglob("*") if x.is_dir()]:
        if (p / "data").exists() or (p / "scripts").exists():
            candidates.append(p)

    if not candidates:
        return None

    candidates.sort(key=lambda x: len(str(x)))
    return candidates[0]


def _fls_extract_archive(file_path, dest_dir):
    file_path = Path(file_path)
    name = file_path.name.lower()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if name.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if name.endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(file_path, mode) as tar:
            safe_extract_tar(tar, dest_dir)
        return True

    if name.endswith(".zip"):
        with zipfile.ZipFile(file_path, "r") as z:
            safe_extract_zip(z, dest_dir)
        return True

    if name.endswith(".gz") and not name.endswith(".tar.gz"):
        out_name = file_path.stem
        out_file = dest_dir / out_name
        with gzip.open(file_path, "rb") as src, open(out_file, "wb") as dst:
            shutil.copyfileobj(src, dst)
        # 如果解出来的是 tar，再继续解。
        try:
            if tarfile.is_tarfile(out_file):
                sub = dest_dir / (out_name + "_extract")
                sub.mkdir(parents=True, exist_ok=True)
                with tarfile.open(out_file, "r:*") as tar:
                    safe_extract_tar(tar, sub)
        except Exception:
            pass
        return True

    if name.endswith((".7z", ".rar")):
        tool = shutil.which("7z") or shutil.which("7za") or shutil.which("unar") or shutil.which("bsdtar")
        if not tool:
            raise RuntimeError("系统未安装 7z / unar / bsdtar，无法解压 7z/rar")
        base = os.path.basename(tool).lower()
        if base in ("7z", "7za"):
            subprocess.check_call([tool, "x", "-y", f"-o{dest_dir}", str(file_path)])
        elif base == "unar":
            subprocess.check_call([tool, "-o", str(dest_dir), str(file_path)])
        else:
            subprocess.check_call([tool, "-xf", str(file_path), "-C", str(dest_dir)])
        return True

    raise RuntimeError("不支持的备份格式")


def _fls_backup_find_dependency_file(extract_dir, backup_root=None):
    """
    查找备份中的依赖列表。
    支持：
    - dependencies.txt
    - requirements.txt
    - data/dependencies.txt
    - data/requirements.txt
    """
    candidates = []

    extract_dir = Path(extract_dir)

    if backup_root:
        backup_root = Path(backup_root)
        candidates.extend([
            backup_root / "dependencies.txt",
            backup_root / "requirements.txt",
            backup_root / "data" / "dependencies.txt",
            backup_root / "data" / "requirements.txt",
        ])

    candidates.extend([
        extract_dir / "dependencies.txt",
        extract_dir / "requirements.txt",
        extract_dir / "data" / "dependencies.txt",
        extract_dir / "data" / "requirements.txt",
    ])

    for item in candidates:
        try:
            if item.exists() and item.is_file() and item.stat().st_size > 0:
                return item
        except Exception:
            pass

    try:
        for item in extract_dir.rglob("*"):
            if item.is_file() and item.name in ("dependencies.txt", "requirements.txt"):
                if item.stat().st_size > 0:
                    return item
    except Exception:
        pass

    return None


def _fls_backup_install_dependencies(dep_file):
    """
    从备份依赖列表恢复依赖。
    安装日志写入 log/backup-restore-deps-*.log。
    """
    dep_file = Path(dep_file)

    if not dep_file.exists():
        return False, "未找到依赖列表文件", ""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "backup-restore-deps-{}.log".format(datetime.now().strftime("%Y%m%d-%H%M%S"))

    with open(log_file, "ab", buffering=0) as log_fp:
        header = (
            "===== 备份恢复依赖 =====\n"
            "时间: {}\n"
            "Python: {}\n"
            "依赖列表: {}\n"
            "命令: {} -m pip install -r {}\n"
            "============================================================\n"
        ).format(now_str(), sys.executable, dep_file, sys.executable, dep_file)
        log_fp.write(header.encode("utf-8"))

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(dep_file)],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR),
                env=os.environ.copy(),
                timeout=1800
            )

            footer = "\n===== 依赖恢复结束：{}，退出码：{} =====\n".format(now_str(), proc.returncode)
            log_fp.write(footer.encode("utf-8"))

            if proc.returncode == 0:
                return True, "依赖恢复成功", str(log_file)

            return False, "依赖恢复失败，退出码：{}".format(proc.returncode), str(log_file)

        except Exception as e:
            log_fp.write(("\n依赖恢复异常: {}\n".format(e)).encode("utf-8"))
            return False, "依赖恢复异常：{}".format(e), str(log_file)


def _fls_patched_backup_page():
    body = """
<div class="card">
    <div class="card-title">导出面板数据</div>
    <div class="help">
        会导出 data、scripts 目录中的任务配置、代理配置、全局变量和脚本。<br>
        同时会自动导出当前 Python 依赖列表 dependencies.txt，恢复时可选择是否安装这些依赖。
    </div>
    <br>
    <a class="btn btn-primary" href="/backup/export">导出备份</a>
</div>

<div class="card">
    <div class="card-title">导入面板数据</div>
    <form method="post" action="/backup/import" enctype="multipart/form-data">
        <input type="file" name="file" accept=".tar.gz,.tgz,.gz,.zip,.rar,.7z,.tar,application/gzip,application/zip,application/x-7z-compressed,application/vnd.rar,application/octet-stream">
        <br><br>
        <label>
            <input type="checkbox" name="restore_deps" value="1" style="width:auto;">
            如果备份中包含依赖列表，同时恢复 Python 依赖
        </label>
        <div class="help">
            依赖恢复会执行：<code>python -m pip install -r dependencies.txt</code><br>
            安装日志会写入 log/backup-restore-deps-*.log。<br>
            如果备份中没有 dependencies.txt / requirements.txt，则会自动跳过。
        </div>
        <br>
        <button class="btn btn-orange" type="submit" onclick="return confirm('导入会覆盖当前 data 和 scripts，确定继续吗？')">导入备份</button>
    </form>
    <div class="help">
        支持 .tar.gz / .tgz / .gz / .zip / .rar / .7z / .tar。<br>
        rar / 7z 需要系统存在 7z / unar / bsdtar。
    </div>
</div>
"""
    return layout("备份恢复", "backup", body)

def _fls_patched_backup_import():
    f = request.files.get("file")
    if not f:
        return "未上传文件", 400

    restore_deps = request.form.get("restore_deps") == "1"
    tmp_dir = tempfile.mkdtemp()

    try:
        original = os.path.basename(f.filename or "backup")
        backup_file = Path(tmp_dir) / original
        f.save(str(backup_file))

        extract_dir = Path(tmp_dir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        _fls_extract_archive(backup_file, extract_dir)

        # 如果是二次压缩，继续尝试解压内部常见压缩包。
        for inner in list(extract_dir.rglob("*")):
            if inner.is_file() and inner.name.lower().endswith((".tar.gz", ".tgz", ".gz", ".zip", ".rar", ".7z", ".tar")):
                try:
                    sub = inner.parent / (inner.stem + "_extract")
                    sub.mkdir(parents=True, exist_ok=True)
                    _fls_extract_archive(inner, sub)
                except Exception:
                    pass

        root = _fls_find_backup_root(extract_dir)
        if not root:
            raise RuntimeError("未在备份中找到 data 或 scripts 目录")

        dep_file = _fls_backup_find_dependency_file(extract_dir, root)

        data_src = root / "data"
        scripts_src = root / "scripts"

        if data_src.exists():
            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR)
            shutil.copytree(data_src, DATA_DIR)

        if scripts_src.exists():
            if SCRIPT_DIR.exists():
                shutil.rmtree(SCRIPT_DIR)
            shutil.copytree(scripts_src, SCRIPT_DIR)

        deps_ok = None
        deps_msg = ""
        deps_log = ""

        if restore_deps:
            if dep_file:
                deps_ok, deps_msg, deps_log = _fls_backup_install_dependencies(dep_file)
                if not deps_ok:
                    _fls_system_failure_notify(
                        "备份恢复-依赖恢复",
                        deps_msg or "依赖恢复失败",
                        deps_log,
                        tail_file(deps_log, 2000) if deps_log else "",
                    )
            else:
                deps_ok = False
                deps_msg = "已勾选恢复依赖，但备份中没有 dependencies.txt / requirements.txt"
                _fls_system_failure_notify(
                    "备份恢复-依赖恢复",
                    deps_msg,
                    "",
                    "",
                )

        reload_scheduler()

        if restore_deps:
            # 勾选恢复依赖时，如果产生了依赖恢复日志，导入完成后自动进入该日志。
            if deps_log:
                return redirect(url_for("logfile_view", filename=os.path.basename(deps_log)))

            status_badge = "成功" if deps_ok else "提示"
            body = f"""
<div class="card">
    <div class="card-title">备份导入完成</div>
    <div class="help">
        data / scripts 已恢复。<br>
        依赖恢复状态：<b>{h(status_badge)}</b><br>
        说明：{h(deps_msg or "未执行依赖恢复")}<br>
        日志：{h(deps_log or "-")}
    </div>
    <br>
    <a class="btn btn-primary" href="/backup">返回备份恢复</a>
    <a class="btn btn-gray" href="/logs">查看日志</a>
</div>
"""
            return layout("备份导入完成", "backup", body)

        return redirect(url_for("backup_page"))

    except Exception as e:
        _fls_system_failure_notify(
            "备份恢复",
            str(e),
            "",
            f"上传文件：{getattr(f, 'filename', '')}",
        )
        return f"备份导入失败：{h(e)}", 400

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


app.view_functions["backup_page"] = _fls_patched_backup_page
app.view_functions["backup_import"] = _fls_patched_backup_import
def _fls_install_log_file(install_id, name):
    safe_pkg = safe_name(name or "install")
    return LOG_DIR / f"system-install-{safe_pkg}-{install_id}.log"


@app.route("/install/node")
def _fls_install_node():
    if os.name == "nt":
        return redirect("https://nodejs.org/zh-cn/download")

    install_id = uuid.uuid4().hex
    log_file = _fls_install_log_file(install_id, "node")
    log_fp = open(log_file, "ab", buffering=0)

    pm = ""
    if shutil.which("pkg"):
        cmd = ["sh", "-lc", "pkg update -y && pkg install -y nodejs"]
        pm = "pkg"
    elif shutil.which("apt"):
        cmd = ["sh", "-lc", "apt update && apt install -y nodejs npm"]
        pm = "apt"
    elif shutil.which("apt-get"):
        cmd = ["sh", "-lc", "apt-get update && apt-get install -y nodejs npm"]
        pm = "apt-get"
    elif shutil.which("dnf"):
        cmd = ["sh", "-lc", "dnf install -y nodejs npm"]
        pm = "dnf"
    elif shutil.which("yum"):
        cmd = ["sh", "-lc", "yum install -y nodejs npm"]
        pm = "yum"
    elif shutil.which("apk"):
        cmd = ["sh", "-lc", "apk add --no-cache nodejs npm"]
        pm = "apk"
    elif shutil.which("pacman"):
        cmd = ["sh", "-lc", "pacman -Sy --noconfirm nodejs npm"]
        pm = "pacman"
    else:
        log_fp.write("无法识别包管理器，请手动安装 Node.js\n".encode("utf-8"))
        log_fp.close()
        return redirect(url_for("_fls_system_install_log", install_id=install_id))

    header = (
        f"===== 安装 Node.js =====\n"
        f"时间: {now_str()}\n"
        f"包管理器: {pm}\n"
        f"命令: {' '.join(cmd)}\n"
        f"日志文件: {log_file}\n"
        f"============================================================\n"
    )
    log_fp.write(header.encode("utf-8"))

    try:
        proc = subprocess.Popen(cmd, stdout=log_fp, stderr=subprocess.STDOUT, cwd=str(BASE_DIR), env=os.environ.copy())
        DEPS_RUNNING[install_id] = {
            "process": proc,
            "package": "Node.js",
            "log_file": str(log_file),
            "log_fp": log_fp,
            "start_time": time.time(),
        }
    except Exception as e:
        log_fp.write(f"启动安装失败: {e}\n".encode("utf-8"))
        log_fp.close()

    return redirect(url_for("_fls_system_install_log", install_id=install_id))


@app.route("/install/log/<install_id>")
def _fls_system_install_log(install_id):
    return deps_install_log(install_id)


def _fls_format_bytes(n):
    try:
        n = float(n)
    except Exception:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}"


def _fls_mem_info():
    info = {}
    try:
        if Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    num = re.findall(r"\d+", v)
                    if num:
                        info[k] = int(num[0]) * 1024
    except Exception:
        pass
    return info


@app.route("/panel/status")
def _fls_panel_status():
    mem = _fls_mem_info()
    rss = 0
    try:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            rss *= 1024
    except Exception:
        pass

    disk = shutil.disk_usage(str(BASE_DIR))

    rows = ""
    data = [
        ("系统", platform.platform()),
        ("Python", sys.version.split()[0]),
        ("工作目录", str(BASE_DIR)),
        ("数据目录", str(DATA_DIR)),
        ("日志目录", str(LOG_DIR)),
        ("脚本目录", str(SCRIPT_DIR)),
        ("主进程 PID", os.getpid()),
        ("主进程名", MAIN_PROCESS_NAME),
        ("Host / Port", f"{HOST}:{PORT}"),
        ("鉴权", "已开启" if fls_get_admin_token() else "未开启"),
        ("磁盘总量", _fls_format_bytes(disk.total)),
        ("磁盘已用", _fls_format_bytes(disk.used)),
        ("磁盘可用", _fls_format_bytes(disk.free)),
        ("RAM 总量", _fls_format_bytes(mem.get("MemTotal", 0)) if mem else "-"),
        ("RAM 可用", _fls_format_bytes(mem.get("MemAvailable", 0)) if mem else "-"),
        ("面板进程 RAM 峰值", _fls_format_bytes(rss) if rss else "-"),
    ]

    for k, v in data:
        rows += f"<tr><td><b>{h(k)}</b></td><td>{h(v)}</td></tr>"

    runtime_rows = ""

    for runtime in _fls_runtime_status_items():
        version = runtime.get("version") or "未安装"
        if runtime.get("installed"):
            action = '<span class="badge green">已安装</span>'
        else:
            action = f'<a class="btn btn-primary" href="{h(runtime.get("install_url"))}">安装</a>'

        runtime_rows += f"""
<tr>
    <td><b>{h(runtime.get("name"))}</b></td>
    <td>{h(runtime.get("suffix"))}</td>
    <td>{h(runtime.get("command"))}</td>
    <td>{h(version)}</td>
    <td>{action}</td>
</tr>
"""

    body = f"""
<div class="card">
    <div class="card-title">环境状态</div>
    <div class="table-wrap">
        <table>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">脚本运行环境</div>
    <div class="help">
        这里显示 task 支持的脚本运行器版本。<br>
        Linux / Termux 点击安装会调用系统包管理器安装；Windows 点击安装会跳转到对应官方下载页面手动安装。<br>
        TypeScript 需要额外安装 <code>tsx</code> 或 <code>ts-node</code>。
    </div>
    <br>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>环境</th>
                    <th>脚本类型</th>
                    <th>检测命令</th>
                    <th>版本</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{runtime_rows}</tbody>
        </table>
    </div>
</div>
"""
    return layout("环境状态", "status", body)
pass

# 初始化配置文件，避免首次启动配置缺失。
try:
    save_config(load_config())
except Exception as e:
    print(f"[Config] 初始化配置失败: {e}")

from datetime import timedelta as _fls_timedelta
from urllib.parse import quote as _fls_url_quote

FLS_AUTH_SHORT_SECONDS = 3600
FLS_AUTH_REMEMBER_SECONDS = 7 * 24 * 3600


def fls_get_admin_token():
    """
    获取当前登录 Token。

    优先级：
    1. 启动环境变量 FLS_TOKEN；
    2. data/config.json 中的 admin_token。
    """
    env_token = os.environ.get("FLS_TOKEN", "").strip()
    if env_token:
        return env_token

    try:
        return str(load_config().get("admin_token", "") or "").strip()
    except Exception:
        return ""


def _fls_is_api_request():
    path = request.path or ""
    if path.startswith("/api/"):
        return True

    accept = request.headers.get("Accept", "")
    xhr = request.headers.get("X-Requested-With", "")
    return "application/json" in accept or xhr in ("XMLHttpRequest", "FLS-Ajax")


def _fls_auth_redirect_login():
    next_url = request.full_path if request.query_string else request.path
    return redirect(url_for("_fls_login") + "?next=" + _fls_url_quote(next_url))


def _fls_auth_redirect_setup():
    return redirect(url_for("_fls_setup_token"))


def _fls_auth_set_session(token, remember=False):
    now_ts = int(time.time())
    max_age = FLS_AUTH_REMEMBER_SECONDS if remember else FLS_AUTH_SHORT_SECONDS

    session.clear()
    session["token"] = token
    session["token_expire_at"] = now_ts + max_age
    session["remember_login"] = bool(remember)

    session.permanent = bool(remember)
    if remember:
        app.permanent_session_lifetime = _fls_timedelta(seconds=FLS_AUTH_REMEMBER_SECONDS)
    else:
        app.permanent_session_lifetime = _fls_timedelta(seconds=FLS_AUTH_SHORT_SECONDS)


def _fls_auth_clear_session():
    session.clear()


def _fls_auth_session_valid(token):
    try:
        saved = session.get("token")
        exp = int(session.get("token_expire_at", 0) or 0)

        if saved != token:
            return False

        if exp <= int(time.time()):
            _fls_auth_clear_session()
            return False

        return True
    except Exception:
        _fls_auth_clear_session()
        return False


def check_auth():
    """
    全局认证检查。

    注意：
    旧代码中的 before_request 只调用 check_auth() 但没有 return，
    Flask 因此不会中断请求，导致页面/API 可绕过登录。
    本补丁会替换 before_request_funcs，确保 return 生效。
    """
    endpoint = request.endpoint or ""
    path = request.path or ""

    # 登录 / 退出永远放行
    if endpoint in ("_fls_login", "_fls_logout"):
        return None

    token = fls_get_admin_token()

    # 没设置 Token：只允许进入 /setup 设置 Token
    if not token:
        if endpoint == "_fls_setup_token" or path == "/setup":
            return None

        if _fls_is_api_request():
            return jsonify({
                "ok": False,
                "msg": "面板尚未设置登录 Token，请先访问 /setup 设置"
            }), 403

        return _fls_auth_redirect_setup()

    # 已设置 Token 后，不允许未登录访问 setup
    if endpoint == "_fls_setup_token" or path == "/setup":
        if _fls_auth_session_valid(token):
            return redirect(url_for("_fls_config_page") if "_fls_config_page" in app.view_functions else url_for("dashboard"))
        return _fls_auth_redirect_login()

    # API Header Token
    header_token = request.headers.get("X-Token", "")
    if header_token and header_token == token:
        return None

    # URL Token：兼容旧访问方式，验证成功后写入 1 小时会话并跳转到无 token URL
    arg_token = request.args.get("token", "")
    if arg_token:
        if arg_token == token:
            _fls_auth_set_session(token, remember=False)

            clean_args = dict(request.args)
            clean_args.pop("token", None)

            clean_url = request.path
            if clean_args:
                from urllib.parse import urlencode
                clean_url += "?" + urlencode(clean_args, doseq=True)

            return redirect(clean_url)

        if _fls_is_api_request():
            return jsonify({
                "ok": False,
                "msg": "Token 错误"
            }), 403

        return _fls_auth_redirect_login()

    # Session 登录态
    if _fls_auth_session_valid(token):
        return None

    if _fls_is_api_request():
        return jsonify({
            "ok": False,
            "msg": "登录已过期，请重新登录"
        }), 401

    return _fls_auth_redirect_login()


def _fls_auth_before_request():
    return check_auth()
try:
    app.before_request_funcs[None] = [_fls_auth_before_request]
except Exception as e:
    print(f"[AuthPatch] 替换 before_request 失败: {e}")


def _fls_setup_token():
    """
    首次设置 Token 页面。
    只有当前没有 Token 时可以访问。
    """
    if fls_get_admin_token():
        return redirect(url_for("_fls_login"))

    msg = ""

    if request.method == "POST":
        new_token = request.form.get("token", "").strip()
        confirm_token = request.form.get("confirm_token", "").strip()

        if not new_token:
            msg = "Token 不能为空"
        elif len(new_token) < 6:
            msg = "Token 建议至少 6 位"
        elif new_token != confirm_token:
            msg = "两次输入的 Token 不一致"
        else:
            try:
                save_config({
                    "admin_token": new_token
                })
                _fls_auth_clear_session()
                return redirect(url_for("_fls_login"))
            except Exception as e:
                msg = f"保存失败：{e}"

    body = f"""
<div class="card" style="max-width:620px;margin:8vh auto;">
    <div class="card-title">首次设置登录 Token</div>
    <div class="help">
        当前面板尚未设置登录 Token。为了避免页面和 API 被未授权访问，请先设置登录 Token。
    </div>
    <br>
    <form method="post">
        <div class="form-item">
            <label>登录 Token</label>
            <input name="token" type="password" placeholder="请输入新的登录 Token" autofocus>
        </div>
        <br>
        <div class="form-item">
            <label>确认 Token</label>
            <input name="confirm_token" type="password" placeholder="请再次输入登录 Token">
        </div>
        <br>
        <button class="btn btn-primary" type="submit">保存 Token</button>
    </form>
    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
    <div class="help">
        也可以在启动时设置环境变量：<br>
        <code>FLS_TOKEN=你的Token sh fls.sh start</code>
    </div>
</div>
"""
    return layout("首次设置 Token", "config", body)


# 注册 / 覆盖认证相关路由
try:
    if "_fls_setup_token" not in app.view_functions:
        app.add_url_rule("/setup", endpoint="_fls_setup_token", view_func=_fls_setup_token, methods=["GET", "POST"])
    else:
        pass

    pass
    pass
except Exception as e:
    print(f"[AuthPatch] 注册登录路由失败: {e}")


def _fls_about_with_author():
    body = """
<div class="card">
    <div class="card-title">关于 FLS 面板</div>
    <div class="help">
        <p><b>FLS 面板</b> 是一个轻量级脚本任务管理面板，可用于管理 Python、Shell、Node.js 等脚本任务。</p>
        <p>支持任务管理、Cron 定时、脚本导入/拉取、日志查看、依赖管理、代理配置、备份恢复和面板配置。</p>
        <p>作者：<b>余生只有凄渺</b></p>
        <p>QQ群：<b>923184177</b></p>
        <p>项目仓库：<a href="https://github.com/liyw0205/fls" target="_blank">https://github.com/liyw0205/fls</a></p>
    </div>
</div>

<div class="card">
    <div class="card-title">说明</div>
    <div class="help">
        <p>环境、运行器、目录、依赖状态等信息请查看：<a href="/panel/status">环境状态</a></p>
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令规则</div>
    <div class="code">
task 1.py<br>
task a/test.sh<br>
task demo.js<br>
task demo.ts<br>
task script.ps1<br>
task run.bat<br>
task demo.php<br>
task demo.rb<br>
task demo.pl<br>
task demo.lua<br>
task app.jar<br><br>
不加 task 则作为系统命令执行。
    </div>
</div>
"""
    return layout("关于", "about", body)


try:
    app.view_functions["about"] = _fls_about_with_author
except Exception as e:
    print(f"[AuthPatch] 覆盖关于页面失败: {e}")


# 初始化配置文件。如果启动时提供了 FLS_TOKEN，不强制写入配置文件，只作为运行时 Token。
try:
    cfg = load_config()
    if os.environ.get("FLS_TOKEN", "").strip() and not cfg.get("admin_token"):
        print("[AuthPatch] 检测到启动环境变量 FLS_TOKEN，将作为当前运行 Token 使用")
    save_config(cfg)
except Exception as e:
    print(f"[AuthPatch] 初始化认证配置失败: {e}")

import smtplib as _fls_smtplib
from email.mime.text import MIMEText as _fls_MIMEText
from email.header import Header as _fls_Header
from email.utils import formataddr as _fls_formataddr


FLS_NOTIFY_CHANNELS = {
    "console": {
        "name": "控制台日志",
        "fields": []
    },
    "bark": {
        "name": "Bark",
        "fields": [
            ("BARK_PUSH", "Bark 地址或设备码", "例如：https://api.day.app/xxxx 或 xxxx"),
            ("BARK_ARCHIVE", "是否存档，可空", "1 / true"),
            ("BARK_GROUP", "分组，可空", ""),
            ("BARK_SOUND", "声音，可空", ""),
            ("BARK_ICON", "图标 URL，可空", ""),
            ("BARK_LEVEL", "时效性，可空", "active / timeSensitive / passive"),
            ("BARK_URL", "点击跳转 URL，可空", "")
        ]
    },
    "serverj": {
        "name": "Server 酱",
        "fields": [
            ("PUSH_KEY", "PUSH_KEY", "Server 酱 SendKey，兼容旧版与 Turbo 版")
        ]
    },
    "pushplus": {
        "name": "PushPlus",
        "fields": [
            ("PUSH_PLUS_TOKEN", "Token", ""),
            ("PUSH_PLUS_USER", "群组编码，可空", ""),
            ("PUSH_PLUS_TEMPLATE", "模板", "html / markdown / txt，默认 html"),
            ("PUSH_PLUS_CHANNEL", "渠道", "wechat / webhook / cp / mail / sms，默认 wechat"),
            ("PUSH_PLUS_WEBHOOK", "Webhook 编码，可空", ""),
            ("PUSH_PLUS_CALLBACKURL", "回调地址，可空", ""),
            ("PUSH_PLUS_TO", "好友令牌/企业微信用户，可空", "")
        ]
    },
    "telegram": {
        "name": "Telegram Bot",
        "fields": [
            ("TG_BOT_TOKEN", "Bot Token", ""),
            ("TG_USER_ID", "User ID / Chat ID", ""),
            ("TG_API_HOST", "API Host，可空", "例如：https://api.telegram.org"),
            ("TG_PROXY_HOST", "代理 Host，可空", ""),
            ("TG_PROXY_PORT", "代理 Port，可空", ""),
            ("TG_PROXY_AUTH", "代理认证，可空", "user:pass")
        ]
    },
    "qywxbot": {
        "name": "企业微信机器人",
        "fields": [
            ("QYWX_KEY", "机器人 Key", ""),
            ("QYWX_ORIGIN", "企业微信 API 地址，可空", "默认：https://qyapi.weixin.qq.com")
        ]
    },
    "qywxapp": {
        "name": "企业微信应用",
        "fields": [
            ("QYWX_AM", "企业微信应用配置", "corpid,corpsecret,touser,agentid，可选第5项 media_id"),
            ("QYWX_ORIGIN", "企业微信 API 地址，可空", "默认：https://qyapi.weixin.qq.com")
        ]
    },
    "dingding": {
        "name": "钉钉机器人",
        "fields": [
            ("DD_BOT_TOKEN", "Access Token", ""),
            ("DD_BOT_SECRET", "加签 Secret", "")
        ]
    },
    "feishu": {
        "name": "飞书机器人",
        "fields": [
            ("FSKEY", "Webhook Key", ""),
            ("FSSECRET", "签名 Secret，可空", "")
        ]
    },
    "smtp": {
        "name": "SMTP 邮件",
        "fields": [
            ("SMTP_SERVER", "SMTP 服务器", "例如：smtp.qq.com:465"),
            ("SMTP_SSL", "是否 SSL", "true / false"),
            ("SMTP_EMAIL", "邮箱", ""),
            ("SMTP_PASSWORD", "密码/授权码", ""),
            ("SMTP_NAME", "发件人名称", "")
        ]
    },
    "ntfy": {
        "name": "Ntfy",
        "fields": [
            ("NTFY_URL", "Ntfy 地址", "例如：https://ntfy.sh"),
            ("NTFY_TOPIC", "Topic", ""),
            ("NTFY_PRIORITY", "优先级", "1-5，默认 3"),
            ("NTFY_TOKEN", "Token，可空", ""),
            ("NTFY_USERNAME", "用户名，可空", ""),
            ("NTFY_PASSWORD", "密码，可空", ""),
            ("NTFY_ACTIONS", "动作，可空", "")
        ]
    },
    "wxpusher": {
        "name": "WxPusher",
        "fields": [
            ("WXPUSHER_APP_TOKEN", "App Token", ""),
            ("WXPUSHER_TOPIC_IDS", "Topic IDs", "多个用英文分号 ; 分隔"),
            ("WXPUSHER_UIDS", "UIDs", "多个用英文分号 ; 分隔")
        ]
    },
    "webhook": {
        "name": "自定义 Webhook",
        "fields": [
            ("WEBHOOK_URL", "请求 URL", "支持 $title / $content"),
            ("WEBHOOK_METHOD", "请求方法", "POST / GET"),
            ("WEBHOOK_CONTENT_TYPE", "Content-Type", "application/json / text/plain / application/x-www-form-urlencoded"),
            ("WEBHOOK_HEADERS", "请求头", "每行一个：Key: Value"),
            ("WEBHOOK_BODY", "请求体", "支持 $title / $content")
        ]
    },
    "gocqhttp": {
        "name": "go-cqhttp",
        "fields": [
            ("GOBOT_URL", "接口地址", "例如：http://127.0.0.1/send_private_msg"),
            ("GOBOT_QQ", "推送目标", "user_id=个人QQ 或 group_id=QQ群"),
            ("GOBOT_TOKEN", "Access Token，可空", "")
        ]
    },
    "gotify": {
        "name": "Gotify",
        "fields": [
            ("GOTIFY_URL", "Gotify 地址", "例如：https://push.example.com"),
            ("GOTIFY_TOKEN", "应用 Token", ""),
            ("GOTIFY_PRIORITY", "优先级", "默认 0")
        ]
    },
    "igot": {
        "name": "iGot",
        "fields": [
            ("IGOT_PUSH_KEY", "iGot Push Key", "")
        ]
    },
    "pushdeer": {
        "name": "PushDeer",
        "fields": [
            ("DEER_KEY", "PushDeer Key", ""),
            ("DEER_URL", "PushDeer URL，可空", "默认：https://api2.pushdeer.com/message/push")
        ]
    },
    "synology_chat": {
        "name": "Synology Chat",
        "fields": [
            ("CHAT_URL", "Chat URL", ""),
            ("CHAT_TOKEN", "Chat Token", "")
        ]
    },
    "weplus": {
        "name": "微加机器人",
        "fields": [
            ("WE_PLUS_BOT_TOKEN", "用户令牌", ""),
            ("WE_PLUS_BOT_RECEIVER", "接收者，可空", ""),
            ("WE_PLUS_BOT_VERSION", "调用版本", "pro")
        ]
    },
    "qmsg": {
        "name": "Qmsg 酱",
        "fields": [
            ("QMSG_KEY", "QMSG_KEY", ""),
            ("QMSG_TYPE", "QMSG_TYPE", "send / group 等")
        ]
    },
    "aibotk": {
        "name": "智能微秘书",
        "fields": [
            ("AIBOTK_KEY", "ApiKey", ""),
            ("AIBOTK_TYPE", "发送目标类型", "room 或 contact"),
            ("AIBOTK_NAME", "群名或好友昵称", "")
        ]
    },
    "pushme": {
        "name": "PushMe",
        "fields": [
            ("PUSHME_KEY", "PushMe Key", ""),
            ("PUSHME_URL", "PushMe URL，可空", "默认：https://push.i-i.me/")
        ]
    },
    "chronocat": {
        "name": "Chronocat",
        "fields": [
            ("CHRONOCAT_URL", "Chronocat URL", ""),
            ("CHRONOCAT_QQ", "推送目标", "可包含 user_id=123 或 group_id=456，支持多个"),
            ("CHRONOCAT_TOKEN", "Token", "")
        ]
    },
    "openilink": {
        "name": "OpeniLink",
        "fields": [
            ("OPENILINK_APP_TOKEN", "App Token", ""),
            ("OPENILINK_HUB_URL", "Hub URL，可空", "默认：https://hub.openilink.com"),
            ("OPENILINK_CONTEXT_TOKEN", "Context Token，可空", "")
        ]
    }
}


def _fls_notify_default_config():
    cfg = {
        "enabled": True,
        "default_channels": [],
        "channels": {}
    }

    for key in FLS_NOTIFY_CHANNELS:
        cfg["channels"][key] = {
            "enabled": False,
            "config": {}
        }

    cfg["channels"]["console"]["enabled"] = False
    return cfg


def _fls_notify_get_config():
    cfg = load_config()
    notify = cfg.get("notify")
    default = _fls_notify_default_config()

    if not isinstance(notify, dict):
        notify = {}

    merged = default
    merged.update({k: v for k, v in notify.items() if k != "channels"})

    chs = notify.get("channels")
    if not isinstance(chs, dict):
        chs = {}

    for key in FLS_NOTIFY_CHANNELS:
        old = chs.get(key)
        if not isinstance(old, dict):
            old = {}
        merged["channels"][key].update(old)
        if not isinstance(merged["channels"][key].get("config"), dict):
            merged["channels"][key]["config"] = {}

    if not isinstance(merged.get("default_channels"), list):
        merged["default_channels"] = []

    return merged


def _fls_notify_parse_headers(headers):
    parsed = {}
    for raw in str(headers or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        parsed[k.strip()] = v.strip()
    return parsed


def _fls_notify_webhook_body(body, content_type, title, content):
    raw = str(body or "")
    raw = raw.replace("$title", title).replace("$content", content)

    if not raw:
        return None

    if content_type == "application/json":
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False).encode("utf-8")
        except Exception:
            return raw.encode("utf-8")

    return raw.encode("utf-8")


def _fls_notify_select_options(selected=None, include_default=True, include_none=True):
    selected = selected or ["__default__"]
    if isinstance(selected, str):
        selected = [selected]

    options = ""

    if include_default:
        s = "selected" if "__default__" in selected else ""
        options += f'<option value="__default__" {s}>使用全局默认通知渠道</option>'

    if include_none:
        s = "selected" if "__none__" in selected else ""
        options += f'<option value="__none__" {s}>不通知</option>'

    for key, meta in FLS_NOTIFY_CHANNELS.items():
        s = "selected" if key in selected else ""
        options += f'<option value="{h(key)}" {s}>{h(meta.get("name", key))}</option>'

    return options
# 配置页增加通知管理入口
try:
    _fls_old_config_view_for_notify = app.view_functions.get("_fls_config_page")

    def _fls_config_page_with_notify_entry():
        resp = _fls_old_config_view_for_notify()
        if request.method != "GET":
            return resp

        if not isinstance(resp, str):
            return resp

        card = """
<div class="card">
    <div class="card-title">通知管理</div>
    <div class="help">
        配置 Bark、Server 酱、PushPlus、Telegram、企业微信机器人、钉钉、飞书、SMTP、Ntfy、WxPusher、自定义 Webhook 等通知渠道。
    </div>
    <br>
    <a class="btn btn-primary" href="/notify">进入通知管理</a>
</div>
"""
        return resp.replace("</form>", card + "</form>", 1)

    if _fls_old_config_view_for_notify:
        pass
except Exception as e:
    print(f"[NotifyPatch] 配置页入口补丁失败: {e}")


# 导航栏增加通知配置入口
try:
    _fls_old_layout_for_notify = layout

    def layout(title, active, body):
        html_text = _fls_old_layout_for_notify(title, active, body)
        notify_link = f'<a class="{"active" if active == "notify" else ""}" href="/notify">🔔 通知管理</a>'

        if 'href="/notify"' in html_text:
            return html_text

        html_text = html_text.replace(
            '<a class="active" href="/config">🔧 配置</a>',
            '<a class="active" href="/config">🔧 配置</a>' + notify_link
        ).replace(
            '<a class="" href="/config">🔧 配置</a>',
            '<a class="" href="/config">🔧 配置</a>' + notify_link
        )

        return html_text
except Exception as e:
    print(f"[NotifyPatch] 导航补丁失败: {e}")


# 任务表单增加通知选择
def _fls_task_form_with_notify(task=None):
    if task is None:
        task = {
            "id": "",
            "name": "",
            "command": "task ",
            "cron": "",
            "enabled": True,
            "proxy_id": "",
            "env": {},
            "notify_channels": ["__default__"]
        }

    token = tq()
    checked = "checked" if task.get("enabled", True) else ""
    env_text = env_to_text(task.get("env", {}) or {})
    proxy_options = proxy_select_options(task.get("proxy_id", ""))
    notify_options = _fls_notify_select_options(task.get("notify_channels", ["__default__"]))

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">任务信息</div>
    <div class="form-grid">
        <div class="form-item">
            <label>任务名</label>
            <input name="name" value="{h(task.get('name', ''))}" placeholder="例如：中国联通">
        </div>
        <div class="form-item">
            <label>Cron 表达式</label>
            <input name="cron" value="{h(task.get('cron', ''))}" placeholder="例如：0 8 * * *">
            <div class="help">留空表示手动运行任务。支持 5 位或 6 位 Cron。</div>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>命令</label>
        <input name="command" value="{h(task.get('command', ''))}" placeholder="task 1.py">
        <div class="help">
            <b>task 1.py</b> = 运行脚本目录下的 1.py。<br>
            不以 task 开头则作为系统命令执行。
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>代理</label>
        <select name="proxy_id">{proxy_options}</select>
        <div class="help">任务运行时会自动注入 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。</div>
    </div>

    <br>

    <div class="form-item">
        <label>任务结束通知</label>
        <select name="notify_channels" multiple size="8">{notify_options}</select>
        <div class="help">
            任务结束后会把日志发送到所选通知渠道。<br>
            选择“使用全局默认通知渠道”则跟随通知管理中的默认渠道；选择“不通知”则该任务不发送。
        </div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用任务
    </label>
</div>

<div class="card">
    <div class="card-title">任务变量</div>
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
    <div class="help">任务变量仅对此任务生效，会覆盖同名全局变量。</div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/tasks{token}">返回</a>
</div>
</form>

<div class="card">
    <div class="card-title">示例</div>
    <div class="code">
task chinaUnicom.py<br>
task a/test.sh arg1 arg2<br>
task demo.js<br>
python3 /root/other.py
    </div>
</div>
"""
    return body


task_form = _fls_task_form_with_notify
try:
    pass
    pass
except Exception as e:
    print(f"[NotifyPatch] 覆盖任务创建/编辑失败: {e}")


# 任务结束后通知日志


_fls_old_run_task_now_for_notify = _fls_base_run_task_now


# 覆盖全局 run_task_now 后，API / 定时器会自动使用新逻辑


# 登录成功通知
try:
    _fls_old_login_for_notify = app.view_functions.get("_fls_login")

    def _fls_login_with_notify():
        if request.method == "POST":
            token = fls_get_admin_token()
            input_token = request.form.get("token", "").strip()

            if token and input_token == token:
                ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
                ua = request.headers.get("User-Agent", "")
                try:
                    fls_notify_send(
                        "FLS 面板登录通知",
                        f"时间：{now_str()}\nIP：{ip}\nUser-Agent：{ua}",
                        None
                    )
                except Exception as e:
                    print(f"[NotifyPatch] 登录通知失败: {e}")

        return _fls_old_login_for_notify()

    if _fls_old_login_for_notify:
        pass
except Exception as e:
    print(f"[NotifyPatch] 登录通知补丁失败: {e}")


# 初始化通知配置
try:
    cfg = load_config()
    if "notify" not in cfg:
        cfg["notify"] = _fls_notify_default_config()
        save_config(cfg)
except Exception as e:
    print(f"[NotifyPatch] 初始化通知配置失败: {e}")

import base64 as _fls_fix_base64
import hashlib as _fls_fix_hashlib
import hmac as _fls_fix_hmac
import threading as _fls_fix_threading
import urllib.parse as _fls_fix_urlparse


def _fls_fix_real_newline(value):
    """
    把历史补丁中误写成字面量的 \\n 修正为真正换行。
    """
    return str(value or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


def _fls_fix_daemon_log_file():
    return LOG_DIR / "fls-manager-daemon.log"


def _fls_fix_write_daemon_log(text):
    """
    控制台通知明确写入 fls-manager-daemon.log。
    如果是 nohup / systemd 启动，print 本身也会进入运行日志；
    这里再主动追加一次，确保控制台渠道可追踪。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_fls_fix_daemon_log_file(), "a", encoding="utf-8") as f:
            f.write(str(text))
            if not str(text).endswith("\n"):
                f.write("\n")
    except Exception as e:
        try:
            print(f"[NotifyFix] 写入 daemon 日志失败: {e}")
        except Exception:
            pass


def _fls_notify_split_content(content, limit=2000):
    """
    覆盖原分片函数：
    - 先修正字面量 \\n；
    - 超过 2000 字符时，优先在 1900 - 2100 附近的换行、句号、逗号切分。
    """
    text = _fls_fix_real_newline(content)

    if len(text) <= limit:
        return [text]

    parts = []
    seps = ["\n", "。", "，", ",", "；", ";"]

    while len(text) > limit:
        start = min(1900, max(0, len(text) - 1))
        end = min(2100, len(text))
        best = -1
        best_distance = 999999

        for i in range(start, end):
            if i < len(text) and text[i] in seps:
                distance = abs(i - limit)
                if distance < best_distance:
                    best = i
                    best_distance = distance

        cut = limit if best < 0 else best + 1
        parts.append(text[:cut].strip())
        text = text[cut:].lstrip()

    if text:
        parts.append(text)

    return parts or [""]


def _fls_notify_join_title_content(title, content):
    """
    拼接通知正文。

    任务通知正文只显示脚本输出内容。
    需要 title 的渠道会通过独立标题字段展示任务名；
    纯文本渠道不再把 title 拼到正文前面，避免出现多余第一行或前置空白。
    """
    return str(content or "").lstrip("\r\n")

def _fls_notify_send_one(channel, title, content):
    """
    单通知渠道发送。

    本函数按青龙 notify.py 的渠道做移植，并保留 FLS 原有通知实例模式。
    """
    title = _fls_fix_real_newline(title) if "_fls_fix_real_newline" in globals() else str(title or "")
    content = _fls_fix_real_newline(content) if "_fls_fix_real_newline" in globals() else str(content or "")

    notify = _fls_notify_get_config()
    item = notify.get("channels", {}).get(channel, {})
    c = item.get("config", {}) or {}

    if not item.get("enabled"):
        return False, "渠道未启用"

    def _cfg(name, default=""):
        return str(c.get(name, default) if c.get(name, default) is not None else "").strip()

    def _json(resp):
        try:
            return resp.json()
        except Exception:
            return {
                "status_code": getattr(resp, "status_code", ""),
                "text": getattr(resp, "text", "")[:500],
            }

    def _rfc2047(value):
        return "=?utf-8?B?" + base64.b64encode(str(value).encode("utf-8")).decode("utf-8") + "?="

    try:
        if channel == "console":
            msg = (
                f"\n===== FLS 控制台通知 =====\n"
                f"时间：{now_str()}\n"
                f"标题：{title}\n"
                f"内容：\n{content}\n"
                f"==========================\n"
            )
            print(msg)
            if "_fls_fix_write_daemon_log" in globals():
                _fls_fix_write_daemon_log(msg)
            return True, "ok"

        if channel == "bark":
            push = _cfg("BARK_PUSH")
            if not push:
                return False, "BARK_PUSH 为空"

            url = push if push.startswith("http") else "https://api.day.app/" + push
            data = {"title": title, "body": content}
            mapping = {
                "BARK_ARCHIVE": "isArchive",
                "BARK_GROUP": "group",
                "BARK_SOUND": "sound",
                "BARK_ICON": "icon",
                "BARK_LEVEL": "level",
                "BARK_URL": "url",
            }
            for k, v in mapping.items():
                if _cfg(k):
                    data[v] = _cfg(k)

            r = requests.post(url, json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 200, str(js)

        if channel == "serverj":
            key = _cfg("PUSH_KEY")
            if not key:
                return False, "PUSH_KEY 为空"

            m = re.match(r"sctp(\d+)t", key)
            if m:
                url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send"
            else:
                url = f"https://sctapi.ftqq.com/{key}.send"

            r = requests.post(
                url,
                data={"text": title, "desp": content.replace("\n", "\n\n")},
                timeout=15,
            )
            js = _json(r)
            return js.get("errno") == 0 or js.get("code") == 0, str(js)

        if channel == "pushplus":
            token = _cfg("PUSH_PLUS_TOKEN")
            if not token:
                return False, "PUSH_PLUS_TOKEN 为空"

            data = {
                "token": token,
                "title": title,
                "content": content,
                "topic": _cfg("PUSH_PLUS_USER"),
                "template": _cfg("PUSH_PLUS_TEMPLATE", "html") or "html",
                "channel": _cfg("PUSH_PLUS_CHANNEL", "wechat") or "wechat",
                "webhook": _cfg("PUSH_PLUS_WEBHOOK"),
                "callbackUrl": _cfg("PUSH_PLUS_CALLBACKURL"),
                "to": _cfg("PUSH_PLUS_TO"),
            }
            r = requests.post("https://www.pushplus.plus/send", json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 200, str(js)

        if channel == "telegram":
            bot = _cfg("TG_BOT_TOKEN")
            uid = _cfg("TG_USER_ID")
            if not bot or not uid:
                return False, "TG_BOT_TOKEN 或 TG_USER_ID 为空"

            api_host = _cfg("TG_API_HOST").rstrip("/") or "https://api.telegram.org"
            url = f"{api_host}/bot{bot}/sendMessage"

            proxies = None
            if _cfg("TG_PROXY_HOST") and _cfg("TG_PROXY_PORT"):
                host = _cfg("TG_PROXY_HOST")
                if _cfg("TG_PROXY_AUTH") and "@" not in host:
                    host = _cfg("TG_PROXY_AUTH") + "@" + host
                proxy = "http://{}:{}".format(host, _cfg("TG_PROXY_PORT"))
                proxies = {"http": proxy, "https": proxy}

            r = requests.post(
                url,
                data={
                    "chat_id": uid,
                    "text": _fls_notify_join_title_content(title, content),
                    "disable_web_page_preview": "true",
                },
                proxies=proxies,
                timeout=20,
            )
            js = _json(r)
            return bool(js.get("ok")), str(js)

        if channel == "qywxbot":
            key = _cfg("QYWX_KEY")
            if not key:
                return False, "QYWX_KEY 为空"

            origin = _cfg("QYWX_ORIGIN").rstrip("/") or "https://qyapi.weixin.qq.com"
            url = f"{origin}/cgi-bin/webhook/send?key={key}"
            r = requests.post(
                url,
                json={"msgtype": "text", "text": {"content": _fls_notify_join_title_content(title, content)}},
                timeout=15,
            )
            js = _json(r)
            return js.get("errcode") == 0, str(js)

        if channel == "qywxapp":
            qywx_am = _cfg("QYWX_AM")
            if not qywx_am:
                return False, "QYWX_AM 为空"

            arr = [x.strip() for x in qywx_am.split(",")]
            if len(arr) not in (4, 5):
                return False, "QYWX_AM 格式错误，应为 corpid,corpsecret,touser,agentid[,media_id]"

            corpid, corpsecret, touser, agentid = arr[:4]
            media_id = arr[4] if len(arr) == 5 else ""
            origin = _cfg("QYWX_ORIGIN").rstrip("/") or "https://qyapi.weixin.qq.com"

            token_resp = requests.post(
                f"{origin}/cgi-bin/gettoken",
                params={"corpid": corpid, "corpsecret": corpsecret},
                timeout=15,
            )
            token_json = _json(token_resp)
            access_token = token_json.get("access_token")
            if not access_token:
                return False, str(token_json)

            send_url = f"{origin}/cgi-bin/message/send?access_token={access_token}"
            if media_id:
                data = {
                    "touser": touser,
                    "msgtype": "mpnews",
                    "agentid": agentid,
                    "mpnews": {
                        "articles": [{
                            "title": title,
                            "thumb_media_id": media_id,
                            "author": "FLS",
                            "content_source_url": "",
                            "content": content.replace("\n", "<br/>"),
                            "digest": content[:100],
                        }]
                    },
                }
            else:
                data = {
                    "touser": touser,
                    "msgtype": "text",
                    "agentid": agentid,
                    "text": {"content": _fls_notify_join_title_content(title, content)},
                    "safe": "0",
                }

            r = requests.post(send_url, json=data, timeout=15)
            js = _json(r)
            return js.get("errmsg") == "ok" or js.get("errcode") == 0, str(js)

        if channel == "dingding":
            token = _cfg("DD_BOT_TOKEN")
            secret = _cfg("DD_BOT_SECRET")
            if not token or not secret:
                return False, "DD_BOT_TOKEN 或 DD_BOT_SECRET 为空"

            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"https://oapi.dingtalk.com/robot/send?access_token={token}&timestamp={timestamp}&sign={sign}"
            r = requests.post(
                url,
                json={"msgtype": "text", "text": {"content": _fls_notify_join_title_content(title, content)}},
                timeout=15,
            )
            js = _json(r)
            return not js.get("errcode"), str(js)

        if channel == "feishu":
            key = _cfg("FSKEY")
            if not key:
                return False, "FSKEY 为空"

            url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{key}"
            data = {"msg_type": "text", "content": {"text": _fls_notify_join_title_content(title, content)}}

            secret = _cfg("FSSECRET")
            if secret:
                timestamp = str(int(time.time()))
                string_to_sign = f"{timestamp}\n{secret}"
                hmac_code = hmac.new(
                    string_to_sign.encode("utf-8"),
                    digestmod=hashlib.sha256,
                ).digest()
                data["timestamp"] = timestamp
                data["sign"] = base64.b64encode(hmac_code).decode("utf-8")

            r = requests.post(url, json=data, timeout=15)
            js = _json(r)
            return js.get("StatusCode") == 0 or js.get("code") == 0, str(js)

        if channel == "smtp":
            server = _cfg("SMTP_SERVER")
            email_addr = _cfg("SMTP_EMAIL")
            password = _cfg("SMTP_PASSWORD")
            name = _cfg("SMTP_NAME") or email_addr

            if not server or not email_addr or not password:
                return False, "SMTP 配置不完整"

            msg = _fls_MIMEText(content, "plain", "utf-8")
            msg["From"] = _fls_formataddr((_fls_Header(name, "utf-8").encode(), email_addr))
            msg["To"] = _fls_formataddr((_fls_Header(name, "utf-8").encode(), email_addr))
            msg["Subject"] = _fls_Header(title, "utf-8")

            if ":" in server:
                host, port = server.rsplit(":", 1)
                port = int(port)
            else:
                host = server
                port = 465 if _cfg("SMTP_SSL", "false").lower() == "true" else 25

            if _cfg("SMTP_SSL", "false").lower() == "true":
                smtp_obj = _fls_smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                smtp_obj = _fls_smtplib.SMTP(host, port, timeout=20)

            smtp_obj.login(email_addr, password)
            smtp_obj.sendmail(email_addr, email_addr, msg.as_bytes())
            smtp_obj.close()
            return True, "ok"

        if channel == "ntfy":
            url = _cfg("NTFY_URL").rstrip("/")
            topic = _cfg("NTFY_TOPIC")
            if not url or not topic:
                return False, "NTFY_URL 或 NTFY_TOPIC 为空"

            headers = {"Title": _rfc2047(title), "Priority": _cfg("NTFY_PRIORITY", "3") or "3"}

            if _cfg("NTFY_TOKEN"):
                headers["Authorization"] = "Bearer " + _cfg("NTFY_TOKEN")
            elif _cfg("NTFY_USERNAME") and _cfg("NTFY_PASSWORD"):
                auth = _cfg("NTFY_USERNAME") + ":" + _cfg("NTFY_PASSWORD")
                headers["Authorization"] = "Basic " + base64.b64encode(auth.encode("utf-8")).decode("utf-8")

            if _cfg("NTFY_ACTIONS"):
                headers["Actions"] = _rfc2047(_cfg("NTFY_ACTIONS"))

            r = requests.post(f"{url}/{topic}", data=content.encode("utf-8"), headers=headers, timeout=15)
            return r.status_code in (200, 201, 202), r.text[:500]

        if channel == "wxpusher":
            app_token = _cfg("WXPUSHER_APP_TOKEN")
            if not app_token:
                return False, "WXPUSHER_APP_TOKEN 为空"

            topic_ids = []
            if _cfg("WXPUSHER_TOPIC_IDS"):
                topic_ids = [int(x.strip()) for x in _cfg("WXPUSHER_TOPIC_IDS").split(";") if x.strip()]

            uids = []
            if _cfg("WXPUSHER_UIDS"):
                uids = [x.strip() for x in _cfg("WXPUSHER_UIDS").split(";") if x.strip()]

            if not topic_ids and not uids:
                return False, "WXPUSHER_TOPIC_IDS 和 WXPUSHER_UIDS 至少填写一个"

            data = {
                "appToken": app_token,
                "content": (f"<h2>{html.escape(title)}</h2>" if str(title or "").strip() else "") + f"<pre style='white-space:pre-wrap'>{html.escape(content)}</pre>",
                "summary": title[:96],
                "contentType": 2,
                "topicIds": topic_ids,
                "uids": uids,
                "verifyPayType": 0,
            }
            r = requests.post("https://wxpusher.zjiecode.com/api/send/message", json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 1000, str(js)

        if channel == "webhook":
            url = _cfg("WEBHOOK_URL")
            method = (_cfg("WEBHOOK_METHOD") or "POST").upper()
            content_type = _cfg("WEBHOOK_CONTENT_TYPE") or "application/json"

            if not url:
                return False, "WEBHOOK_URL 为空"

            url = url.replace("$title", urllib.parse.quote_plus(title)).replace("$content", urllib.parse.quote_plus(content))
            headers = _fls_notify_parse_headers(c.get("WEBHOOK_HEADERS", ""))
            if content_type:
                headers.setdefault("Content-Type", content_type)

            body = _fls_notify_webhook_body(c.get("WEBHOOK_BODY", ""), content_type, title, content)
            r = requests.request(method=method, url=url, headers=headers, data=body, timeout=15)
            return 200 <= r.status_code < 300, f"{r.status_code} {r.text[:500]}"

        if channel == "gocqhttp":
            url = _cfg("GOBOT_URL")
            target = _cfg("GOBOT_QQ")
            token = _cfg("GOBOT_TOKEN")
            if not url or not target:
                return False, "GOBOT_URL 或 GOBOT_QQ 为空"

            params = {}
            for part in target.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v
            params["message"] = _fls_notify_join_title_content(title, content)
            if token:
                params["access_token"] = token

            r = requests.get(url, params=params, timeout=15)
            js = _json(r)
            return js.get("status") == "ok" or js.get("retcode") == 0, str(js)

        if channel == "gotify":
            url = _cfg("GOTIFY_URL").rstrip("/")
            token = _cfg("GOTIFY_TOKEN")
            if not url or not token:
                return False, "GOTIFY_URL 或 GOTIFY_TOKEN 为空"

            r = requests.post(
                f"{url}/message",
                params={"token": token},
                data={
                    "title": title,
                    "message": content,
                    "priority": _cfg("GOTIFY_PRIORITY", "0") or "0",
                },
                timeout=15,
            )
            js = _json(r)
            return bool(js.get("id")), str(js)

        if channel == "igot":
            key = _cfg("IGOT_PUSH_KEY")
            if not key:
                return False, "IGOT_PUSH_KEY 为空"

            r = requests.post(
                f"https://push.hellyw.com/{key}",
                data={"title": title, "content": content},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            js = _json(r)
            return js.get("ret") == 0, str(js)

        if channel == "pushdeer":
            key = _cfg("DEER_KEY")
            if not key:
                return False, "DEER_KEY 为空"

            url = _cfg("DEER_URL") or "https://api2.pushdeer.com/message/push"
            r = requests.post(
                url,
                data={"pushkey": key, "text": title, "desp": content, "type": "markdown"},
                timeout=15,
            )
            js = _json(r)
            ok = False
            try:
                ok = len(js.get("content", {}).get("result", [])) > 0
            except Exception:
                ok = js.get("code") == 0
            return ok, str(js)

        if channel == "synology_chat":
            url = _cfg("CHAT_URL")
            token = _cfg("CHAT_TOKEN")
            if not url or not token:
                return False, "CHAT_URL 或 CHAT_TOKEN 为空"

            r = requests.post(
                url + token,
                data="payload=" + json.dumps({"text": _fls_notify_join_title_content(title, content)}, ensure_ascii=False),
                timeout=15,
            )
            return r.status_code == 200, f"{r.status_code} {r.text[:500]}"

        if channel == "weplus":
            token = _cfg("WE_PLUS_BOT_TOKEN")
            if not token:
                return False, "WE_PLUS_BOT_TOKEN 为空"

            template = "html" if len(content) > 800 else "txt"
            data = {
                "token": token,
                "title": title,
                "content": content,
                "template": template,
                "receiver": _cfg("WE_PLUS_BOT_RECEIVER"),
                "version": _cfg("WE_PLUS_BOT_VERSION", "pro") or "pro",
            }
            r = requests.post("https://www.weplusbot.com/send", json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 200, str(js)

        if channel == "qmsg":
            key = _cfg("QMSG_KEY")
            qtype = _cfg("QMSG_TYPE")
            if not key or not qtype:
                return False, "QMSG_KEY 或 QMSG_TYPE 为空"

            url = f"https://qmsg.zendee.cn/{qtype}/{key}"
            r = requests.post(url, params={"msg": f"{title}\n\n{content.replace('----', '-')}"},
                              timeout=15)
            js = _json(r)
            return js.get("code") == 0, str(js)

        if channel == "aibotk":
            key = _cfg("AIBOTK_KEY")
            atype = _cfg("AIBOTK_TYPE")
            name = _cfg("AIBOTK_NAME")
            if not key or not atype or not name:
                return False, "AIBOTK_KEY / AIBOTK_TYPE / AIBOTK_NAME 配置不完整"

            if atype == "room":
                url = "https://api-bot.aibotk.com/openapi/v1/chat/room"
                data = {"apiKey": key, "roomName": name, "message": {"type": 1, "content": f"【FLS 通知】\n\n{title}\n{content}"}}
            else:
                url = "https://api-bot.aibotk.com/openapi/v1/chat/contact"
                data = {"apiKey": key, "name": name, "message": {"type": 1, "content": f"【FLS 通知】\n\n{title}\n{content}"}}

            r = requests.post(url, json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 0, str(js)

        if channel == "pushme":
            key = _cfg("PUSHME_KEY")
            if not key:
                return False, "PUSHME_KEY 为空"

            url = _cfg("PUSHME_URL") or "https://push.i-i.me/"
            r = requests.post(url, data={"push_key": key, "title": title, "content": content}, timeout=15)
            return r.status_code == 200 and r.text.strip() == "success", f"{r.status_code} {r.text[:500]}"

        if channel == "chronocat":
            base_url = _cfg("CHRONOCAT_URL").rstrip("/")
            qq = _cfg("CHRONOCAT_QQ")
            token = _cfg("CHRONOCAT_TOKEN")
            if not base_url or not qq or not token:
                return False, "CHRONOCAT_URL / CHRONOCAT_QQ / CHRONOCAT_TOKEN 配置不完整"

            user_ids = re.findall(r"user_id=(\d+)", qq)
            group_ids = re.findall(r"group_id=(\d+)", qq)
            if not user_ids and not group_ids:
                return False, "CHRONOCAT_QQ 中未找到 user_id 或 group_id"

            url = f"{base_url}/api/message/send"
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            results = []
            all_ok = True

            for chat_type, ids in [(1, user_ids), (2, group_ids)]:
                for chat_id in ids:
                    data = {
                        "peer": {"chatType": chat_type, "peerUin": chat_id},
                        "elements": [{"elementType": 1, "textElement": {"content": _fls_notify_join_title_content(title, content)}}],
                    }
                    r = requests.post(url, headers=headers, json=data, timeout=15)
                    ok = r.status_code == 200
                    all_ok = all_ok and ok
                    results.append(f"{chat_id}:{r.status_code}:{r.text[:200]}")

            return all_ok, "; ".join(results)

        if channel == "openilink":
            token = _cfg("OPENILINK_APP_TOKEN")
            if not token:
                return False, "OPENILINK_APP_TOKEN 为空"

            base_url = _cfg("OPENILINK_HUB_URL").rstrip("/") or "https://hub.openilink.com"
            data = {"type": "text", "content": _fls_notify_join_title_content(title, content)}
            if _cfg("OPENILINK_CONTEXT_TOKEN"):
                data["context_token"] = _cfg("OPENILINK_CONTEXT_TOKEN")

            r = requests.post(
                f"{base_url}/bot/v1/message/send",
                json=data,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=15,
            )
            js = _json(r)
            return bool(js.get("ok")), str(js)

        return False, "未知渠道"

    except Exception as e:
        return False, str(e)
try:
    pass
except Exception as e:
    print(f"[NotifyFix] 覆盖登录视图失败: {e}")
try:
    pass
except Exception as e:
    print(f"[NotifyFix] 覆盖通知配置页失败: {e}")


def _fls_config_page_with_notify_entry_pretty():
    """
    重新包装配置页中的通知入口，避免重复插入太多原生卡片。
    """
    resp = _fls_old_config_view_for_notify() if "_fls_old_config_view_for_notify" in globals() and _fls_old_config_view_for_notify else None

    if request.method != "GET":
        return resp

    if not isinstance(resp, str):
        return resp

    if 'href="/notify"' in resp and "通知管理" in resp:
        return resp

    card = f"""
<div class="card">
    <div class="card-title">通知管理</div>
    <div class="help">
        配置任务结束通知和登录通知。控制台日志渠道会写入：<code>{h(_fls_fix_daemon_log_file())}</code>
    </div>
    <br>
    <a class="btn btn-primary" href="/notify">进入通知管理</a>
</div>
"""
    return resp.replace("</form>", card + "</form>", 1)


try:
    if "_fls_old_config_view_for_notify" in globals() and _fls_old_config_view_for_notify:
        app.view_functions["_fls_config_page"] = _fls_config_page_with_notify_entry_pretty
except Exception as e:
    print(f"[NotifyFix] 覆盖配置入口失败: {e}")


def _fls_notify_fix_runtime_note():
    try:
        _fls_fix_write_daemon_log(
            f"[NotifyFix] 补丁已加载，控制台通知日志文件：{_fls_fix_daemon_log_file()}"
        )
    except Exception:
        pass


_fls_notify_fix_runtime_note()

import copy as _fls_patch_copy
import threading as _fls_patch_threading
from math import ceil as _fls_patch_ceil


def _fls_patch_cfg():
    try:
        return load_config()
    except Exception:
        return {}


def _fls_patch_save_cfg(cfg):
    try:
        save_config(cfg)
    except Exception as e:
        print(f"[Patch] 保存配置失败: {e}")


# ============================================================
# 通知实例管理
# ============================================================
def _fls_notify_items():
    cfg = _fls_patch_cfg()
    items = cfg.get("notify_items")
    if isinstance(items, list):
        changed = False
        for it in items:
            if "enabled" not in it:
                it["enabled"] = True
                changed = True
            if "config" not in it or not isinstance(it.get("config"), dict):
                it["config"] = {}
                changed = True
        if changed:
            cfg["notify_items"] = items
            _fls_patch_save_cfg(cfg)
        return items

    items = []
    old = cfg.get("notify")
    if isinstance(old, dict):
        channels = old.get("channels", {}) or {}
        for key, meta in FLS_NOTIFY_CHANNELS.items():
            one = channels.get(key, {}) or {}
            if one.get("enabled"):
                items.append({
                    "id": uuid.uuid4().hex,
                    "name": meta.get("name", key),
                    "channel": key,
                    "enabled": True,
                    "config": one.get("config", {}) or {},
                    "created_at": now_str(),
                    "updated_at": now_str(),
                })

    cfg["notify_items"] = items
    if "notify_default_ids" not in cfg:
        cfg["notify_default_ids"] = []
    _fls_patch_save_cfg(cfg)
    return items


def _fls_notify_save_items(items):
    cfg = _fls_patch_cfg()
    cfg["notify_items"] = items
    _fls_patch_save_cfg(cfg)


def _fls_notify_default_ids():
    cfg = _fls_patch_cfg()
    ids = cfg.get("notify_default_ids")
    if isinstance(ids, list):
        enabled = {x.get("id") for x in _fls_notify_items() if x.get("enabled", True)}
        return [x for x in ids if x in enabled]
    return []


def _fls_notify_save_default_ids(ids):
    cfg = _fls_patch_cfg()
    enabled = {x.get("id") for x in _fls_notify_items() if x.get("enabled", True)}
    cfg["notify_default_ids"] = [x for x in ids if x in enabled]
    _fls_patch_save_cfg(cfg)


def _fls_notify_get_item(item_id):
    for it in _fls_notify_items():
        if it.get("id") == item_id:
            return it
    return None


def _fls_notify_unique_name(channel, name="", exclude_id=""):
    base = str(name or "").strip()
    if not base:
        base = FLS_NOTIFY_CHANNELS.get(channel, {}).get("name", channel)

    names = {
        str(x.get("name", ""))
        for x in _fls_notify_items()
        if x.get("id") != exclude_id
    }

    if base not in names:
        return base

    idx = 1
    while True:
        candidate = f"{base}-{idx}"
        if candidate not in names:
            return candidate
        idx += 1


def _fls_notify_item_from_form(old=None):
    old = old or {}
    channel = request.form.get("channel", old.get("channel", "console")).strip() or "console"
    meta = FLS_NOTIFY_CHANNELS.get(channel, FLS_NOTIFY_CHANNELS.get("console", {}))

    cfg = {}
    for field, label, placeholder in meta.get("fields", []):
        cfg[field] = request.form.get(field, "")

    return {
        "id": old.get("id") or uuid.uuid4().hex,
        "name": _fls_notify_unique_name(channel, request.form.get("name", ""), old.get("id", "")),
        "channel": channel,
        "enabled": request.form.get("enabled", "1") == "1",
        "config": cfg,
        "created_at": old.get("created_at") or now_str(),
        "updated_at": now_str(),
    }


def _fls_notify_item_form(item=None):
    if item is None:
        item = {
            "id": "",
            "name": "",
            "channel": request.args.get("channel", "console") or "console",
            "enabled": True,
            "config": {},
        }

    channel = item.get("channel", "console")
    cfg = item.get("config", {}) or {}
    checked = "checked" if item.get("enabled", True) else ""

    channel_options = ""
    for key, meta in FLS_NOTIFY_CHANNELS.items():
        selected = "selected" if key == channel else ""
        channel_options += f'<option value="{h(key)}" {selected}>{h(meta.get("name", key))}</option>'

    fields_html = ""
    meta = FLS_NOTIFY_CHANNELS.get(channel, FLS_NOTIFY_CHANNELS.get("console", {}))
    if meta.get("fields"):
        for field, label, placeholder in meta.get("fields", []):
            fields_html += f"""
<div class="form-item">
    <label>{h(label)}</label>
    <input name="{h(field)}" value="{h(cfg.get(field, ""))}" placeholder="{h(placeholder)}">
</div>
"""
    else:
        fields_html = '<div class="help">该渠道无需额外配置。</div>'

    body = f"""
<style>
.fls-notify-tip {{
    background: linear-gradient(135deg, #ecfdf5, #eff6ff);
}}
</style>
<form method="post">
<div class="card fls-notify-tip">
    <div class="card-title">{"编辑通知" if item.get("id") else "新增通知"}</div>
    <div class="help">
        通知名称为空时自动使用渠道名；如果名称重复，会自动追加 -1、-2。
    </div>
</div>

<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>通知名称</label>
            <input name="name" value="{h(item.get("name", ""))}" placeholder="例如：微信机器人">
        </div>
        <div class="form-item">
            <label>通知渠道</label>
            <select name="channel" onchange="this.form.submit()">{channel_options}</select>
            <div class="help">选择渠道后会显示该渠道的配置项。</div>
        </div>
    </div>
    <br>
    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用此通知
    </label>
</div>

<div class="card">
    <div class="card-title">{h(meta.get("name", channel))} 配置</div>
    <div class="form-grid">
        {fields_html}
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit" name="action" value="save">保存</button>
    <button class="btn btn-orange" type="submit" name="action" value="test">保存并测试</button>
    <a class="btn btn-gray" href="/notify">返回</a>
</div>
</form>
"""
    return layout("通知配置", "notify", body)


def _fls_notify_send_item(item, title, content):
    if not item or not item.get("enabled", True):
        return False, "通知已禁用"

    channel = item.get("channel", "")
    config = item.get("config", {}) or {}

    if channel not in FLS_NOTIFY_CHANNELS:
        return False, "未知通知渠道"

    old_func = globals().get("_fls_notify_get_config")
    lock = globals().setdefault("_fls_notify_item_send_lock", _fls_patch_threading.Lock())

    def fake_config():
        base = _fls_notify_default_config()
        base["enabled"] = True
        base["default_channels"] = [channel]
        if channel not in base["channels"]:
            base["channels"][channel] = {"enabled": True, "config": config}
        base["channels"][channel]["enabled"] = True
        base["channels"][channel]["config"] = config
        return base

    with lock:
        try:
            globals()["_fls_notify_get_config"] = fake_config
            ok, msg = _fls_notify_send_one(channel, title, content)
            return ok, msg
        finally:
            if old_func:
                globals()["_fls_notify_get_config"] = old_func


@app.route("/notify")
def _fls_notify_page_new():
    items = _fls_notify_items()
    defaults = set(_fls_notify_default_ids())

    rows = ""
    if not items:
        rows = '<tr><td colspan="6">暂无通知，请点击新增通知</td></tr>'
    else:
        for it in items:
            item_id = it.get("id")
            cname = FLS_NOTIFY_CHANNELS.get(it.get("channel"), {}).get("name", it.get("channel"))
            badge = '<span class="badge green">启用</span>' if it.get("enabled", True) else '<span class="badge gray">禁用</span>'
            default_badge = '<span class="badge blue">默认</span>' if item_id in defaults else '<span class="badge gray">-</span>'
            toggle_text = "禁用" if it.get("enabled", True) else "启用"
            toggle_class = "btn-gray" if it.get("enabled", True) else "btn-primary"

            rows += f"""
<tr>
    <td><b>{h(it.get("name", ""))}</b></td>
    <td>{h(cname)}</td>
    <td>{badge}</td>
    <td>{default_badge}</td>
    <td>{h(it.get("updated_at", "-"))}</td>
    <td>
        <a class="btn btn-orange" href="/notify/test/{h(item_id)}">测试</a>
        <a class="btn btn-blue" href="/notify/edit/{h(item_id)}">编辑</a>
        <a class="btn {toggle_class}" href="/notify/toggle/{h(item_id)}">{toggle_text}</a>
        <a class="btn btn-red" href="/notify/delete/{h(item_id)}" onclick="return confirm('确定删除该通知吗？')">删除</a>
    </td>
</tr>
"""

    default_options = _fls_notify_select_options_items(defaults, include_default=False, include_none=False)

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">通知管理</div>
            <div class="help">
                可新增多个通知配置，同一渠道也可以配置多份，便于区分不同用途。
            </div>
        </div>
        <a class="btn btn-primary" href="/notify/new">新增通知</a>
    </div>
</div>

<form method="post" action="/notify/default">
<div class="card">
    <div class="card-title">默认通知</div>
    <select name="default_ids" multiple size="6">{default_options}</select>
    <div class="help">任务选择“使用全局默认通知”时会使用这里勾选的通知。禁用通知不会出现在此处。</div>
    <br>
    <button class="btn btn-primary" type="submit">保存默认通知</button>
</div>
</form>

<div class="card">
    <div class="card-title">通知列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>渠道</th>
                    <th>状态</th>
                    <th>默认</th>
                    <th>更新时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>
"""
    return layout("通知管理", "notify", body)


@app.route("/notify/default", methods=["POST"])
def _fls_notify_default_save():
    _fls_notify_save_default_ids(request.form.getlist("default_ids"))
    return redirect(url_for("_fls_notify_page_new"))


@app.route("/notify/new", methods=["GET", "POST"])
def _fls_notify_new_item():
    if request.method == "POST":
        item = _fls_notify_item_from_form()
        items = _fls_notify_items()
        items.append(item)
        _fls_notify_save_items(items)

        if request.form.get("action") == "test":
            _fls_notify_send_item(item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

        return redirect(url_for("_fls_notify_page_new"))

    return _fls_notify_item_form()


@app.route("/notify/edit/<item_id>", methods=["GET", "POST"])
def _fls_notify_edit_item(item_id):
    items = _fls_notify_items()
    item = None
    for it in items:
        if it.get("id") == item_id:
            item = it
            break

    if not item:
        abort(404)

    if request.method == "POST":
        new_item = _fls_notify_item_from_form(item)
        for idx, it in enumerate(items):
            if it.get("id") == item_id:
                items[idx] = new_item
                break
        _fls_notify_save_items(items)

        if request.form.get("action") == "test":
            _fls_notify_send_item(new_item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

        return redirect(url_for("_fls_notify_page_new"))

    return _fls_notify_item_form(item)


@app.route("/notify/toggle/<item_id>")
def _fls_notify_toggle_item(item_id):
    items = _fls_notify_items()
    for it in items:
        if it.get("id") == item_id:
            it["enabled"] = not it.get("enabled", True)
            it["updated_at"] = now_str()
            break
    _fls_notify_save_items(items)
    _fls_notify_save_default_ids(_fls_notify_default_ids())
    return redirect(url_for("_fls_notify_page_new"))


@app.route("/notify/delete/<item_id>")
def _fls_notify_delete_item(item_id):
    items = [x for x in _fls_notify_items() if x.get("id") != item_id]
    _fls_notify_save_items(items)
    _fls_notify_save_default_ids(_fls_notify_default_ids())

    tasks = load_tasks()
    changed = False
    for t in tasks:
        ids = t.get("notify_ids")
        if isinstance(ids, list) and item_id in ids:
            t["notify_ids"] = [x for x in ids if x != item_id]
            changed = True
    if changed:
        save_tasks(tasks)

    return redirect(url_for("_fls_notify_page_new"))


@app.route("/notify/test/<item_id>")
def _fls_notify_test_item(item_id):
    item = _fls_notify_get_item(item_id)
    if not item:
        abort(404)

    ok, msg = _fls_notify_send_item(item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

    body = f"""
<div class="card">
    <div class="card-title">通知测试结果</div>
    <div class="help">
        通知：<b>{h(item.get("name"))}</b><br>
        结果：<b>{"成功" if ok else "失败"}</b><br>
        返回：{h(msg)}
    </div>
    <br>
    <a class="btn btn-gray" href="/notify">返回通知管理</a>
</div>
"""
    return layout("通知测试", "notify", body)


# ============================================================
# 代理禁用支持
# ============================================================
_fls_patch_old_get_proxy = _fls_base_get_proxy


_fls_patch_old_load_proxies = _fls_base_load_proxies


@app.route("/proxy/toggle/<proxy_id>")
def _fls_proxy_toggle(proxy_id):
    proxies = load_proxies()
    for p in proxies:
        if p.get("id") == proxy_id:
            p["enabled"] = not p.get("enabled", True)
            p["updated_at"] = now_str()
            break
    save_proxies(proxies)
    return redirect(url_for("proxy_page"))
pass


# 给原代理表单补一个启用字段
_fls_patch_old_proxy_form = _fls_base_proxy_form


# ============================================================
# 任务表单 / 任务分页搜索 / 必填校验
# ============================================================
def _fls_task_form_patched(task=None):
    if task is None:
        task = {
            "id": "",
            "name": "",
            "command": "task ",
            "cron": "",
            "enabled": True,
            "proxy_id": "",
            "env": {},
            "notify_ids": ["__default__"],
        }

    if "notify_ids" not in task:
        old = task.get("notify_channels", ["__default__"])
        task["notify_ids"] = old if old in (["__default__"], ["__none__"]) else ["__default__"]

    checked = "checked" if task.get("enabled", True) else ""
    env_text = env_to_text(task.get("env", {}) or {})
    proxy_options = proxy_select_options(task.get("proxy_id", ""))
    notify_options = _fls_notify_select_options_items(task.get("notify_ids", ["__default__"]))

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">任务信息</div>
    <div class="form-grid">
        <div class="form-item">
            <label>任务名，必填</label>
            <input name="name" required value="{h(task.get("name", ""))}" placeholder="例如：中国联通">
        </div>
        <div class="form-item">
            <label>Cron 表达式</label>
            <input name="cron" value="{h(task.get("cron", ""))}" placeholder="例如：0 8 * * *">
            <div class="help">留空表示手动运行任务。支持 5 位或 6 位 Cron。</div>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>命令，必填</label>
        <input name="command" required value="{h(task.get("command", ""))}" placeholder="task 1.py">
    </div>

    <br>

    <div class="form-item">
        <label>代理</label>
        <select name="proxy_id">{proxy_options}</select>
        <div class="help">只显示已启用代理。已禁用代理会被自动忽略。</div>
    </div>

    <br>

    <div class="form-item">
        <label>任务结束通知</label>
        <select name="notify_ids" multiple size="8">{notify_options}</select>
        <div class="help">只显示已启用通知。若任务之前选中过已禁用通知，发送时会自动跳过。</div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用任务
    </label>
</div>

<div class="card">
    <div class="card-title">任务变量</div>
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/tasks">返回</a>
</div>
</form>
"""
    return body


task_form = _fls_task_form_patched
pass
pass


def _fls_tasks_filter_page(tasks):
    q = request.args.get("q", "").strip().lower()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 10

    if q:
        tasks = [
            t for t in tasks
            if q in str(t.get("name", "")).lower()
            or q in str(t.get("command", "")).lower()
            or q in str(t.get("cron", "")).lower()
        ]

    total = len(tasks)
    pages = max(1, _fls_patch_ceil(total / per_page))
    page = min(page, pages)
    start = (page - 1) * per_page
    return q, page, pages, total, tasks[start:start + per_page]


def _fls_page_links(base, q, page, pages):
    if pages <= 1:
        return ""
    links = ""
    for i in range(1, pages + 1):
        cls = "btn-primary" if i == page else "btn-gray"
        url = f"{base}?page={i}"
        if q:
            url += "&q=" + _fls_url_quote(q)
        links += f'<a class="btn {cls}" href="{h(url)}">{i}</a>'
    return f'<div class="card"><div class="action-row">{links}</div></div>'


def _fls_tasks_page_patched():
    tasks = load_tasks()
    q, page, pages, total, show_tasks = _fls_tasks_filter_page(tasks)

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务管理</div>
            <div class="help">共 {total} 个匹配任务；超过 10 个自动分页。</div>
        </div>
        <a class="btn btn-primary" href="/task/new">新建任务</a>
    </div>
</div>

<form method="get">
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索任务</label>
            <input name="q" value="{h(q)}" placeholder="任务名 / 命令 / Cron">
        </div>
        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">搜索</button>
            <a class="btn btn-gray" href="/tasks">重置</a>
        </div>
    </div>
</div>
</form>

<div class="card">
    <div class="table-wrap">{tasks_table(show_tasks)}</div>
</div>
{_fls_page_links("/tasks", q, page, pages)}
"""
    return layout("任务管理", "tasks", body)


app.view_functions["tasks_page"] = _fls_tasks_page_patched
# ============================================================
# 任务结束通知修复
# ============================================================


# ============================================================
# 日志中心改名日志管理 + 搜索分页折叠
# ============================================================
def _fls_patch_nav_rename_logs():
    global layout
    old_layout = layout

    def layout(title, active, body):
        html_text = old_layout(title, active, body)
        html_text = html_text.replace("📁 日志中心", "📁 日志管理")
        html_text = html_text.replace("<title>日志中心</title>", "<title>日志管理</title>")
        return html_text

    globals()["layout"] = layout


_fls_patch_nav_rename_logs()
pass


# 初始化通知实例配置
try:
    _fls_notify_items()
except Exception as e:
    print(f"[Patch] 初始化通知实例失败: {e}")

from urllib.parse import quote as _fls_rewrite_quote
import threading as _fls_rewrite_threading


def _fls_rewrite_notify_channels():
    """
    可配置通知渠道。
    去掉 console / 控制台日志，避免和日志管理重复。
    """
    return {
        k: v
        for k, v in FLS_NOTIFY_CHANNELS.items()
        if k != "console"
    }


def _fls_rewrite_cfg():
    try:
        return load_config()
    except Exception:
        return {}


def _fls_rewrite_save_cfg(cfg):
    try:
        save_config(cfg)
    except Exception as e:
        print(f"[NotifyRewrite] 保存配置失败: {e}")


def _fls_rewrite_channel_name(channel):
    return _fls_rewrite_notify_channels().get(channel, {}).get("name", channel)


def _fls_rewrite_notify_items():
    """
    通知实例列表。
    自动迁移旧 notify_items，并移除 console。
    """
    cfg = _fls_rewrite_cfg()
    items = cfg.get("notify_items")

    if not isinstance(items, list):
        items = []

        old = cfg.get("notify")
        if isinstance(old, dict):
            old_channels = old.get("channels", {}) or {}
            for key, meta in _fls_rewrite_notify_channels().items():
                old_one = old_channels.get(key, {}) or {}
                if old_one.get("enabled"):
                    items.append({
                        "id": uuid.uuid4().hex,
                        "name": meta.get("name", key),
                        "channel": key,
                        "enabled": True,
                        "config": old_one.get("config", {}) or {},
                        "created_at": now_str(),
                        "updated_at": now_str(),
                    })

    changed = False
    cleaned = []

    for item in items:
        if not isinstance(item, dict):
            changed = True
            continue

        channel = item.get("channel")
        if channel == "console" or channel not in _fls_rewrite_notify_channels():
            changed = True
            continue

        if not item.get("id"):
            item["id"] = uuid.uuid4().hex
            changed = True

        if "enabled" not in item:
            item["enabled"] = True
            changed = True

        if not isinstance(item.get("config"), dict):
            item["config"] = {}
            changed = True

        if not item.get("name"):
            item["name"] = _fls_rewrite_channel_name(channel)
            changed = True

        cleaned.append(item)

    if cleaned != items:
        changed = True

    cfg["notify_items"] = cleaned

    default_ids = cfg.get("notify_default_ids")
    if not isinstance(default_ids, list):
        default_ids = []

    valid_enabled = {
        x.get("id")
        for x in cleaned
        if x.get("enabled", True)
    }

    new_default_ids = []
    for item_id in default_ids:
        if item_id in valid_enabled and item_id not in new_default_ids:
            new_default_ids.append(item_id)

    if new_default_ids != default_ids:
        cfg["notify_default_ids"] = new_default_ids
        changed = True

    if changed:
        _fls_rewrite_save_cfg(cfg)

    return cleaned


def _fls_rewrite_save_notify_items(items):
    cfg = _fls_rewrite_cfg()
    cfg["notify_items"] = items
    _fls_rewrite_save_cfg(cfg)


def _fls_rewrite_enabled_notify_items():
    return [
        x for x in _fls_rewrite_notify_items()
        if x.get("enabled", True)
    ]


def _fls_rewrite_notify_default_ids():
    cfg = _fls_rewrite_cfg()
    ids = cfg.get("notify_default_ids")
    if not isinstance(ids, list):
        return []

    enabled = {
        x.get("id")
        for x in _fls_rewrite_enabled_notify_items()
    }

    result = []
    for item_id in ids:
        if item_id in enabled and item_id not in result:
            result.append(item_id)

    return result


def _fls_rewrite_save_notify_default_ids(ids):
    enabled = {
        x.get("id")
        for x in _fls_rewrite_enabled_notify_items()
    }

    result = []
    for item_id in ids or []:
        if item_id in enabled and item_id not in result:
            result.append(item_id)

    cfg = _fls_rewrite_cfg()
    cfg["notify_default_ids"] = result
    _fls_rewrite_save_cfg(cfg)


def _fls_rewrite_get_notify_item(item_id):
    for item in _fls_rewrite_notify_items():
        if item.get("id") == item_id:
            return item
    return None


def _fls_rewrite_unique_notify_name(channel, name="", exclude_id=""):
    base = str(name or "").strip()
    if not base:
        base = _fls_rewrite_channel_name(channel)

    exists = {
        str(x.get("name", ""))
        for x in _fls_rewrite_notify_items()
        if x.get("id") != exclude_id
    }

    if base not in exists:
        return base

    idx = 1
    while True:
        candidate = f"{base}-{idx}"
        if candidate not in exists:
            return candidate
        idx += 1


def _fls_rewrite_notify_item_from_form(old=None):
    old = old or {}
    channels = _fls_rewrite_notify_channels()

    channel = request.form.get("channel", old.get("channel", "bark")).strip()
    if channel not in channels:
        channel = next(iter(channels.keys()))

    meta = channels[channel]

    config = {}
    for field, label, placeholder in meta.get("fields", []):
        config[field] = request.form.get(field, "")

    return {
        "id": old.get("id") or uuid.uuid4().hex,
        "name": _fls_rewrite_unique_notify_name(channel, request.form.get("name", ""), old.get("id", "")),
        "channel": channel,
        "enabled": request.form.get("enabled", "1") == "1",
        "config": config,
        "created_at": old.get("created_at") or now_str(),
        "updated_at": now_str(),
    }


def _fls_rewrite_notify_channel_options(selected):
    html_options = ""
    for key, meta in _fls_rewrite_notify_channels().items():
        s = "selected" if key == selected else ""
        html_options += f'<option value="{h(key)}" {s}>{h(meta.get("name", key))}</option>'
    return html_options


def _fls_rewrite_notify_item_form(item=None):
    channels = _fls_rewrite_notify_channels()

    if item is None:
        default_channel = request.args.get("channel", "").strip()
        if default_channel not in channels:
            default_channel = next(iter(channels.keys()))

        item = {
            "id": "",
            "name": "",
            "channel": default_channel,
            "enabled": True,
            "config": {},
        }
    else:
        item = dict(item)
        url_channel = request.args.get("channel", "").strip()
        if url_channel in channels:
            item["channel"] = url_channel

    channel = item.get("channel")
    if channel not in channels:
        channel = next(iter(channels.keys()))
        item["channel"] = channel

    meta = channels[channel]
    config = item.get("config", {}) or {}
    checked = "checked" if item.get("enabled", True) else ""

    quick_links = ""
    for key, m in channels.items():
        if item.get("id"):
            url = f"/notify/edit/{item.get('id')}?channel={_fls_rewrite_quote(key)}"
        else:
            url = f"/notify/new?channel={_fls_rewrite_quote(key)}"

        cls = "btn-primary" if key == channel else "btn-gray"
        quick_links += f'<a class="btn {cls}" href="{h(url)}">{h(m.get("name", key))}</a>'

    fields_html = ""
    fields = meta.get("fields", [])

    if fields:
        for field, label, placeholder in fields:
            fields_html += f"""
<div class="form-item">
    <label>{h(label)}</label>
    <input name="{h(field)}" value="{h(config.get(field, ""))}" placeholder="{h(placeholder)}">
</div>
"""
    else:
        fields_html = '<div class="help">该渠道无需额外配置。</div>'

    title = "编辑通知" if item.get("id") else "新增通知"

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">{h(title)}</div>
    <div class="help">
        通知名称留空时会自动使用渠道名；若重名会自动追加序号。
    </div>
</div>

<div class="card">
    <div class="card-title">选择通知渠道</div>
    <div class="action-row">{quick_links}</div>
    <div class="help">点击上方渠道可切换并查看对应配置项。</div>
</div>

<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>通知名称</label>
            <input name="name" value="{h(item.get("name", ""))}" placeholder="例如：微信机器人">
        </div>
        <div class="form-item">
            <label>通知渠道</label>
            <select name="channel">{_fls_rewrite_notify_channel_options(channel)}</select>
            <div class="help">保存时以这里选择的渠道为准。</div>
        </div>
    </div>
    <br>
    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用此通知
    </label>
</div>

<div class="card">
    <div class="card-title">{h(meta.get("name", channel))} 配置</div>
    <div class="form-grid">{fields_html}</div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit" name="action" value="save">保存</button>
    <button class="btn btn-orange" type="submit" name="action" value="test">保存并测试</button>
    <a class="btn btn-gray" href="/notify">返回通知管理</a>
</div>
</form>
"""
    return layout("通知配置", "notify", body)


def _fls_rewrite_notify_select_options(selected=None, include_default=True, include_none=True):
    selected = selected or ["__none__"]
    if isinstance(selected, str):
        selected = [selected]

    enabled_items = _fls_rewrite_enabled_notify_items()
    enabled_ids = {
        x.get("id")
        for x in enabled_items
    }

    selected = [
        x for x in selected
        if x in ("__default__", "__none__") or x in enabled_ids
    ]

    options = ""

    if include_default:
        s = "selected" if "__default__" in selected else ""
        options += f'<option value="__default__" {s}>使用全局默认通知</option>'

    if include_none:
        s = "selected" if "__none__" in selected or not selected else ""
        options += f'<option value="__none__" {s}>不通知</option>'

    for item in enabled_items:
        s = "selected" if item.get("id") in selected else ""
        cname = _fls_rewrite_channel_name(item.get("channel"))
        options += f'<option value="{h(item.get("id"))}" {s}>{h(item.get("name"))} [{h(cname)}]</option>'

    return options

def _fls_rewrite_normalize_notify_ids(ids):
    if isinstance(ids, str):
        ids = [ids]

    ids = [x for x in (ids or []) if x]

    # 三种模式互斥：
    # 1. 使用全局默认通知
    # 2. 不通知
    # 3. 其他渠道
    if "__none__" in ids:
        return ["__none__"]

    enabled_ids = {
        x.get("id")
        for x in _fls_rewrite_enabled_notify_items()
    }

    real = []
    for item_id in ids:
        if item_id in enabled_ids and item_id not in real:
            real.append(item_id)

    if real:
        return real

    if "__default__" in ids:
        return ["__default__"]

    return ["__none__"]


def _fls_rewrite_parse_notify_ids_from_form():
    mode = request.form.get("notify_mode", "__none__").strip()

    if mode == "__default__":
        return ["__default__"]

    if mode == "custom":
        selected = [
            x for x in request.form.getlist("notify_ids_custom")
            if x
        ]

        enabled_ids = {
            x.get("id")
            for x in _fls_rewrite_enabled_notify_items()
        }

        real = []
        for item_id in selected:
            if item_id in enabled_ids and item_id not in real:
                real.append(item_id)

        # 选择“其他渠道”但下面一个渠道都没选，自动回到“不通知”。
        if not real:
            return ["__none__"]

        return real

    return ["__none__"]

def _fls_rewrite_send_item(item, title, content):
    if not item:
        return False, "通知不存在"

    if not item.get("enabled", True):
        return False, "通知已禁用"

    channel = item.get("channel")
    if channel not in _fls_rewrite_notify_channels():
        return False, "未知或已移除的通知渠道"

    config = item.get("config", {}) or {}
    old_get_config = globals().get("_fls_notify_get_config")
    lock = globals().setdefault("_fls_rewrite_notify_send_lock", _fls_rewrite_threading.Lock())

    def fake_config():
        cfg = _fls_notify_default_config()
        cfg["enabled"] = True
        cfg["default_channels"] = [channel]
        if channel not in cfg["channels"]:
            cfg["channels"][channel] = {
                "enabled": True,
                "config": config,
            }
        cfg["channels"][channel]["enabled"] = True
        cfg["channels"][channel]["config"] = config
        return cfg

    with lock:
        try:
            globals()["_fls_notify_get_config"] = fake_config
            return _fls_notify_send_one(channel, title, content)
        finally:
            if old_get_config:
                globals()["_fls_notify_get_config"] = old_get_config


def _fls_rewrite_title_for_chunk(title, idx, total):
    """
    分片标题修正：
    - 任务通知标题为空时，分片也保持空标题；
    - 非空标题才追加分片标记。
    """
    if not str(title or "").strip():
        return ""

    if total <= 1:
        return title

    m = re.match(r"任务结束：(.*?)，退出码：.*", str(title or ""))
    if m:
        return f"任务：{m.group(1)} [{idx}/{total}]"

    return f"{title} [{idx}/{total}]"

def _fls_notify_channel_needs_title(channel):
    """
    判断通知渠道是否需要标题。

    这些渠道有独立 title / subject / summary / text 字段，
    如果 title 为空，通知列表体验较差，甚至部分渠道可能显示异常。
    对这些渠道，任务通知会使用任务名作为标题。
    """
    return str(channel or "").strip() in {
        "bark",
        "serverj",
        "pushplus",
        "smtp",
        "ntfy",
        "wxpusher",
        "gotify",
        "igot",
        "pushdeer",
        "weplus",
        "qywxapp",
    }

def _fls_rewrite_send_by_ids(title, content, ids=None):
    if isinstance(ids, str):
        ids = [ids]

    ids = ids or ["__default__"]

    if "__none__" in ids:
        return []

    enabled_ids = {
        x.get("id")
        for x in _fls_rewrite_enabled_notify_items()
    }

    real_ids = []
    for item_id in ids:
        if item_id in enabled_ids and item_id not in real_ids:
            real_ids.append(item_id)

    # 只有纯默认时才使用全局默认通知。
    # 如果任务同时出现默认和具体通知，则忽略默认，避免重复通知。
    if not real_ids and "__default__" in ids:
        for item_id in _fls_rewrite_notify_default_ids():
            if item_id in enabled_ids and item_id not in real_ids:
                real_ids.append(item_id)

    if not real_ids:
        return []

    chunks = _fls_notify_split_content(content, 2000)
    results = []

    for idx, chunk in enumerate(chunks, 1):
        part_title = _fls_rewrite_title_for_chunk(title, idx, len(chunks))

        for item_id in real_ids:
            item = _fls_rewrite_get_notify_item(item_id)
            if not item or not item.get("enabled", True):
                results.append({
                    "id": item_id,
                    "name": item_id,
                    "ok": False,
                    "msg": "通知不存在或已禁用",
                })
                continue

            send_title = part_title if _fls_notify_channel_needs_title(item.get("channel", "")) else ""
            ok, msg = _fls_rewrite_send_item(item, send_title, chunk)
            results.append({
                "id": item_id,
                "name": item.get("name", item_id),
                "ok": ok,
                "msg": msg,
            })

            line = f"[NotifyRewrite] {item.get('name')} {'发送成功' if ok else '发送失败'}：{msg}"
            print(line)
            try:
                _fls_fix_write_daemon_log(line)
            except Exception:
                pass

    return results


def _fls_rewrite_send_all_enabled(title, content):
    ids = [
        x.get("id")
        for x in _fls_rewrite_enabled_notify_items()
    ]
    return _fls_rewrite_send_by_ids(title, content, ids)


def _fls_rewrite_notify_page():
    items = _fls_rewrite_notify_items()
    defaults = set(_fls_rewrite_notify_default_ids())

    rows = ""

    if not items:
        rows = '<tr><td colspan="6">暂无通知，请点击新增通知</td></tr>'
    else:
        for item in items:
            item_id = item.get("id")
            cname = _fls_rewrite_channel_name(item.get("channel"))
            enabled_badge = '<span class="badge green">启用</span>' if item.get("enabled", True) else '<span class="badge gray">禁用</span>'
            default_badge = '<span class="badge blue">全局默认</span>' if item_id in defaults else '<span class="badge gray">-</span>'
            toggle_text = "禁用" if item.get("enabled", True) else "启用"
            toggle_class = "btn-gray" if item.get("enabled", True) else "btn-primary"

            rows += f"""
<tr>
    <td><b>{h(item.get("name", ""))}</b></td>
    <td>{h(cname)}</td>
    <td>{enabled_badge}</td>
    <td>{default_badge}</td>
    <td>{h(item.get("updated_at", "-"))}</td>
    <td>
        <a class="btn btn-orange" href="/notify/test/{h(item_id)}">测试</a>
        <a class="btn btn-blue" href="/notify/edit/{h(item_id)}">编辑</a>
        <a class="btn {toggle_class}" href="/notify/toggle/{h(item_id)}">{toggle_text}</a>
        <a class="btn btn-red" href="/notify/delete/{h(item_id)}" onclick="return confirm('确定删除该通知吗？')">删除</a>
    </td>
</tr>
"""

    default_options = _fls_rewrite_notify_select_options(
        list(defaults),
        include_default=False,
        include_none=False,
    )

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">通知管理</div>
            <div class="help">
                可新增多个通知实例。任务里选择“使用全局默认通知”时，会使用下面保存的全局默认通知。
            </div>
        </div>
        <a class="btn btn-primary" href="/notify/new">新增通知</a>
    </div>
</div>

<form method="post" action="/notify/default">
<div class="card">
    <div class="card-title">全局默认通知</div>
    <select name="default_ids" multiple size="6">{default_options}</select>
    <div class="help">
        这里设置全局默认通知。任务选择“使用全局默认通知”时会使用这里的配置。<br>
        登录通知会发送到所有已启用通知。
    </div>
    <br>
    <button class="btn btn-primary" type="submit">保存全局默认通知</button>
</div>
</form>

<div class="card">
    <div class="card-title">通知列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>渠道</th>
                    <th>状态</th>
                    <th>全局默认</th>
                    <th>更新时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>
"""
    return layout("通知管理", "notify", body)

def _fls_rewrite_notify_default_save():
    _fls_rewrite_save_notify_default_ids(request.form.getlist("default_ids"))
    return redirect(url_for("_fls_notify_page_new") if "_fls_notify_page_new" in app.view_functions else "/notify")


def _fls_rewrite_notify_new():
    if request.method == "POST":
        item = _fls_rewrite_notify_item_from_form()
        items = _fls_rewrite_notify_items()
        items.append(item)
        _fls_rewrite_save_notify_items(items)

        if request.form.get("action") == "test":
            _fls_rewrite_send_item(item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

        return redirect("/notify")

    return _fls_rewrite_notify_item_form()


def _fls_rewrite_notify_edit(item_id):
    items = _fls_rewrite_notify_items()
    item = None

    for one in items:
        if one.get("id") == item_id:
            item = one
            break

    if not item:
        abort(404)

    if request.method == "POST":
        new_item = _fls_rewrite_notify_item_from_form(item)

        for idx, one in enumerate(items):
            if one.get("id") == item_id:
                items[idx] = new_item
                break

        _fls_rewrite_save_notify_items(items)

        if request.form.get("action") == "test":
            _fls_rewrite_send_item(new_item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

        return redirect("/notify")

    return _fls_rewrite_notify_item_form(item)


def _fls_rewrite_notify_toggle(item_id):
    items = _fls_rewrite_notify_items()

    for item in items:
        if item.get("id") == item_id:
            item["enabled"] = not item.get("enabled", True)
            item["updated_at"] = now_str()
            break

    _fls_rewrite_save_notify_items(items)
    _fls_rewrite_save_notify_default_ids(_fls_rewrite_notify_default_ids())
    return redirect("/notify")


def _fls_rewrite_notify_delete(item_id):
    items = [
        x for x in _fls_rewrite_notify_items()
        if x.get("id") != item_id
    ]
    _fls_rewrite_save_notify_items(items)
    _fls_rewrite_save_notify_default_ids(_fls_rewrite_notify_default_ids())

    tasks = load_tasks()
    changed = False

    for task in tasks:
        notify_ids = task.get("notify_ids")
        if isinstance(notify_ids, list) and item_id in notify_ids:
            task["notify_ids"] = [x for x in notify_ids if x != item_id]
            changed = True

    if changed:
        save_tasks(tasks)

    return redirect("/notify")


def _fls_rewrite_notify_test(item_id):
    item = _fls_rewrite_get_notify_item(item_id)

    if not item:
        abort(404)

    ok, msg = _fls_rewrite_send_item(
        item,
        "FLS 通知测试",
        f"这是一条测试通知。\n时间：{now_str()}",
    )

    body = f"""
<div class="card">
    <div class="card-title">通知测试结果</div>
    <div class="help">
        通知：<b>{h(item.get("name"))}</b><br>
        渠道：<b>{h(_fls_rewrite_channel_name(item.get("channel")))}</b><br>
        结果：<b>{"成功" if ok else "失败"}</b><br>
        返回：{h(msg)}
    </div>
    <br>
    <a class="btn btn-gray" href="/notify">返回通知管理</a>
</div>
"""
    return layout("通知测试", "notify", body)


# 覆盖通知相关 endpoint。
try:
    app.view_functions["_fls_notify_config_page"] = _fls_rewrite_notify_page
except Exception:
    pass

try:
    app.view_functions["_fls_notify_page_new"] = _fls_rewrite_notify_page
except Exception:
    pass

try:
    app.view_functions["_fls_notify_default_save"] = _fls_rewrite_notify_default_save
except Exception:
    pass

try:
    app.view_functions["_fls_notify_new_item"] = _fls_rewrite_notify_new
except Exception:
    pass

try:
    app.view_functions["_fls_notify_edit_item"] = _fls_rewrite_notify_edit
except Exception:
    pass

try:
    app.view_functions["_fls_notify_toggle_item"] = _fls_rewrite_notify_toggle
except Exception:
    pass

try:
    app.view_functions["_fls_notify_delete_item"] = _fls_rewrite_notify_delete
except Exception:
    pass

try:
    app.view_functions["_fls_notify_test_item"] = _fls_rewrite_notify_test
except Exception:
    pass


# ============================================================
# 通知发送函数兼容覆盖
# ============================================================
def fls_notify_send(title, content, channels=None):
    """
    兼容旧调用：
    - channels 为 None 时发送默认通知；
    - 不再支持 console；
    - 具体发送走通知实例。
    """
    return _fls_rewrite_send_by_ids(title, content, channels)
def _fls_notify_select_options_items(selected=None, include_default=True, include_none=True):
    return _fls_rewrite_notify_select_options(selected, include_default, include_none)


# ============================================================
# 代理禁用启用：列表直接操作，任务编辑只显示启用代理
# ============================================================
try:
    _fls_rewrite_raw_load_proxies = _fls_patch_old_load_proxies
except Exception:
    _fls_rewrite_raw_load_proxies = load_proxies


def load_proxies():
    proxies = _fls_rewrite_raw_load_proxies()
    changed = False

    for proxy in proxies:
        if "enabled" not in proxy:
            proxy["enabled"] = True
            changed = True

    if changed:
        save_proxies(proxies)

    return proxies


def get_proxy(proxy_id):
    if not proxy_id:
        return None

    for proxy in load_proxies():
        if proxy.get("id") == proxy_id:
            if proxy.get("enabled", True) is False:
                return None
            return proxy

    return None


def proxy_select_options(selected_id=""):
    proxies = load_proxies()

    enabled_ids = {
        p.get("id")
        for p in proxies
        if p.get("enabled", True)
    }

    if selected_id not in enabled_ids:
        selected_id = ""

    options = '<option value="">不使用代理</option>'

    for proxy in proxies:
        if not proxy.get("enabled", True):
            continue

        selected = "selected" if proxy.get("id") == selected_id else ""
        name = proxy.get("name") or proxy.get("type")
        ptype = proxy.get("type", "")
        options += f'<option value="{h(proxy.get("id"))}" {selected}>{h(name)} [{h(ptype)}]</option>'

    return options


def proxy_from_form():
    return {
        "id": request.form.get("id", "").strip(),
        "name": request.form.get("name", "").strip() or "未命名代理",
        "type": request.form.get("type", "http").strip(),
        "host": request.form.get("host", "").strip(),
        "port": request.form.get("port", "").strip(),
        "username": request.form.get("username", "").strip(),
        "password": request.form.get("password", "").strip(),
        "url": request.form.get("url", "").strip(),
        "enabled": request.form.get("enabled", "1") == "1",
    }


def _fls_rewrite_proxy_page():
    proxies = load_proxies()
    rows = ""

    if not proxies:
        rows = '<tr><td colspan="6">暂无代理，请点击新增代理</td></tr>'
    else:
        for proxy in proxies:
            proxy_id = proxy.get("id")
            ptype = proxy.get("type", "")
            addr = proxy.get("url", "") if ptype == "github" else f'{proxy.get("host", "")}:{proxy.get("port", "")}'
            badge = '<span class="badge green">启用</span>' if proxy.get("enabled", True) else '<span class="badge gray">禁用</span>'
            toggle_text = "禁用" if proxy.get("enabled", True) else "启用"
            toggle_class = "btn-gray" if proxy.get("enabled", True) else "btn-primary"

            rows += f"""
<tr>
    <td>{h(proxy.get("name", ""))}</td>
    <td>{h(ptype)}</td>
    <td>{h(addr)}</td>
    <td>{badge}</td>
    <td>{h(proxy.get("created_at", "-"))}</td>
    <td>
        <a class="btn btn-blue" href="/proxy/edit/{h(proxy_id)}">编辑</a>
        <button class="btn btn-orange" type="button" onclick="flsProxyTest('{h(proxy_id)}')">测试</button>
        <button class="btn btn-primary" type="button" onclick="flsProxyQuality('{h(proxy_id)}')">质量检测</button>
        <a class="btn {toggle_class}" href="/proxy/toggle/{h(proxy_id)}">{toggle_text}</a>
        <a class="btn btn-red" href="/proxy/delete/{h(proxy_id)}" onclick="return confirm('确定删除代理吗？')">删除</a>
    </td>
</tr>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">代理管理</div>
            <div class="help">
                禁用代理后，任务编辑页不再显示；已选择该代理的任务运行时会自动跳过该代理。<br>
                代理测试和质量检测结果会直接显示在当前页面。
            </div>
        </div>
        <a class="btn btn-primary" href="/proxy/new">新增代理</a>
    </div>
</div>

<div class="card">
    <div class="card-title">代理列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>地址</th>
                    <th>状态</th>
                    <th>创建时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>

<div class="card" id="flsProxyResultCard" style="display:none;">
    <div class="card-title">代理检测结果</div>
    <div class="help" id="flsProxyResultText">等待操作</div>
</div>

<script>
function flsProxyEscapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{
            "&":"&amp;",
            "<":"&lt;",
            ">":"&gt;",
            '"':"&quot;",
            "'":"&#39;"
        }}[c];
    }});
}}

function flsProxyShowResult(html){{
    var card = document.getElementById("flsProxyResultCard");
    var text = document.getElementById("flsProxyResultText");
    if(!card || !text) return;

    card.style.display = "block";
    text.innerHTML = html;

    try {{
        card.scrollIntoView({{behavior:"smooth", block:"nearest"}});
    }} catch(e) {{}}
}}

async function flsProxyTest(proxyId){{
    flsProxyShowResult("正在测试代理，请稍候...");

    try {{
        var res = await fetch("/api/proxy/test/" + encodeURIComponent(proxyId), {{
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        var json = await res.json();

        if(json.ok){{
            flsProxyShowResult(
                "代理：<b>" + flsProxyEscapeHtml(json.name || "") + "</b><br>" +
                "状态：<b style='color:#18a058'>成功</b><br>" +
                "状态码：" + flsProxyEscapeHtml(String(json.status_code)) + "<br>" +
                "耗时：" + flsProxyEscapeHtml(String(json.elapsed_ms)) + " ms"
            );
        }} else {{
            flsProxyShowResult(
                "代理：<b>" + flsProxyEscapeHtml(json.name || "") + "</b><br>" +
                "状态：<b style='color:#dc2626'>失败</b><br>" +
                "错误：" + flsProxyEscapeHtml(json.error || json.msg || "未知错误")
            );
        }}
    }} catch(e) {{
        flsProxyShowResult("请求失败：" + flsProxyEscapeHtml(String(e)));
    }}
}}

async function flsProxyQuality(proxyId){{
    flsProxyShowResult("正在进行质量检测，请稍候...");

    try {{
        var res = await fetch("/api/proxy/quality/" + encodeURIComponent(proxyId), {{
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        var json = await res.json();

        if(!json.ok){{
            flsProxyShowResult(
                "代理：<b>" + flsProxyEscapeHtml(json.name || "") + "</b><br>" +
                "检测失败：" + flsProxyEscapeHtml(json.error || json.msg || "未知错误")
            );
            return;
        }}

        var html = "代理：<b>" + flsProxyEscapeHtml(json.name || "") + "</b><br><br>";
        html += "<div class='table-wrap'><table><thead><tr>" +
            "<th>测试地址</th><th>结果</th><th>状态码</th><th>耗时 / 错误</th>" +
            "</tr></thead><tbody>";

        for(var i = 0; i < json.items.length; i++){{
            var item = json.items[i];
            html += "<tr>" +
                "<td>" + flsProxyEscapeHtml(item.url) + "</td>" +
                "<td>" + (item.ok ? "<span class='badge green'>成功</span>" : "<span class='badge red'>失败</span>") + "</td>" +
                "<td>" + flsProxyEscapeHtml(String(item.status_code)) + "</td>" +
                "<td>" + flsProxyEscapeHtml(String(item.elapsed)) + "</td>" +
                "</tr>";
        }}

        html += "</tbody></table></div>";
        flsProxyShowResult(html);
    }} catch(e) {{
        flsProxyShowResult("请求失败：" + flsProxyEscapeHtml(String(e)));
    }}
}}
</script>
"""
    return layout("代理管理", "proxy", body)

def _fls_rewrite_proxy_toggle(proxy_id):
    proxies = load_proxies()

    for proxy in proxies:
        if proxy.get("id") == proxy_id:
            proxy["enabled"] = not proxy.get("enabled", True)
            proxy["updated_at"] = now_str()
            break

    save_proxies(proxies)
    return redirect("/proxy")


try:
    app.view_functions["proxy_page"] = _fls_rewrite_proxy_page
except Exception:
    pass

try:
    app.view_functions["_fls_proxy_toggle"] = _fls_rewrite_proxy_toggle
except Exception:
    pass


try:
    _fls_rewrite_old_proxy_form = _fls_base_proxy_form
except Exception:
    _fls_rewrite_old_proxy_form = None


def proxy_form(proxy=None, mode="new"):
    if _fls_rewrite_old_proxy_form:
        html_text = _fls_rewrite_old_proxy_form(proxy, mode)
    else:
        html_text = ""

    enabled = True if proxy is None else proxy.get("enabled", True)
    checked = "checked" if enabled else ""

    insert = f"""
<br>
<label>
    <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
    启用此代理
</label>
"""

    if 'name="enabled"' in html_text:
        return html_text

    marker = '<div class="form-item" id="githubProxyBox"'
    pos = html_text.find(marker)

    if pos >= 0:
        return html_text[:pos] + insert + "\n" + html_text[pos:]

    return html_text + insert


# ============================================================
# 任务表单：通知只显示启用项；代理只显示启用项
# ============================================================


def fls_parse_task_env_from_form():
    """
    解析任务变量表格表单。
    新格式：
    - env_table_mode=1
    - env_key 多个
    - env_value 多个

    兼容旧格式：
    - env_text
    """
    if request.form.get("env_table_mode") == "1":
        keys = request.form.getlist("env_key")
        values = request.form.getlist("env_value")

        env = {}
        for idx, key in enumerate(keys):
            key = str(key or "").strip()
            if not key:
                continue

            value = values[idx] if idx < len(values) else ""
            env[key] = value

        return env

    return parse_env_text(request.form.get("env_text", ""))


def fls_task_env_table_rows(env):
    """
    任务变量表格行。
    值超过 50 字符时默认收起，展开后编辑完整值。
    """
    env = env or {}

    if not env:
        return ""

    rows = ""

    for key in sorted(env.keys()):
        value = str(env.get(key, ""))
        value_textarea = f'<textarea name="env_value" style="min-height:72px;">{h(value)}</textarea>'

        if len(value) > 50:
            short = value[:50] + "..."
            value_cell = (
                '<details>'
                f'<summary style="cursor:pointer;"><code>{h(short)}</code></summary>'
                '<div style="margin-top:8px;">'
                f'{value_textarea}'
                '</div>'
                '</details>'
            )
        else:
            value_cell = value_textarea

        rows += f"""
<tr>
    <td>
        <input name="env_key" value="{h(key)}" placeholder="变量名">
    </td>
    <td>
        {value_cell}
    </td>
    <td>
        <button class="btn btn-red" type="button" onclick="flsTaskEnvRemoveRow(this)">删除</button>
    </td>
</tr>
"""

    return rows

def _fls_rewrite_task_form(task=None):
    if task is None:
        task = {
            "id": "",
            "name": "",
            "command": "task ",
            "cron": "",
            "enabled": True,
            "proxy_id": "",
            "env": {},
            "notify_ids": ["__none__"],
        }

    if "notify_ids" not in task:
        task["notify_ids"] = task.get("notify_channels") or ["__none__"]

    notify_ids = _fls_rewrite_normalize_notify_ids(task.get("notify_ids"))

    if notify_ids == ["__default__"]:
        notify_mode = "__default__"
        custom_selected = []
    elif notify_ids == ["__none__"] or "__none__" in notify_ids:
        notify_mode = "__none__"
        custom_selected = []
    else:
        notify_mode = "custom"
        custom_selected = notify_ids

    checked = "checked" if task.get("enabled", True) else ""
    proxy_options = proxy_select_options(task.get("proxy_id", ""))

    enabled_items = _fls_rewrite_enabled_notify_items()
    custom_options = ""

    if enabled_items:
        for item in enabled_items:
            selected = "selected" if item.get("id") in custom_selected else ""
            cname = _fls_rewrite_channel_name(item.get("channel"))
            custom_options += f'<option value="{h(item.get("id"))}" {selected}>{h(item.get("name"))} [{h(cname)}]</option>'
    else:
        custom_options = '<option value="" disabled>暂无已启用通知渠道</option>'

    mode_default_checked = "checked" if notify_mode == "__default__" else ""
    mode_none_checked = "checked" if notify_mode == "__none__" else ""
    mode_custom_checked = "checked" if notify_mode == "custom" else ""

    env_rows = fls_task_env_table_rows(task.get("env", {}) or {})
    env_empty_row_style = "" if not env_rows else "display:none;"

    body = f"""
<form method="post">
<input type="hidden" name="env_table_mode" value="1">

<div class="card">
    <div class="card-title">任务信息</div>
    <div class="form-grid">
        <div class="form-item">
            <label>任务名，必填</label>
            <input name="name" required value="{h(task.get("name", ""))}" placeholder="例如：中国联通">
        </div>
        <div class="form-item">
            <label>Cron 表达式</label>
            <input name="cron" value="{h(task.get("cron", ""))}" placeholder="例如：0 8 * * *">
            <div class="help">留空表示手动运行任务。支持 5 位或 6 位 Cron。</div>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>命令，必填</label>
        <input name="command" required value="{h(task.get("command", ""))}" placeholder="task 1.py">
    </div>

    <br>

    <div class="form-item">
        <label>代理</label>
        <select name="proxy_id">{proxy_options}</select>
        <div class="help">只显示已启用代理。若任务之前选中过已禁用代理，运行时会自动跳过该代理。</div>
    </div>

    <br>

    <div class="form-item">
        <label>任务结束通知</label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="notify_mode" value="__default__" {mode_default_checked} style="width:auto;" onchange="flsToggleNotifyCustomBox()">
            使用全局默认通知
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="notify_mode" value="__none__" {mode_none_checked} style="width:auto;" onchange="flsToggleNotifyCustomBox()">
            不通知
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="notify_mode" value="custom" {mode_custom_checked} style="width:auto;" onchange="flsToggleNotifyCustomBox()">
            其他渠道
        </label>

        <div id="flsNotifyCustomBox" style="margin-top:10px;display:none;">
            <select name="notify_ids_custom" multiple size="8">{custom_options}</select>
            <div class="help">
                这里可多选具体通知渠道。<br>
                如果选择“其他渠道”但未勾选任何通知，保存后会自动按“不通知”处理。
            </div>
        </div>

        <div class="help">
            三种通知方式只能选择一种；新建任务默认不通知。
        </div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用任务
    </label>
</div>

<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务变量</div>
            <div class="help">
                任务变量仅对此任务生效，会覆盖同名全局变量。<br>
                变量值超过 50 个字符会默认收起，点击可展开编辑完整值。
            </div>
        </div>
        <button class="btn btn-primary" type="button" onclick="flsTaskEnvAddRow()">新增任务变量</button>
    </div>

    <br>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>变量名</th>
                    <th>值</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody id="flsTaskEnvTbody">
                {env_rows}
                <tr id="flsTaskEnvEmptyRow" style="{env_empty_row_style}">
                    <td colspan="3">暂无任务变量，请点击“新增任务变量”</td>
                </tr>
            </tbody>
        </table>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/tasks">返回</a>
</div>
</form>

<script>
function flsToggleNotifyCustomBox(){{
    var box = document.getElementById("flsNotifyCustomBox");
    var checked = document.querySelector('input[name="notify_mode"]:checked');
    if(!box || !checked) return;
    box.style.display = checked.value === "custom" ? "block" : "none";
}}

function flsTaskEnvEscapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{
            "&":"&amp;",
            "<":"&lt;",
            ">":"&gt;",
            '"':"&quot;",
            "'":"&#39;"
        }}[c];
    }});
}}

function flsTaskEnvRefreshEmptyRow(){{
    var tbody = document.getElementById("flsTaskEnvTbody");
    var empty = document.getElementById("flsTaskEnvEmptyRow");
    if(!tbody || !empty) return;

    var count = 0;
    var rows = tbody.querySelectorAll("tr");
    rows.forEach(function(row){{
        if(row.id !== "flsTaskEnvEmptyRow") count++;
    }});

    empty.style.display = count > 0 ? "none" : "";
}}

function flsTaskEnvAddRow(key, value){{
    var tbody = document.getElementById("flsTaskEnvTbody");
    if(!tbody) return;

    key = key || "";
    value = value || "";

    var tr = document.createElement("tr");
    tr.innerHTML =
        '<td><input name="env_key" value="' + flsTaskEnvEscapeHtml(key) + '" placeholder="变量名"></td>' +
        '<td><textarea name="env_value" style="min-height:72px;" placeholder="变量值">' + flsTaskEnvEscapeHtml(value) + '</textarea></td>' +
        '<td><button class="btn btn-red" type="button" onclick="flsTaskEnvRemoveRow(this)">删除</button></td>';

    tbody.appendChild(tr);
    flsTaskEnvRefreshEmptyRow();
}}

function flsTaskEnvRemoveRow(btn){{
    var tr = btn ? btn.closest("tr") : null;
    if(tr) tr.remove();
    flsTaskEnvRefreshEmptyRow();
}}

flsToggleNotifyCustomBox();
flsTaskEnvRefreshEmptyRow();
</script>
"""
    return body


def _fls_rewrite_task_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        command = request.form.get("command", "").strip()
        cron_expr = request.form.get("cron", "").strip()

        if not name:
            return "任务名不能为空", 400

        if not command:
            return "命令不能为空", 400

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        task = {
            "id": uuid.uuid4().hex,
            "name": name,
            "command": command,
            "cron": cron_expr,
            "enabled": request.form.get("enabled") == "1",
            "proxy_id": request.form.get("proxy_id", "").strip(),
            "env": fls_parse_task_env_from_form(),
            "notify_ids": _fls_rewrite_parse_notify_ids_from_form(),
            "run_count": 0,
            "created_at": now_str(),
            "updated_at": now_str(),
        }

        tasks = load_tasks()
        tasks.append(task)
        save_tasks(tasks)
        reload_scheduler()
        return redirect_to("tasks_page")

    return layout("新建任务", "tasks", _fls_rewrite_task_form())


def _fls_rewrite_task_edit(task_id):
    tasks = load_tasks()
    task = None

    for one in tasks:
        if one.get("id") == task_id:
            task = one
            break

    if not task:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        command = request.form.get("command", "").strip()
        cron_expr = request.form.get("cron", "").strip()

        if not name:
            return "任务名不能为空", 400

        if not command:
            return "命令不能为空", 400

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        task["name"] = name
        task["command"] = command
        task["cron"] = cron_expr
        task["enabled"] = request.form.get("enabled") == "1"
        task["proxy_id"] = request.form.get("proxy_id", "").strip()
        task["env"] = fls_parse_task_env_from_form()
        task["notify_ids"] = _fls_rewrite_parse_notify_ids_from_form()
        task["updated_at"] = now_str()
        task.setdefault("run_count", 0)

        save_tasks(tasks)
        reload_scheduler()
        return redirect_to("tasks_page")

    task["notify_ids"] = _fls_rewrite_normalize_notify_ids(
        task.get("notify_ids") or task.get("notify_channels") or ["__default__"]
    )

    return layout("编辑任务", "tasks", _fls_rewrite_task_form(task))


try:
    app.view_functions["task_new"] = _fls_rewrite_task_new
    app.view_functions["task_edit"] = _fls_rewrite_task_edit
except Exception as e:
    print(f"[NotifyRewrite] 覆盖任务编辑失败: {e}")


# ============================================================
# 任务结束通知：使用新通知实例并修正分片标题
# ============================================================
def _fls_rewrite_append_task_log(log_file, text):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))
            if not str(text).endswith("\n"):
                f.write(b"\n")
    except Exception as e:
        print(f"[NotifyRewrite] 写任务日志失败: {e}")


def _fls_notify_extract_user_log_content(log_text):
    """
    提取通知用日志内容。

    通知里只发送脚本自身输出，不发送 FLS 管理器写入的任务启动头、
    实际启动命令、任务结束行、通知结果等面板辅助日志。
    """
    text = str(log_text or "")
    lines = text.splitlines()

    if not lines:
        return ""

    # 1. 去掉 FLS 启动头。
    # 启动头以：
    # ===== 启动任务: xxx =====
    # ...
    # ============================================================
    # 结束。通知正文从这条分隔线之后开始。
    start_idx = 0
    if lines and lines[0].startswith("===== 启动任务:"):
        for idx, line in enumerate(lines):
            if line.strip().startswith("====") and idx > 0:
                start_idx = idx + 1
                break

    lines = lines[start_idx:]

    # 2. 去掉 FLS 写入的任务结束行及其后面的通知结果等内容。
    end_idx = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("===== 任务已结束:"):
            end_idx = idx
            break

        if stripped.startswith("===== 通知结果"):
            end_idx = idx
            break

    lines = lines[:end_idx]

    # 3. 去掉首尾空行。
    while lines and not lines[0].strip():
        lines.pop(0)

    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def _fls_system_failure_notify(action, message, log_file="", extra=""):
    """
    系统功能失败通知：
    - 依赖安装失败
    - 备份恢复失败
    - 运行环境安装失败

    发送到所有已启用通知渠道。
    """
    try:
        action = str(action or "").strip() or "系统功能"
        message = str(message or "").strip() or "未知错误"
        log_file = str(log_file or "").strip()
        extra = str(extra or "").strip()

        lines = [
            f"时间：{now_str()}",
            f"功能：{action}",
            f"结果：失败",
            f"原因：{message}",
        ]

        if log_file:
            lines.append(f"日志文件：{log_file}")

        if extra:
            lines.append("")
            lines.append(extra)

        title = f"FLS 系统功能失败：{action}"
        content = "\n".join(lines)

        if "_fls_rewrite_send_all_enabled" in globals():
            _fls_rewrite_send_all_enabled(title, content)
        elif "fls_notify_send" in globals():
            fls_notify_send(title, content, None)
    except Exception as e:
        try:
            print(f"[SystemFailNotify] 发送失败: {e}")
        except Exception:
            pass

def _fls_rewrite_task_watcher(task_id, task_snapshot, proc, log_file, log_fp):
    try:
        return_code = proc.wait()

        try:
            if log_fp:
                log_fp.write(f"\n===== 任务已结束: {now_str()}，退出码: {return_code} =====\n".encode("utf-8"))
                log_fp.close()
        except Exception:
            pass

        try:
            RUNNING.pop(task_id, None)
        except Exception:
            pass

        task_name = task_snapshot.get("name") or task_snapshot.get("command") or task_id
        notify_ids = _fls_rewrite_normalize_notify_ids(
            task_snapshot.get("notify_ids") or task_snapshot.get("notify_channels") or ["__default__"]
        )

        if notify_ids == ["__none__"] or "__none__" in notify_ids:
            _fls_rewrite_append_task_log(log_file, "\n===== 通知结果 =====\n任务设置为不通知\n")
            return

        log_text = tail_file(str(log_file), 5000)
        # 通知标题使用任务名；正文仍只显示脚本输出内容。
        title = str(task_name or "").strip() or "FLS"

        # 通知正文只发送日志内容，不再附加任务名、命令、结束时间、退出码、日志文件等说明。
        content = _fls_notify_extract_user_log_content(log_text)

        results = _fls_rewrite_send_by_ids(title, content, notify_ids)

        lines = ["\n===== 通知结果，不包含在本次通知内容中 ====="]

        if not results:
            lines.append("未配置可用通知或全局默认通知为空")
        else:
            for result in results:
                lines.append(f"{result.get('name')}: {'成功' if result.get('ok') else '失败'} - {result.get('msg')}")

        lines.append("============================================================\n")
        _fls_rewrite_append_task_log(log_file, "\n".join(lines))

    except Exception as e:
        msg = f"[NotifyRewrite] 任务通知监听失败: {e}"
        print(msg)
        try:
            _fls_fix_write_daemon_log(msg)
        except Exception:
            pass


def run_task_now(task_id, source="manual"):
    """
    覆盖任务运行函数：
    使用最早保存的原始启动函数，避免多个旧 watcher 重复通知。
    """
    base_run = globals().get("_fls_old_run_task_now_for_notify")

    if not base_run:
        base_run = globals().get("_fls_patch_original_run_task_now")

    if not base_run:
        return False, "原始 run_task_now 不存在"

    ok, msg = base_run(task_id, source)

    if ok:
        try:
            task = get_task(task_id) or {}
            info = RUNNING.get(task_id) or {}
            proc = info.get("process")
            log_file = info.get("log_file")
            log_fp = info.get("log_fp")

            if proc and log_file:
                th = _fls_rewrite_threading.Thread(
                    target=_fls_rewrite_task_watcher,
                    args=(task_id, dict(task), proc, log_file, log_fp),
                    daemon=True,
                    name=f"fls-notify-rewrite-{task_id[:8]}",
                )
                th.start()
        except Exception as e:
            print(f"[NotifyRewrite] 启动任务通知监听失败: {e}")

    return ok, msg


# ============================================================
# 登录成功通知：发送给所有已启用通知
# ============================================================
def _fls_rewrite_login():
    token = fls_get_admin_token()

    if not token:
        return redirect(url_for("_fls_setup_token"))

    if _fls_auth_session_valid(token):
        return redirect(url_for("dashboard"))

    msg = ""

    if request.method == "POST":
        input_token = request.form.get("token", "").strip()
        remember = request.form.get("remember") == "1"

        if input_token == token:
            _fls_auth_set_session(token, remember=remember)

            try:
                ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
                ua = request.headers.get("User-Agent", "")
                _fls_rewrite_send_all_enabled(
                    "FLS 面板登录通知",
                    f"时间：{now_str()}\nIP：{ip}\nUser-Agent：{ua}",
                )
            except Exception as e:
                print(f"[NotifyRewrite] 登录通知失败: {e}")

            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)

        msg = "Token 错误"

    body = f"""
<div class="card" style="max-width:520px;margin:8vh auto;">
    <div class="card-title">登录 FLS 面板</div>
    <form method="post">
        <div class="form-item">
            <label>Token</label>
            <input name="token" type="password" placeholder="请输入登录 Token" autofocus>
        </div>
        <br>
        <label>
            <input type="checkbox" name="remember" value="1" style="width:auto;">
            保持登录 7 天
        </label>
        <div class="help">不勾选则登录有效期为 1 小时。</div>
        <br>
        <button class="btn btn-primary" type="submit">登录</button>
    </form>
    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
</div>
"""
    return layout("登录", "login", body)


try:
    app.view_functions["_fls_login"] = _fls_rewrite_login
except Exception as e:
    print(f"[NotifyRewrite] 覆盖登录页失败: {e}")


# ============================================================
# 导航文字微调
# ============================================================
try:
    _fls_rewrite_old_layout = layout

    def layout(title, active, body):
        html_text = _fls_rewrite_old_layout(title, active, body)
        html_text = html_text.replace("📁 日志中心", "📁 日志管理")
        return html_text

    globals()["layout"] = layout
except Exception as e:
    print(f"[NotifyRewrite] 导航修正失败: {e}")


# 初始化一次，清理 console 和无效默认通知。
try:
    _fls_rewrite_notify_items()
    _fls_rewrite_save_notify_default_ids(_fls_rewrite_notify_default_ids())
except Exception as e:
    print(f"[NotifyRewrite] 初始化失败: {e}")

try:
    _fls_no_refresh_old_layout_20260429 = layout

    def layout(title, active, body):
        html_text = _fls_no_refresh_old_layout_20260429(title, active, body)

        if "__FLS_NO_REFRESH_PATCH_20260429__" in html_text:
            return html_text

        patch_script = """
<script id="__FLS_NO_REFRESH_PATCH_20260429__">
(function(){
    if (window.__FLS_NO_REFRESH_PATCH_20260429__) return;
    window.__FLS_NO_REFRESH_PATCH_20260429__ = true;

    function sameOrigin(url){
        try {
            return new URL(url, location.href).origin === location.origin;
        } catch(e) {
            return false;
        }
    }

    function shouldSkipPath(path){
        if (path.indexOf("/scripts/download/") === 0) return true;
        if (path === "/backup/export") return true;
        return false;
    }

    function isJsonApiAllowed(path){
        if (path.indexOf("/api/proxy/test/") === 0) return true;
        if (path.indexOf("/api/proxy/quality/") === 0) return true;
        return false;
    }

    function shouldAjaxLink(a){
        if (!a) return false;

        var href = a.getAttribute("href") || "";
        if (!href) return false;
        if (href.indexOf("#") === 0) return false;
        if (href.indexOf("javascript:") === 0) return false;
        if (a.target && a.target !== "_self") return false;
        if (a.hasAttribute("download")) return false;
        if (!sameOrigin(a.href)) return false;

        var u = new URL(a.href, location.href);
        var path = u.pathname;

        if (shouldSkipPath(path)) return false;

        if (path.indexOf("/api/") === 0 && !isJsonApiAllowed(path)) {
            return false;
        }

        return true;
    }

    function confirmIfNeeded(a){
        var onclick = a.getAttribute("onclick") || "";

        if (onclick.indexOf("confirm") < 0) {
            return true;
        }

        var msg = "确定继续吗？";
        var m1 = onclick.match(/confirm\\('([^']*)'\\)/);
        var m2 = onclick.match(/confirm\\("([^"]*)"\\)/);

        if (m1 && m1[1]) msg = m1[1];
        if (m2 && m2[1]) msg = m2[1];

        return window.confirm(msg);
    }

    function setLoading(on){
        var content = document.querySelector(".content");
        if (!content) return;

        if (on) {
            content.style.opacity = "0.55";
            content.style.transition = "opacity .12s ease";
        } else {
            content.style.opacity = "1";
        }
    }

    function runInlineScripts(container){
        var scripts = container.querySelectorAll("script");
        scripts.forEach(function(oldScript){
            var script = document.createElement("script");

            for (var i = 0; i < oldScript.attributes.length; i++) {
                var attr = oldScript.attributes[i];
                script.setAttribute(attr.name, attr.value);
            }

            script.textContent = oldScript.textContent;
            oldScript.parentNode.replaceChild(script, oldScript);
        });
    }

    async function replaceHtmlFromResponse(res, push){
        var text = await res.text();
        var doc = new DOMParser().parseFromString(text, "text/html");

        var newContent = doc.querySelector(".content");
        var newTitle = doc.querySelector(".title");
        var newNav = doc.querySelector(".nav");

        var oldContent = document.querySelector(".content");
        var oldTitle = document.querySelector(".title");
        var oldNav = document.querySelector(".nav");

        if (!newContent || !oldContent) {
            location.href = res.url || location.href;
            return;
        }

        if (window.__FLS_ACTIVE_LOG_INTERVAL__) {
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }

        oldContent.innerHTML = newContent.innerHTML;

        if (newTitle && oldTitle) oldTitle.innerHTML = newTitle.innerHTML;
        if (newNav && oldNav) oldNav.innerHTML = newNav.innerHTML;

        document.title = doc.title || document.title;

        runInlineScripts(oldContent);

        if (push) {
            history.pushState({url: res.url || location.href}, "", res.url || location.href);
        }

        window.scrollTo(0, 0);

        if (typeof toggleMenu === "function") {
            toggleMenu(false);
        }
    }

    function showJsonResult(json){
        try {
            alert(JSON.stringify(json, null, 2));
        } catch(e) {
            alert(String(json));
        }
    }

    async function ajaxLoad(url, push){
        setLoading(true);

        try {
            var res = await fetch(url, {
                headers: {
                    "X-Requested-With": "FLS-Ajax"
                },
                credentials: "same-origin"
            });

            var ct = res.headers.get("content-type") || "";

            if (ct.indexOf("application/json") >= 0) {
                var json = await res.json();
                showJsonResult(json);
                setLoading(false);
                return;
            }

            if (!res.ok) {
                location.href = url;
                return;
            }

            await replaceHtmlFromResponse(res, push);
            setLoading(false);
        } catch(e) {
            location.href = url;
        }
    }

    document.addEventListener("click", function(e){
        var a = e.target.closest("a");
        if (!a) return;

        if (!a.closest(".content") && !a.closest(".nav")) return;
        if (!shouldAjaxLink(a)) return;

        e.preventDefault();
        e.stopImmediatePropagation();

        if (!confirmIfNeeded(a)) {
            return;
        }

        ajaxLoad(a.href, true);
    }, true);

    document.addEventListener("submit", async function(e){
        var form = e.target;
        if (!form || !form.closest(".content")) return;

        var method = (form.getAttribute("method") || "GET").toUpperCase();
        var action = form.getAttribute("action") || location.href;
        var url = new URL(action, location.href);

        if (!sameOrigin(url.href)) return;

        e.preventDefault();
        e.stopImmediatePropagation();

        setLoading(true);

        try {
            var opts = {
                method: method,
                headers: {
                    "X-Requested-With": "FLS-Ajax"
                },
                credentials: "same-origin"
            };

            var fetchUrl = url.href;

            if (method === "GET") {
                var fdGet = new FormData(form);
                fdGet.forEach(function(v, k){
                    url.searchParams.set(k, v);
                });
                fetchUrl = url.href;
            } else {
                var fd = new FormData(form);
                if (e.submitter && e.submitter.name && !fd.has(e.submitter.name)) {
                    fd.append(e.submitter.name, e.submitter.value || "");
                }
                opts.body = fd;
            }

            var res = await fetch(fetchUrl, opts);
            var ct = res.headers.get("content-type") || "";

            if (ct.indexOf("application/json") >= 0) {
                var json = await res.json();
                showJsonResult(json);
                setLoading(false);
                return;
            }

            if (!res.ok) {
                location.href = fetchUrl;
                return;
            }

            await replaceHtmlFromResponse(res, true);
            setLoading(false);
        } catch(err) {
            form.submit();
        }
    }, true);

    window.addEventListener("popstate", function(){
        ajaxLoad(location.href, false);
    });
})();
</script>
"""

        if "</body>" in html_text:
            return html_text.replace("</body>", patch_script + "\n</body>", 1)

        return html_text + patch_script

    globals()["layout"] = layout

except Exception as _fls_no_refresh_e:
    try:
        print("[NoRefreshPatch] 加载失败:", _fls_no_refresh_e)
    except Exception:
        pass

from urllib.parse import quote as _fls_script_new_quote


def _fls_script_new_join_url(path, **params):
    query = []
    for k, v in params.items():
        if v is None:
            continue
        v = str(v)
        if v == "":
            continue
        query.append(f"{_fls_script_new_quote(str(k))}={_fls_script_new_quote(v)}")

    if query:
        sep = "&" if "?" in path else "?"
        path = path + sep + "&".join(query)

    return path


def _fls_script_new_current_dir_from_request():
    current_rel = request.args.get("p", "").strip().strip("/")

    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    if not current_dir.is_dir():
        current_dir = current_dir.parent
        current_rel = _fls_rel_or_empty(current_dir)

    return current_rel, current_dir


def _fls_script_new_safe_child_rel(current_rel, name):
    name = str(name or "").strip().strip("/")

    if not name:
        raise ValueError("名称不能为空")

    if "/" in name or "\\" in name:
        raise ValueError("只能在当前目录新建，请不要在名称中包含 / 或 \\")

    if name in (".", ".."):
        raise ValueError("名称非法")

    if current_rel:
        return current_rel.strip("/").rstrip("/") + "/" + name

    return name


def _fls_scripts_new_page_20260429():
    msg = ""
    current_rel, current_dir = _fls_script_new_current_dir_from_request()

    if request.method == "POST":
        current_rel = request.form.get("current_rel", "").strip().strip("/")

        try:
            current_dir = script_safe_path(current_rel)
        except Exception:
            current_rel = ""
            current_dir = SCRIPT_DIR

        if not current_dir.exists():
            current_dir.mkdir(parents=True, exist_ok=True)

        if not current_dir.is_dir():
            current_dir = current_dir.parent
            current_rel = _fls_rel_or_empty(current_dir)

        item_type = request.form.get("item_type", "file").strip()
        name = request.form.get("name", "").strip()
        content = request.form.get("content", "")

        try:
            rel = _fls_script_new_safe_child_rel(current_rel, name)
            target = script_safe_path(rel)

            if target.exists():
                raise FileExistsError(f"目标已存在：{target}")

            if item_type == "dir":
                target.mkdir(parents=True, exist_ok=False)
                return redirect(_fls_script_url(current_rel))

            if item_type != "file":
                raise ValueError("新建类型错误")

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return redirect(_fls_script_url(current_rel))

        except Exception as e:
            msg = f"新建失败：{e}"

    back_url = _fls_script_url(current_rel)
    action_url = _fls_script_new_join_url("/pull/new", p=current_rel)

    body = f"""
<form method="post" action="{h(action_url)}">
<input type="hidden" name="current_rel" value="{h(current_rel)}">

<div class="card">
    <div class="card-title">新建文件 / 文件夹</div>
    <div class="help">
        当前目录：<b>{h(current_dir)}</b><br>
        路径：{_fls_breadcrumb(current_rel)}
    </div>
</div>

<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>新建类型</label>
            <select name="item_type">
                <option value="file">新建文件</option>
                <option value="dir">新建文件夹</option>
            </select>
            <div class="help">选择“新建文件夹”时，下方文件内容会被忽略。</div>
        </div>

        <div class="form-item">
            <label>名称</label>
            <input name="name" required placeholder="例如：test.py 或 demo">
            <div class="help">只能在当前目录新建，不要包含 / 或 \\。</div>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">文件内容</div>
    <textarea name="content" placeholder="新建文件时可在这里输入内容，然后点击保存。"></textarea>
    <div class="help">
        点击“保存新建”后才会创建文件。只返回或离开页面不会创建文件。
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存新建</button>
    <a class="btn btn-gray" href="{h(back_url)}">返回当前目录</a>
</div>

<div class="card">
    <div class="card-title">提示</div>
    <div class="code">
新建 Python 文件：test.py<br>
新建 Shell 文件：run.sh<br>
新建 Node 文件：demo.js<br>
新建文件夹：demo
    </div>
</div>

<div class="card">
    <div class="card-title">结果</div>
    <div class="help">{h(msg or "暂无操作")}</div>
</div>
</form>
"""
    return layout("新建脚本", "pull", body)
try:
    if "_fls_scripts_new_page_20260429" not in app.view_functions:
        app.add_url_rule(
            "/pull/new",
            endpoint="_fls_scripts_new_page_20260429",
            view_func=_fls_scripts_new_page_20260429,
            methods=["GET", "POST"],
        )
    else:
        pass

    pass

except Exception as e:
    print(f"[ScriptNewPatch] 加载失败: {e}")

from urllib.parse import quote as _fls_file_view_quote
from urllib.parse import unquote as _fls_file_view_unquote


def _fls_file_view_url(path_value):
    """
    生成文件查看 URL。
    path 参数既支持相对路径，也支持绝对路径。
    """
    return "/scripts/view?path=" + _fls_file_view_quote(str(path_value or ""))
def _fls_extract_task_file_from_command(command):
    """
    从任务命令中提取可查看的文件路径。

    支持：
    - task a.py
    - task ./a.py
    - task ../a.py
    - task /root/a.py
    - python3 /root/a.py
    - bash ./run.sh
    - node /root/demo.js
    """
    raw = str(command or "").strip()

    if not raw:
        return ""

    try:
        parts = shlex.split(raw)
    except Exception:
        parts = raw.split()

    if not parts:
        return ""

    # task 命令：第二个参数是脚本路径
    if parts[0] == "task" and len(parts) >= 2:
        return parts[1]

    # 普通系统命令：尽量找第一个像文件且存在的参数
    for item in parts[1:]:
        if not item:
            continue

        candidate = Path(item).expanduser()

        try:
            if candidate.is_absolute() and candidate.exists() and candidate.is_file():
                return str(candidate)
        except Exception:
            pass

        try:
            rel_candidate = (BASE_DIR / item).resolve()
            if rel_candidate.exists() and rel_candidate.is_file():
                return str(rel_candidate)
        except Exception:
            pass

        try:
            script_candidate = (SCRIPT_DIR / item).resolve()
            if script_candidate.exists() and script_candidate.is_file():
                return item
        except Exception:
            pass

    return ""


def _fls_command_with_file_link(command):
    """
    任务列表中的命令展示：
    如果能识别文件路径，则命令文本可点击查看文件。
    """
    command_text = str(command or "")
    file_path = _fls_extract_task_file_from_command(command_text)

    if not file_path:
        return h(command_text)

    return f'<a href="{h(_fls_file_view_url(file_path))}">{h(command_text)}</a>'


# 已禁用重复路由：原 @app.route("/scripts/view")
def _fls_task_script_path_for_build(rel_or_abs):
    """
    task 命令路径解析：
    - 绝对路径：直接使用；
    - 相对路径：相对 SCRIPT_DIR；
    """
    raw_path = str(rel_or_abs or "").strip()

    if not raw_path:
        raise ValueError("脚本路径不能为空")

    p = Path(raw_path).expanduser()

    if p.is_absolute():
        script_path = p.resolve()
    else:
        script_path = (SCRIPT_DIR / raw_path).resolve()

    if not script_path.exists():
        raise FileNotFoundError(f"脚本不存在：{script_path}")

    if not script_path.is_file():
        raise ValueError(f"不是文件：{script_path}")

    return script_path


def build_command(task):
    """
    覆盖 build_command：
    task 命令支持相对路径和绝对路径。

    支持：
    - .py  -> python
    - .sh  -> bash
    - .js  -> node
    - .ts  -> tsx 或 ts-node
    - .ps1 -> pwsh 或 powershell
    - .bat -> Windows shell
    - .php -> php
    - .rb  -> ruby
    - .pl  -> perl
    - .lua -> lua
    - .jar -> java -jar
    """
    raw = str(task.get("command", "")).strip()

    if not raw:
        raise ValueError("任务命令为空")

    if raw == "task" or raw.startswith("task "):
        parts = shlex.split(raw)

        if len(parts) < 2:
            raise ValueError("task 后面需要填写脚本路径，例如：task 1.py 或 task /root/test.py")

        script_path = _fls_task_script_path_for_build(parts[1])
        args = parts[2:]

        suffix = script_path.suffix.lower().lstrip(".")

        # 配置页类型开关。
        if "_fls_type_enabled" in globals():
            if not _fls_type_enabled(suffix):
                raise ValueError(f"脚本类型 .{suffix} 当前已禁用，请到配置页启用")

        if suffix == "py":
            return {
                "cmd": [PYTHON_BIN, str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "sh":
            if not _fls_cmd_exists(BASH_BIN):
                raise ValueError("bash 不可用，无法运行 .sh")
            return {
                "cmd": [BASH_BIN, str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "js":
            if not _fls_cmd_exists(NODE_BIN):
                raise ValueError("node 不可用，无法运行 .js")
            return {
                "cmd": [NODE_BIN, str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "ts":
            runner = shutil.which("tsx") or shutil.which("ts-node")
            if not runner:
                raise ValueError("tsx / ts-node 不可用，无法运行 .ts，请先安装相关 Node 依赖")
            return {
                "cmd": [runner, str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "ps1":
            shell_bin = shutil.which("pwsh") or shutil.which("powershell")
            if not shell_bin:
                raise ValueError("PowerShell 不可用，无法运行 .ps1")
            return {
                "cmd": [shell_bin, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "bat":
            if os.name != "nt":
                raise ValueError(".bat 仅支持 Windows")
            return {
                "cmd": [str(script_path)] + args,
                "shell": True,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "php":
            if not _fls_cmd_exists("php"):
                raise ValueError("php 不可用，无法运行 .php")
            return {
                "cmd": ["php", str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "rb":
            if not _fls_cmd_exists("ruby"):
                raise ValueError("ruby 不可用，无法运行 .rb")
            return {
                "cmd": ["ruby", str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "pl":
            if not _fls_cmd_exists("perl"):
                raise ValueError("perl 不可用，无法运行 .pl")
            return {
                "cmd": ["perl", str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "lua":
            if not _fls_cmd_exists("lua"):
                raise ValueError("lua 不可用，无法运行 .lua")
            return {
                "cmd": ["lua", str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        if suffix == "jar":
            if not _fls_cmd_exists("java"):
                raise ValueError("java 不可用，无法运行 .jar")
            return {
                "cmd": ["java", "-jar", str(script_path)] + args,
                "shell": False,
                "cwd": str(script_path.parent),
                "mode": "task",
            }

        raise ValueError(f"不支持的脚本类型：.{suffix}")

    return {
        "cmd": raw,
        "shell": True,
        "cwd": str(BASE_DIR),
        "mode": "system",
    }

def _fls_render_file_manager_rows_with_view_20260429(current_rel=""):
    """
    覆盖脚本管理文件列表：
    文件名可点击查看。
    """
    current_rel = str(current_rel or "").strip().strip("/")

    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    if not current_dir.is_dir():
        current_dir = current_dir.parent
        current_rel = _fls_rel_or_empty(current_dir)

    rows = ""

    if current_dir.resolve() != SCRIPT_DIR.resolve():
        parent = current_dir.parent
        parent_rel = _fls_rel_or_empty(parent)

        rows += f"""
<tr>
    <td><span class="badge gray">返回</span></td>
    <td>
        <a href="{h(_fls_script_url(parent_rel))}" style="font-weight:900;font-size:16px;">..</a>
    </td>
    <td>-</td>
    <td>-</td>
    <td>{h(str(parent))}</td>
    <td>
        <a class="btn btn-gray" href="{h(_fls_script_url(parent_rel))}">返回上级</a>
    </td>
</tr>
"""

    try:
        items = list(current_dir.iterdir())
    except Exception:
        items = []

    items.sort(key=lambda x: (x.is_file(), x.name.lower()))

    if not items and not rows:
        return '<tr><td colspan="6">暂无脚本，请点击“拉取”“导入”或“新建”添加脚本</td></tr>'

    if not items and rows:
        rows += '<tr><td colspan="6">当前目录为空</td></tr>'
        return rows

    for item in items:
        try:
            rel = script_rel_path(item)
        except Exception:
            continue

        is_dir = item.is_dir()

        badge = '<span class="badge green">文件夹</span>' if is_dir else '<span class="badge blue">文件</span>'

        if is_dir:
            size_text = "-"
            name_html = f'<a href="{h(_fls_script_url(rel))}" style="font-weight:800;">📁 {h(item.name)}</a>'
            open_btn = f'<a class="btn btn-primary" href="{h(_fls_script_url(rel))}">打开</a>'
        else:
            try:
                size = item.stat().st_size / 1024
                size_text = f"{size:.1f} KB"
            except Exception:
                size_text = "-"

            view_url = _fls_file_view_url(rel)
            name_html = f'<a href="{h(view_url)}" style="font-weight:800;">📄 {h(item.name)}</a>'
            open_btn = f'<a class="btn btn-blue" href="{h(view_url)}">查看</a>'

        try:
            mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = "-"

        rows += f"""
<tr>
    <td>{badge}</td>
    <td>
        {name_html}
        <div class="help">{h(rel)}</div>
    </td>
    <td>{h(size_text)}</td>
    <td>{h(mtime)}</td>
    <td>{h(str(item))}</td>
    <td>
        {open_btn}
        <a class="btn btn-red" href="{h(_fls_script_delete_url(rel))}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
    </td>
</tr>
"""

    return rows


_fls_render_file_manager_rows = _fls_render_file_manager_rows_with_view_20260429
_fls_render_file_manager_rows.__name__ = "_fls_render_file_manager_rows_with_view_20260429"

try:
    globals()["_fls_render_file_manager_rows"] = _fls_render_file_manager_rows_with_view_20260429
except Exception:
    pass
def tasks_table(tasks):
    """
    覆盖任务表格：
    命令列可点击查看任务文件。
    新增：下次执行时间
    """
    token = tq()

    html_text = """
<table id="tasksTable">
<thead>
<tr>
    <th>任务名</th>
    <th>命令</th>
    <th>Cron</th>
    <th>下次执行</th>
    <th>启用</th>
    <th>状态</th>
    <th>运行次数</th>
    <th>PID</th>
    <th>进程名</th>
    <th>操作</th>
</tr>
</thead>
<tbody>
"""

    if not tasks:
        html_text += '<tr><td colspan="10">暂无任务，请点击新建任务</td></tr>'
    else:
        for task in tasks:
            task_id = task["id"]
            name = task.get("name") or task.get("command") or "未命名任务"
            command = task.get("command", "")
            cron = task.get("cron", "") or "手动"
            next_run_text = get_task_next_run_time_text(task)
            enabled = task.get("enabled", True)
            run_count = int(task.get("run_count", 0))

            running = is_running(task_id)
            pid = RUNNING.get(task_id, {}).get("pid", "-") if running else "-"
            process_name = RUNNING.get(task_id, {}).get("process_name", "-") if running else safe_process_name(name)

            enabled_badge = '<span class="badge green">启用</span>' if enabled else '<span class="badge gray">禁用</span>'
            status_badge = '<span class="badge blue">运行中</span>' if running else '<span class="badge red">已停止</span>'

            toggle_text = "禁用" if enabled else "启用"
            toggle_class = "btn-gray" if enabled else "btn-primary"

            html_text += f"""
<tr data-task-id="{h(task_id)}">
    <td><b>{h(name)}</b></td>
    <td>{_fls_command_with_file_link(command)}</td>
    <td>{h(cron)}</td>
    <td>{h(next_run_text)}</td>
    <td>{enabled_badge}</td>
    <td>{status_badge}</td>
    <td>{run_count}</td>
    <td>{h(pid)}</td>
    <td>{h(process_name)}</td>
    <td>
        <button class="btn btn-primary" type="button" onclick="taskAjaxAction('run','{h(task_id)}')">运行</button>
        <button class="btn btn-red" type="button" onclick="taskAjaxAction('stop','{h(task_id)}')">结束</button>
        <a class="btn btn-orange" href="/log/{h(task_id)}{token}">日志</a>
        <a class="btn btn-blue" href="/task/edit/{h(task_id)}{token}">编辑</a>
        <button class="btn {toggle_class}" type="button" onclick="taskAjaxAction('toggle','{h(task_id)}')">{toggle_text}</button>
        <button class="btn btn-gray" type="button" onclick="taskAjaxAction('delete','{h(task_id)}')">删除</button>
    </td>
</tr>
"""

    html_text += """
</tbody>
</table>

<script>
async function taskAjaxAction(action, taskId){
    if(action === "delete"){
        if(!confirm("确定删除该任务吗？")) return;
    }
    if(action === "stop"){
        if(!confirm("确定结束该任务吗？")) return;
    }

    const row = document.querySelector('tr[data-task-id="' + CSS.escape(taskId) + '"]');
    if(row){
        row.style.opacity = "0.55";
    }

    try{
        const url = "/api/task/action/" + encodeURIComponent(action) + "/" + encodeURIComponent(taskId) + "__TOKEN_PLACEHOLDER__";
        const res = await fetch(url, {
            method: "POST",
            headers: {"X-Requested-With": "XMLHttpRequest"}
        });
        const json = await res.json();

        if(!json.ok){
            alert(json.msg || "操作失败");
            if(row) row.style.opacity = "1";
            return;
        }

        await refreshTasksTablePartial();

    }catch(e){
        alert("请求失败：" + e);
        if(row) row.style.opacity = "1";
    }
}

async function refreshTasksTablePartial(){
    try{
        const res = await fetch(window.location.href, {
            headers: {"X-Requested-With": "XMLHttpRequest"}
        });
        const html = await res.text();
        const doc = new DOMParser().parseFromString(html, "text/html");
        const newTable = doc.querySelector("#tasksTable");
        const oldTable = document.querySelector("#tasksTable");

        if(newTable && oldTable){
            oldTable.replaceWith(newTable);
        }else{
            location.reload();
        }
    }catch(e){
        location.reload();
    }
}
</script>
"""
    if ADMIN_TOKEN:
        html_text = html_text.replace("__TOKEN_PLACEHOLDER__", f"?token={ADMIN_TOKEN}")
    else:
        html_text = html_text.replace("__TOKEN_PLACEHOLDER__", "")

    return html_text


try:
    pass
except Exception as e:
    print(f"[FileViewPatch] 覆盖脚本管理页面失败: {e}")


try:
    pass
except Exception:
    pass

from urllib.parse import quote as _fls_rename_edit_quote
from urllib.parse import unquote as _fls_rename_edit_unquote


def _fls_rename_edit_file_url(path_value):
    return "/scripts/view?path=" + _fls_rename_edit_quote(str(path_value or ""))


def _fls_rename_edit_rename_url(path_value):
    return "/scripts/rename?path=" + _fls_rename_edit_quote(str(path_value or ""))


def _fls_rename_edit_resolve(path_value):
    raw = str(path_value or "").strip()
    if not raw:
        raise ValueError("路径不能为空")

    raw = _fls_rename_edit_unquote(raw)
    p = Path(raw).expanduser()

    if p.is_absolute():
        target = p.resolve()
    else:
        target = (SCRIPT_DIR / raw).resolve()

    if not target.exists():
        raise FileNotFoundError(f"路径不存在：{target}")

    return target


def _fls_rename_edit_display_path(target):
    try:
        return str(Path(target).resolve().relative_to(SCRIPT_DIR.resolve()))
    except Exception:
        return str(Path(target).resolve())


def _fls_rename_edit_back_url(target):
    try:
        target = Path(target).resolve()
        script_root = SCRIPT_DIR.resolve()

        if target == script_root:
            return _fls_script_url("")

        if str(target).startswith(str(script_root)):
            parent = target.parent
            if parent == script_root:
                return _fls_script_url("")
            return _fls_script_url(str(parent.relative_to(script_root)))
    except Exception:
        pass

    return "/pull"


def _fls_rename_edit_safe_name(name):
    name = str(name or "").strip()

    if not name:
        raise ValueError("新名称不能为空")

    if "/" in name or "\\" in name:
        raise ValueError("新名称不能包含 / 或 \\")

    if name in (".", ".."):
        raise ValueError("新名称非法")

    return name


@app.route("/scripts/rename", methods=["GET", "POST"])
def _fls_scripts_rename_20260429():
    raw_path = request.args.get("path", "").strip()
    msg = ""

    try:
        target = _fls_rename_edit_resolve(raw_path)
    except Exception as e:
        body = f"""
<div class="card">
    <div class="card-title">改名失败</div>
    <div class="help">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="/pull">返回脚本管理</a>
</div>
"""
        return layout("改名失败", "pull", body), 400

    try:
        target.resolve().relative_to(SCRIPT_DIR.resolve())
    except Exception:
        body = f"""
<div class="card">
    <div class="card-title">不允许改名</div>
    <div class="help">
        只能改名脚本目录内的文件或文件夹。<br>
        当前路径：{h(target)}
    </div>
    <br>
    <a class="btn btn-gray" href="/pull">返回脚本管理</a>
</div>
"""
        return layout("不允许改名", "pull", body), 400

    if target.resolve() == SCRIPT_DIR.resolve():
        body = """
<div class="card">
    <div class="card-title">不允许改名</div>
    <div class="help">不允许改名 scripts 根目录。</div>
    <br>
    <a class="btn btn-gray" href="/pull">返回脚本管理</a>
</div>
"""
        return layout("不允许改名", "pull", body), 400

    if request.method == "POST":
        try:
            new_name = _fls_rename_edit_safe_name(request.form.get("new_name", ""))
            new_target = target.with_name(new_name).resolve()

            try:
                new_target.relative_to(SCRIPT_DIR.resolve())
            except Exception:
                raise ValueError("新路径非法")

            if new_target.exists():
                raise FileExistsError(f"目标已存在：{new_target}")

            target.rename(new_target)
            return redirect(_fls_rename_edit_back_url(new_target))

        except Exception as e:
            msg = f"改名失败：{e}"

    type_text = "文件夹" if target.is_dir() else "文件"
    back_url = _fls_rename_edit_back_url(target)

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">改名{h(type_text)}</div>
    <div class="help">
        当前路径：<b>{h(target)}</b><br>
        显示路径：<b>{h(_fls_rename_edit_display_path(target))}</b>
    </div>
</div>

<div class="card">
    <div class="form-item">
        <label>新名称</label>
        <input name="new_name" required value="{h(target.name)}" placeholder="请输入新名称">
        <div class="help">只能修改当前文件或文件夹名称，不能包含 / 或 \\。</div>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存改名</button>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>

<div class="card">
    <div class="card-title">结果</div>
    <div class="help">{h(msg or "暂无操作")}</div>
</div>
</form>
"""
    return layout("改名", "pull", body)


def _fls_scripts_view_edit_20260429():
    raw_path = request.args.get("path", "").strip()
    msg = ""

    try:
        target = _fls_rename_edit_resolve(raw_path)
        if not target.is_file():
            raise ValueError(f"不是文件：{target}")
    except Exception as e:
        body = f"""
<div class="card">
    <div class="card-title">查看文件失败</div>
    <div class="help">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="/pull">返回脚本管理</a>
</div>
"""
        return layout("查看文件失败", "pull", body), 400

    if request.method == "POST":
        try:
            new_content = request.form.get("content", "")
            target.write_text(new_content, encoding="utf-8")
            msg = f"保存成功：{now_str()}"
        except Exception as e:
            msg = f"保存失败：{e}"

    try:
        size = target.stat().st_size
    except Exception:
        size = 0

    max_read = 1024 * 1024
    truncated = False

    try:
        with open(target, "rb") as f:
            data = f.read(max_read + 1)

        if len(data) > max_read:
            truncated = True
            data = data[:max_read]

        content = data.decode("utf-8", errors="replace")
    except Exception as e:
        content = f"读取文件失败：{e}"

    display_path = _fls_rename_edit_display_path(target)
    back_url = _fls_rename_edit_back_url(target)
    rename_url = _fls_rename_edit_rename_url(display_path)
    view_action = _fls_rename_edit_file_url(raw_path)

    download_btn = ""
    try:
        rel = str(target.resolve().relative_to(SCRIPT_DIR.resolve()))
        download_btn = f'<a class="btn btn-blue" href="{h(_fls_script_download_url(rel))}">下载</a>'
    except Exception:
        download_btn = ""

    truncated_text = ""
    if truncated:
        truncated_text = "，文件超过 1MB，仅显示前 1MB，保存会用当前显示内容覆盖原文件，请谨慎操作"

    body = f"""
<form method="post" action="{h(view_action)}">
<div class="card">
    <div class="card-title">查看 / 编辑文件：{h(target.name)}</div>
    <div class="help">
        路径：<b>{h(str(target))}</b><br>
        显示路径：<b>{h(display_path)}</b><br>
        大小：<b>{h(size)} Bytes{h(truncated_text)}</b>
    </div>
    <br>
    <button class="btn btn-primary" type="submit">保存文件</button>
    <a class="btn btn-orange" href="{h(rename_url)}">改名</a>
    {download_btn}
    <a class="btn btn-gray" href="{h(back_url)}">返回脚本管理</a>
</div>

<div class="card">
    <div class="card-title">文件内容</div>
    <textarea name="content" style="min-height:680px;">{h(content)}</textarea>
    <div class="help">
        修改后点击“保存文件”才会写入磁盘。<br>
        二进制文件不建议在这里编辑。
    </div>
</div>

<div class="card">
    <div class="card-title">保存结果</div>
    <div class="help">{h(msg or "暂无保存操作")}</div>
</div>
</form>
"""
    return layout("查看 / 编辑文件", "pull", body)


def _fls_render_file_manager_rows_rename_edit_20260429(current_rel=""):
    current_rel = str(current_rel or "").strip().strip("/")

    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    if not current_dir.is_dir():
        current_dir = current_dir.parent
        current_rel = _fls_rel_or_empty(current_dir)

    rows = ""

    if current_dir.resolve() != SCRIPT_DIR.resolve():
        parent = current_dir.parent
        parent_rel = _fls_rel_or_empty(parent)

        rows += f"""
<tr>
    <td><span class="badge gray">返回</span></td>
    <td>
        <a href="{h(_fls_script_url(parent_rel))}" style="font-weight:900;font-size:16px;">..</a>
    </td>
    <td>-</td>
    <td>-</td>
    <td>{h(str(parent))}</td>
    <td>
        <a class="btn btn-gray" href="{h(_fls_script_url(parent_rel))}">返回上级</a>
    </td>
</tr>
"""

    try:
        items = list(current_dir.iterdir())
    except Exception:
        items = []

    items.sort(key=lambda x: (x.is_file(), x.name.lower()))

    if not items and not rows:
        return '<tr><td colspan="6">暂无脚本，请点击“拉取”“导入”或“新建”添加脚本</td></tr>'

    if not items and rows:
        rows += '<tr><td colspan="6">当前目录为空</td></tr>'
        return rows

    for item in items:
        try:
            rel = script_rel_path(item)
        except Exception:
            continue

        is_dir = item.is_dir()
        badge = '<span class="badge green">文件夹</span>' if is_dir else '<span class="badge blue">文件</span>'

        try:
            mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = "-"

        rename_url = _fls_rename_edit_rename_url(rel)

        if is_dir:
            size_text = "-"
            name_html = f'<a href="{h(_fls_script_url(rel))}" style="font-weight:800;">📁 {h(item.name)}</a>'
            buttons = f"""
<a class="btn btn-primary" href="{h(_fls_script_url(rel))}">打开</a>
<a class="btn btn-orange" href="{h(rename_url)}">改名</a>
<a class="btn btn-red" href="{h(_fls_script_delete_url(rel))}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
"""
        else:
            try:
                size = item.stat().st_size / 1024
                size_text = f"{size:.1f} KB"
            except Exception:
                size_text = "-"

            view_url = _fls_rename_edit_file_url(rel)
            download_url = _fls_script_download_url(rel)

            name_html = f'<a href="{h(view_url)}" style="font-weight:800;">📄 {h(item.name)}</a>'
            buttons = f"""
<a class="btn btn-blue" href="{h(view_url)}">查看</a>
<a class="btn btn-primary" href="{h(download_url)}">下载</a>
<a class="btn btn-orange" href="{h(rename_url)}">改名</a>
<a class="btn btn-red" href="{h(_fls_script_delete_url(rel))}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
"""

        rows += f"""
<tr>
    <td>{badge}</td>
    <td>
        {name_html}
        <div class="help">{h(rel)}</div>
    </td>
    <td>{h(size_text)}</td>
    <td>{h(mtime)}</td>
    <td>{h(str(item))}</td>
    <td>{buttons}</td>
</tr>
"""

    return rows


def _fls_scripts_page_rename_edit_20260429():
    current_rel = request.args.get("p", "").strip().strip("/")

    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    if not current_dir.is_dir():
        current_dir = current_dir.parent
        current_rel = _fls_rel_or_empty(current_dir)

    if "_fls_script_new_join_url" in globals():
        new_url = _fls_script_new_join_url("/pull/new", p=current_rel)
    else:
        new_url = "/pull/new"

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">脚本管理</div>
            <div class="help">
                当前目录：<b>{h(current_dir)}</b><br>
                路径：{_fls_breadcrumb(current_rel)}
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-primary" href="/pull/fetch">拉取</a>
            <a class="btn btn-orange" href="/pull/import">导入</a>
            <a class="btn btn-blue" href="{h(new_url)}">新建</a>
            <a class="btn btn-gray" href="{h(_fls_script_url(''))}">回到根目录</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">文件列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>类型</th>
                    <th>名称 / 相对路径</th>
                    <th>大小</th>
                    <th>修改时间</th>
                    <th>绝对路径</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{_fls_render_file_manager_rows_rename_edit_20260429(current_rel)}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令示例</div>
    <div class="code">
task 1.py<br>
task ./1.py<br>
task folder/main.py<br>
task ../other.py<br>
task /root/test.py<br>
task /root/fls/scripts/demo.sh arg1 arg2<br>
task /root/fls/scripts/demo.js
    </div>
</div>
"""
    return layout("脚本管理", "pull", body)


# 覆盖 /scripts/view 现有 endpoint，并允许 POST。
try:
    _found_view_rule = False

    for _rule in app.url_map.iter_rules():
        if str(_rule.rule) == "/scripts/view":
            _found_view_rule = True
            _rule.methods.add("POST")
            _rule.methods.add("OPTIONS")
            app.view_functions[_rule.endpoint] = _fls_scripts_view_edit_20260429

    if not _found_view_rule:
        app.add_url_rule(
            "/scripts/view",
            endpoint="_fls_scripts_view_edit_20260429",
            view_func=_fls_scripts_view_edit_20260429,
            methods=["GET", "POST"],
        )
except Exception as e:
    print(f"[RenameEditPatch] 覆盖 /scripts/view 失败: {e}")


# 覆盖脚本管理页面。
try:
    globals()["_fls_render_file_manager_rows"] = _fls_render_file_manager_rows_rename_edit_20260429
    app.view_functions["scripts_page"] = _fls_scripts_page_rename_edit_20260429
except Exception as e:
    print(f"[RenameEditPatch] 覆盖脚本管理页面失败: {e}")


try:
    pass
except Exception:
    pass

def _fls_logs_display_title_20260429(group_name, count):
    name = str(group_name or "").strip() or "其他日志"

    if name == "其他日志":
        title = "其他日志"
    else:
        title = f"任务：{name}"

    if int(count or 0) > 1:
        title += f"（{int(count)}）"

    return title


def _fls_logs_page_title_patch_20260429():
    q = request.args.get("q", "").strip().lower()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 10

    files = sorted(LOG_DIR.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)

    groups = {}
    for f in files:
        if f.name.startswith("deps-install-") or f.name.startswith("system-install-") or f.name.startswith("fls-manager"):
            key = "其他日志"
        else:
            key = parse_task_name_from_log(f) or "其他日志"

        groups.setdefault(key, []).append(f)

    group_items = []
    for name, fs in groups.items():
        if q:
            matched = q in name.lower() or any(q in x.name.lower() for x in fs)
            if not matched:
                continue
        group_items.append((name, fs))

    group_items.sort(
        key=lambda x: max([f.stat().st_mtime for f in x[1]] or [0]),
        reverse=True
    )

    total = len(group_items)
    pages = max(1, _fls_patch_ceil(total / per_page))
    page = min(page, pages)
    show = group_items[(page - 1) * per_page: page * per_page]

    content = f"""
<form method="get">
<div class="card">
    <div class="card-title">日志管理</div>
    <div class="help">同一分组超过 5 条日志会自动折叠；日志分组过多时会自动分页。</div>
    <br>
    <div class="form-grid">
        <div class="form-item">
            <label>搜索日志</label>
            <input name="q" value="{h(q)}" placeholder="任务名 / 日志文件名">
        </div>
        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">搜索</button>
            <a class="btn btn-gray" href="/logs">重置</a>
        </div>
    </div>
</div>
</form>
"""

    if not show:
        content += """
<div class="card">
    <div class="help">暂无匹配日志</div>
</div>
"""
    else:
        for task_name, log_files in show:
            rows = ""

            for f in log_files:
                size = f.stat().st_size / 1024
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

                rows += f"""
<tr>
    <td>{h(f.name)}</td>
    <td>{size:.1f} KB</td>
    <td>{h(mtime)}</td>
    <td>
        <a class="btn btn-orange" href="/logfile/{h(f.name)}">查看</a>
        <a class="btn btn-red" href="/logfile/delete/{h(f.name)}" onclick="return confirm('确定删除日志 {h(f.name)} 吗？')">删除</a>
    </td>
</tr>
"""

            table = f"""
<div class="table-wrap">
<table>
<thead>
<tr>
    <th>日志文件</th>
    <th>大小</th>
    <th>修改时间</th>
    <th>操作</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
</div>
"""

            display_title = _fls_logs_display_title_20260429(task_name, len(log_files))

            if len(log_files) > 5:
                content += f"""
<div class="card">
    <details>
        <summary style="cursor:pointer;font-weight:900;">{h(display_title)}</summary>
        <br>
        {table}
    </details>
</div>
"""
            else:
                content += f"""
<div class="card">
    <div class="card-title">{h(display_title)}</div>
    {table}
</div>
"""

    content += _fls_page_links("/logs", q, page, pages)
    return layout("日志管理", "logs", content)


try:
    app.view_functions["logs_page"] = _fls_logs_page_title_patch_20260429
except Exception as e:
    print(f"[LogsTitlePatch] 加载失败: {e}")
def _fls_runtime_cmd_output(cmd):
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8
        )
        out = (r.stdout or "").strip()
        if not out:
            return ""
        return out.splitlines()[0].strip()
    except Exception:
        return ""


def _fls_runtime_status_items():
    ts_runner = shutil.which("tsx") or shutil.which("ts-node")
    ts_cmd = Path(ts_runner).name if ts_runner else "tsx / ts-node"

    items = [
        {
            "key": "python",
            "name": "Python",
            "suffix": ".py",
            "command": Path(PYTHON_BIN).name or "python",
            "version": sys.version.split()[0],
            "install_url": "/install/runtime/python",
        },
        {
            "key": "bash",
            "name": "Bash",
            "suffix": ".sh",
            "command": Path(BASH_BIN).name or "bash",
            "version": get_cmd_version("bash") or _fls_runtime_cmd_output(["bash", "--version"]),
            "install_url": "/install/runtime/bash",
        },
        {
            "key": "node",
            "name": "Node.js",
            "suffix": ".js",
            "command": "node",
            "version": get_cmd_version("node") or _fls_runtime_cmd_output(["node", "--version"]),
            "install_url": "/install/node",
        },
        {
            "key": "ts",
            "name": "TypeScript",
            "suffix": ".ts",
            "command": ts_cmd,
            "version": _fls_runtime_cmd_output([ts_runner, "--version"]) if ts_runner else "",
            "install_url": "/install/runtime/typescript",
        },
        {
            "key": "php",
            "name": "PHP",
            "suffix": ".php",
            "command": "php",
            "version": _fls_runtime_cmd_output(["php", "-v"]),
            "install_url": "/install/runtime/php",
        },
        {
            "key": "ruby",
            "name": "Ruby",
            "suffix": ".rb",
            "command": "ruby",
            "version": _fls_runtime_cmd_output(["ruby", "-v"]),
            "install_url": "/install/runtime/ruby",
        },
        {
            "key": "perl",
            "name": "Perl",
            "suffix": ".pl",
            "command": "perl",
            "version": _fls_runtime_cmd_output(["perl", "-v"]),
            "install_url": "/install/runtime/perl",
        },
        {
            "key": "lua",
            "name": "Lua",
            "suffix": ".lua",
            "command": "lua",
            "version": _fls_runtime_cmd_output(["lua", "-v"]),
            "install_url": "/install/runtime/lua",
        },
        {
            "key": "java",
            "name": "Java",
            "suffix": ".jar",
            "command": "java",
            "version": _fls_runtime_cmd_output(["java", "-version"]),
            "install_url": "/install/runtime/java",
        },
    ]

    if os.name == "nt":
        ps_version = _fls_runtime_cmd_output(["pwsh", "--version"]) or _fls_runtime_cmd_output(["powershell", "-Version"])
        items.append({
            "key": "powershell",
            "name": "PowerShell",
            "suffix": ".ps1",
            "command": "pwsh / powershell",
            "version": ps_version,
            "install_url": "/install/runtime/powershell",
        })

    for item in items:
        item["installed"] = bool(item.get("version"))

    return items


def _fls_runtime_windows_url(runtime):
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


def _fls_runtime_install_command(runtime):
    if runtime == "typescript":
        if os.name == "nt":
            return None
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

    pm = ""
    if shutil.which("pkg"):
        pm = "pkg"
    elif shutil.which("apt"):
        pm = "apt"
    elif shutil.which("apt-get"):
        pm = "apt-get"
    elif shutil.which("dnf"):
        pm = "dnf"
    elif shutil.which("yum"):
        pm = "yum"
    elif shutil.which("apk"):
        pm = "apk"
    elif shutil.which("pacman"):
        pm = "pacman"

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


@app.route("/install/runtime/<runtime>")
def _fls_install_runtime(runtime):
    runtime = str(runtime or "").strip().lower()

    if runtime == "node":
        return redirect(url_for("_fls_install_node"))

    if os.name == "nt":
        return redirect(_fls_runtime_windows_url(runtime))

    install_id = uuid.uuid4().hex
    log_file = _fls_install_log_file(install_id, runtime)
    log_fp = open(log_file, "ab", buffering=0)

    try:
        cmd_text = _fls_runtime_install_command(runtime)
    except Exception as e:
        log_fp.write(f"生成安装命令失败: {e}\n".encode("utf-8"))
        log_fp.close()
        _fls_system_failure_notify(
            f"运行环境安装-{runtime}",
            f"生成安装命令失败：{e}",
            str(log_file),
            "",
        )
        return redirect(url_for("_fls_system_install_log", install_id=install_id))

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
        _fls_system_failure_notify(
            f"运行环境安装-{runtime}",
            f"启动安装失败：{e}",
            str(log_file),
            "",
        )

    return redirect(url_for("_fls_system_install_log", install_id=install_id))

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

    return redirect(url_for("_fls_system_install_log", install_id=install_id))
# ============================================================
# 配置端口加载
# - FLS_PORT 环境变量优先；
# - 未设置 FLS_PORT 时，读取配置页保存的 port；
# - 端口变更需要重启面板后生效。
# ============================================================
try:
    if not os.environ.get("FLS_PORT", "").strip():
        PORT = max(1, min(65535, int(load_config().get("port", PORT))))
except Exception as e:
    print(f"[Config] 读取配置端口失败，继续使用当前端口 {PORT}: {e}")


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    reload_scheduler()
    cleanup_logs()

    print("====================================================")
    print("FLS Flask Script Manager")
    print(f"主进程名: {MAIN_PROCESS_NAME}")
    print(f"任务进程名前缀: {TASK_PROCESS_PREFIX}")
    print(f"工作目录: {BASE_DIR}")
    print(f"数据目录: {DATA_DIR}")
    print(f"日志目录: {LOG_DIR}")
    print(f"脚本目录: {SCRIPT_DIR}")
    print(f"Python: {PYTHON_BIN}")
    print(f"Bash: {BASH_BIN}")
    print(f"Node: {NODE_BIN}")
    print(f"访问地址: http://{HOST}:{PORT}")

    _fls_runtime_token_for_print = fls_get_admin_token() if "fls_get_admin_token" in globals() else ADMIN_TOKEN
    if _fls_runtime_token_for_print:
        print(f"Token访问: http://服务器IP:{PORT}/?token={_fls_runtime_token_for_print}")
    else:
        print("警告: 当前未设置登录 Token，首次访问将进入 /setup 引导设置")

    print("====================================================")

    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
