import os
import shlex
import shutil
import sys
import subprocess
from pathlib import Path

from .paths import SCRIPT_DIR
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


def fls_kill_shell_function():
    """
    返回注入到混合 Shell 命令里的 fls_kill 内置函数。

    支持：
      fls_kill -p 3000
      fls_kill -f 文件或路径
      fls_kill -n 名称或命令关键字
      fls_kill -d PID

    适配 Linux / Termux。
    """
    return r'''
# ============================================================
# FLS 内置命令：fls_kill
# ============================================================
fls_kill() {
    FLS_KILL_PIDS=""

    fls_kill_say() {
        echo "[fls_kill] $*"
    }

    fls_kill_add_pid() {
        _pid="$1"

        case "$_pid" in
            ''|*[!0-9]*)
                return 0
                ;;
        esac

        if [ "$_pid" = "$$" ]; then
            return 0
        fi

        if [ -n "${PPID:-}" ] && [ "$_pid" = "$PPID" ]; then
            return 0
        fi

        FLS_KILL_PIDS="$FLS_KILL_PIDS $_pid"
    }

    fls_kill_add_pids() {
        for _pid in "$@"; do
            fls_kill_add_pid "$_pid"
        done
    }

    fls_kill_uniq_pids() {
        echo "$FLS_KILL_PIDS" \
            | tr ' ' '\n' \
            | grep -E '^[0-9]+$' \
            | sort -u \
            | tr '\n' ' '
    }

    fls_kill_by_port() {
        _port="$1"

        [ -n "$_port" ] || return 0

        fls_kill_say "按端口查找: $_port"

        if command -v fuser >/dev/null 2>&1; then
            _found="$(fuser "${_port}/tcp" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u)"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi

        if command -v lsof >/dev/null 2>&1; then
            _found="$(lsof -ti tcp:"$_port" 2>/dev/null | sort -u)"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi

        if command -v ss >/dev/null 2>&1; then
            _found="$(
                ss -ltnp 2>/dev/null \
                | grep ":$_port " \
                | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
                | sort -u
            )"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi

        if command -v netstat >/dev/null 2>&1; then
            _found="$(
                netstat -ltnp 2>/dev/null \
                | grep ":$_port " \
                | awk '{print $7}' \
                | cut -d/ -f1 \
                | grep -E '^[0-9]+$' \
                | sort -u
            )"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi
    }

    fls_kill_by_file() {
        _file="$1"

        [ -n "$_file" ] || return 0

        fls_kill_say "按文件/路径查找: $_file"

        _abs_file="$_file"

        if command -v realpath >/dev/null 2>&1; then
            _abs_file="$(realpath "$_file" 2>/dev/null || echo "$_file")"
        fi

        if command -v fuser >/dev/null 2>&1; then
            _found="$(fuser "$_abs_file" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u)"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi

        if command -v lsof >/dev/null 2>&1; then
            _found="$(lsof -t "$_abs_file" 2>/dev/null | sort -u)"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi

        if command -v pgrep >/dev/null 2>&1; then
            _found="$(pgrep -f "$_file" 2>/dev/null | sort -u)"
            [ -n "$_found" ] && fls_kill_add_pids $_found

            if [ "$_abs_file" != "$_file" ]; then
                _found="$(pgrep -f "$_abs_file" 2>/dev/null | sort -u)"
                [ -n "$_found" ] && fls_kill_add_pids $_found
            fi
        else
            _found="$(
                ps -ef 2>/dev/null \
                | grep "$_file" \
                | grep -v grep \
                | awk '{print $2}' \
                | sort -u
            )"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi
    }

    fls_kill_by_name() {
        _name="$1"

        [ -n "$_name" ] || return 0

        fls_kill_say "按名称/命令关键字查找: $_name"

        if command -v pgrep >/dev/null 2>&1; then
            _found="$(pgrep -f "$_name" 2>/dev/null | sort -u)"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        else
            _found="$(
                ps -ef 2>/dev/null \
                | grep "$_name" \
                | grep -v grep \
                | awk '{print $2}' \
                | sort -u
            )"
            [ -n "$_found" ] && fls_kill_add_pids $_found
        fi
    }

    fls_kill_by_pid() {
        _pid="$1"
        fls_kill_say "按 PID 添加: $_pid"
        fls_kill_add_pid "$_pid"
    }

    fls_kill_do_kill() {
        _final_pids="$(fls_kill_uniq_pids)"

        if [ -z "$_final_pids" ]; then
            fls_kill_say "未找到需要清理的进程"
            return 0
        fi

        fls_kill_say "准备清理进程: $_final_pids"

        for _pid in $_final_pids; do
            if kill -0 "$_pid" >/dev/null 2>&1; then
                fls_kill_say "TERM $_pid"
                kill "$_pid" >/dev/null 2>&1 || true
            fi
        done

        sleep 1

        for _pid in $_final_pids; do
            if kill -0 "$_pid" >/dev/null 2>&1; then
                fls_kill_say "KILL $_pid"
                kill -9 "$_pid" >/dev/null 2>&1 || true
            fi
        done

        fls_kill_say "清理完成"
    }

    if [ "$#" -eq 0 ]; then
        cat <<'EOF'
fls_kill - FLS 内置进程清理命令

用法：
  fls_kill -p 端口
  fls_kill -f 文件或路径
  fls_kill -n 名称或命令关键字
  fls_kill -d PID

示例：
  fls_kill -p 3000
  fls_kill -n apiService
  fls_kill -f kgcheckin/api/app.js
  fls_kill -d 12345
  fls_kill -p 3000 -n apiService
EOF
        return 0
    fi

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -p|--port)
                shift
                [ "$#" -gt 0 ] || { echo "[fls_kill] 缺少端口"; return 1; }
                fls_kill_by_port "$1"
                ;;
            -f|--file)
                shift
                [ "$#" -gt 0 ] || { echo "[fls_kill] 缺少文件"; return 1; }
                fls_kill_by_file "$1"
                ;;
            -n|--name)
                shift
                [ "$#" -gt 0 ] || { echo "[fls_kill] 缺少名称"; return 1; }
                fls_kill_by_name "$1"
                ;;
            -d|--pid)
                shift
                [ "$#" -gt 0 ] || { echo "[fls_kill] 缺少 PID"; return 1; }
                fls_kill_by_pid "$1"
                ;;
            -h|--help)
                fls_kill
                return 0
                ;;
            *)
                echo "[fls_kill] 未知参数: $1"
                return 1
                ;;
        esac

        shift
    done

    fls_kill_do_kill
}
'''


