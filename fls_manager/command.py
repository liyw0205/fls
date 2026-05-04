import os
import shlex
import shutil
import sys
from pathlib import Path

from .paths import BASE_DIR, SCRIPT_DIR
from .config import task_type_enabled

PYTHON_BIN = os.environ.get("FLS_PYTHON") or sys.executable
NODE_BIN = os.environ.get("FLS_NODE", "node")
BASH_BIN = os.environ.get("FLS_BASH", "bash")


def _exists(cmd):
    return bool(shutil.which(cmd))


def task_script_path(rel_or_abs):
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
    raw = str(task.get("command", "")).strip()

    if not raw:
        raise ValueError("任务命令为空")

    if raw == "task" or raw.startswith("task "):
        parts = shlex.split(raw)

        if len(parts) < 2:
            raise ValueError("task 后面需要填写脚本路径，例如：task 1.py")

        script_path = task_script_path(parts[1])
        args = parts[2:]
        suffix = script_path.suffix.lower().lstrip(".")

        if not task_type_enabled(suffix):
            raise ValueError(f"脚本类型 .{suffix} 当前已禁用，请到配置页启用")

        if suffix == "py":
            cmd = [PYTHON_BIN, str(script_path)] + args

        elif suffix == "sh":
            if not _exists(BASH_BIN):
                raise ValueError("bash 不可用")
            cmd = [BASH_BIN, str(script_path)] + args

        elif suffix == "js":
            if not _exists(NODE_BIN):
                raise ValueError("node 不可用")
            cmd = [NODE_BIN, str(script_path)] + args

        elif suffix == "ts":
            runner = shutil.which("tsx") or shutil.which("ts-node")
            if not runner:
                raise ValueError("tsx / ts-node 不可用")
            cmd = [runner, str(script_path)] + args

        elif suffix == "ps1":
            runner = shutil.which("pwsh") or shutil.which("powershell")
            if not runner:
                raise ValueError("PowerShell 不可用，请安装 pwsh 或 powershell")
            cmd = [
                runner,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ] + args

        elif suffix == "bat":
            if os.name != "nt":
                raise ValueError("BAT 脚本仅支持 Windows")
            cmd = ["cmd", "/c", str(script_path)] + args

        elif suffix == "php":
            if not _exists("php"):
                raise ValueError("php 不可用")
            cmd = ["php", str(script_path)] + args

        elif suffix == "rb":
            if not _exists("ruby"):
                raise ValueError("ruby 不可用")
            cmd = ["ruby", str(script_path)] + args

        elif suffix == "pl":
            if not _exists("perl"):
                raise ValueError("perl 不可用")
            cmd = ["perl", str(script_path)] + args

        elif suffix == "lua":
            if not _exists("lua"):
                raise ValueError("lua 不可用")
            cmd = ["lua", str(script_path)] + args

        elif suffix == "jar":
            if not _exists("java"):
                raise ValueError("java 不可用")
            cmd = ["java", "-jar", str(script_path)] + args

        else:
            raise ValueError(f"不支持的脚本类型：.{suffix}")

        return {
            "cmd": cmd,
            "shell": False,
            "cwd": str(script_path.parent),
            "mode": "task",
        }

    return {
        "cmd": raw,
        "shell": True,
        "cwd": str(BASE_DIR),
        "mode": "system",
    }
