import os
import shlex
import shutil
import sys
import subprocess
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


def normalize_script_type(suffix):
    """
    把真实文件后缀归类到配置页里的脚本类型。

    例如：
    - mjs / cjs 都归类为 js
    - mts / cts 都归类为 ts
    - bash 归类为 sh
    - cmd 归类为 bat
    """
    suffix = str(suffix or "").lower().lstrip(".")

    alias_map = {
        "py": "py",
        "pyw": "py",

        "sh": "sh",
        "bash": "sh",

        "js": "js",
        "mjs": "js",
        "cjs": "js",

        "ts": "ts",
        "mts": "ts",
        "cts": "ts",

        "ps1": "ps1",

        "bat": "bat",
        "cmd": "bat",

        "php": "php",

        "rb": "rb",

        "pl": "pl",
        "pm": "pl",

        "lua": "lua",

        "jar": "jar",
    }

    return alias_map.get(suffix, suffix)


def supported_suffix_text():
    return (
        ".py/.pyw, "
        ".sh/.bash, "
        ".js/.mjs/.cjs, "
        ".ts/.mts/.cts, "
        ".ps1, "
        ".bat/.cmd, "
        ".php, "
        ".rb, "
        ".pl/.pm, "
        ".lua, "
        ".jar"
    )


def command_list_to_shell(cmd):
    """
    把命令数组转换成可放进 shell 的字符串。
    Linux / Termux 使用 shlex.quote；
    Windows 使用 subprocess.list2cmdline。
    """
    if os.name == "nt":
        return subprocess.list2cmdline([str(x) for x in cmd])

    return " ".join(shlex.quote(str(x)) for x in cmd)


def task_command_list(script_path, args):
    """
    根据脚本路径生成真实执行命令数组。
    """
    suffix = script_path.suffix.lower().lstrip(".")
    type_key = normalize_script_type(suffix)

    if not task_type_enabled(type_key):
        raise ValueError(
            f"脚本类型 .{suffix} 当前已禁用，请到配置页启用对应类型：{type_key}"
        )

    if type_key == "py":
        return [PYTHON_BIN, str(script_path)] + args

    if type_key == "sh":
        if not _exists(BASH_BIN):
            raise ValueError("bash 不可用，无法运行 Shell 脚本")
        return [BASH_BIN, str(script_path)] + args

    if type_key == "js":
        if not _exists(NODE_BIN):
            raise ValueError("node 不可用，无法运行 Node.js 脚本")
        return [NODE_BIN, str(script_path)] + args

    if type_key == "ts":
        runner = shutil.which("tsx") or shutil.which("ts-node")
        if not runner:
            raise ValueError("tsx / ts-node 不可用，无法运行 TypeScript 脚本")
        return [runner, str(script_path)] + args

    if type_key == "ps1":
        runner = shutil.which("pwsh") or shutil.which("powershell")
        if not runner:
            raise ValueError("PowerShell 不可用，请安装 pwsh 或 powershell")
        return [
            runner,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ] + args

    if type_key == "bat":
        if os.name != "nt":
            raise ValueError("BAT / CMD 脚本仅支持 Windows")
        return ["cmd", "/c", str(script_path)] + args

    if type_key == "php":
        if not _exists("php"):
            raise ValueError("php 不可用，无法运行 PHP 脚本")
        return ["php", str(script_path)] + args

    if type_key == "rb":
        if not _exists("ruby"):
            raise ValueError("ruby 不可用，无法运行 Ruby 脚本")
        return ["ruby", str(script_path)] + args

    if type_key == "pl":
        if not _exists("perl"):
            raise ValueError("perl 不可用，无法运行 Perl 脚本")
        return ["perl", str(script_path)] + args

    if type_key == "lua":
        if not _exists("lua"):
            raise ValueError("lua 不可用，无法运行 Lua 脚本")
        return ["lua", str(script_path)] + args

    if type_key == "jar":
        if not _exists("java"):
            raise ValueError("java 不可用，无法运行 Jar 文件")
        return ["java", "-jar", str(script_path)] + args

    raise ValueError(
        f"不支持的脚本类型：.{suffix}。当前支持：{supported_suffix_text()}"
    )


def parse_task_line_to_cmd(line):
    """
    把单行 task 命令转换成真实命令数组。

    支持：
        task 1.py
        task demo.mjs
        task folder/a.py arg1 arg2
        task /root/test.py

    注意：
    - 混合命令里建议 task 独占一行；
    - 不建议写：task a.py && echo ok
      因为 && 会被当成脚本参数。
    """
    try:
        parts = shlex.split(line)
    except Exception as e:
        raise ValueError(f"task 命令解析失败：{e}")

    if len(parts) < 2:
        raise ValueError("task 后面需要填写脚本路径，例如：task 1.py")

    script_path = task_script_path(parts[1])
    args = parts[2:]

    return task_command_list(script_path, args)


def is_task_line(line):
    """
    判断一行是否是 task 命令行。
    允许前面有空格。
    """
    stripped = str(line or "").strip()
    return stripped == "task" or stripped.startswith("task ")


def expand_mixed_command(raw):
    """
    展开混合命令。

    原始输入：
        echo start
        task 1.py
        cd kgcheckin
        npm install
        task demo.mjs

    展开后：
        echo start
        /usr/bin/python /root/fls/scripts/1.py
        cd kgcheckin
        npm install
        node /root/fls/scripts/demo.mjs
    """
    lines = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    expanded = []

    for line in lines:
        stripped = line.strip()

        # 空行保留，方便日志里看结构。
        if not stripped:
            expanded.append(line)
            continue

        # 注释行保留。
        if stripped.startswith("#"):
            expanded.append(line)
            continue

        # task 行转换为真实命令。
        if is_task_line(stripped):
            cmd = parse_task_line_to_cmd(stripped)
            expanded.append(command_list_to_shell(cmd))
            continue

        # 普通系统命令原样保留。
        expanded.append(line)

    return "\n".join(expanded)


def is_pure_single_task_command(raw):
    """
    是否是单行纯 task 命令。

    这些走非 shell 模式：
        task 1.py
        task demo.mjs arg1

    多行或混合命令走 shell 模式。
    """
    text = str(raw or "").strip()

    if not text:
        return False

    if "\n" in text or "\r" in text:
        return False

    return is_task_line(text)


def build_command(task):
    raw = str(task.get("command", "")).strip()

    if not raw:
        raise ValueError("任务命令为空")

    # ============================================================
    # 1. 单行纯 task 命令：保持原来的直接执行模式。
    # ============================================================
    if is_pure_single_task_command(raw):
        cmd = parse_task_line_to_cmd(raw)

        # 第二个参数是脚本路径。
        # parse_task_line_to_cmd 已经做过校验，这里只用于 cwd。
        parts = shlex.split(raw)
        script_path = task_script_path(parts[1])

        return {
            "cmd": cmd,
            "shell": False,
            "cwd": str(script_path.parent),
            "mode": "task",
        }

    # ============================================================
    # 2. 多行 / 混合命令：shell 模式。
    #
    # 支持：
    #   cd kgcheckin || exit 1
    #   npm install
    #   task ../demo.py
    #   task ../test.mjs
    #
    # 默认工作目录使用 scripts 目录，方便相对路径操作。
    # ============================================================
    expanded_raw = expand_mixed_command(raw)

    return {
        "cmd": expanded_raw,
        "shell": True,
        "cwd": str(SCRIPT_DIR),
        "mode": "mixed",
    }