def has_fls_kill_command(raw):
    """
    判断任务命令中是否使用了 fls_kill。
    """
    lines = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        if stripped.startswith("#"):
            continue

        if stripped == "fls_kill" or stripped.startswith("fls_kill "):
            return True

        if "fls_kill " in stripped:
            return True

    return False


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

        if not stripped:
            expanded.append(line)
            continue

        if stripped.startswith("#"):
            expanded.append(line)
            continue

        if is_task_line(stripped):
            cmd = parse_task_line_to_cmd(stripped)
            expanded.append(command_list_to_shell(cmd))
            continue

        expanded.append(line)

    return "\n".join(expanded)


def is_pure_single_task_command(raw):
    """
    是否是单行纯 task 命令。
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
    # 1. 单行纯 task 命令：保持直接执行模式。
    # ============================================================
    if is_pure_single_task_command(raw):
        cmd = parse_task_line_to_cmd(raw)

        parts = shlex.split(raw)
        script_path = task_script_path(parts[1])

        return {
            "cmd": cmd,
            "display_cmd": command_list_to_shell(cmd),
            "shell": False,
            "cwd": str(script_path.parent),
            "mode": "task",
        }

    # ============================================================
    # 2. 多行 / 混合命令：shell 模式。
    # ============================================================
    expanded_raw = expand_mixed_command(raw)

    display_cmd = expanded_raw

    if has_fls_kill_command(raw):
        run_cmd = fls_kill_shell_function() + "\n\n" + expanded_raw
    else:
        run_cmd = expanded_raw

    return {
        "cmd": run_cmd,
        "display_cmd": display_cmd,
        "shell": True,
        "cwd": str(SCRIPT_DIR),
        "mode": "mixed",
    }