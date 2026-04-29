#!/usr/bin/env bash

# ============================================================
# FLS Manager 启停脚本
#
# 支持：
#   sh fls.sh start
#   sh fls.sh start -t 123456
#   sh fls.sh start -p 5701
#   sh fls.sh start -t 123456 -p 5701
#   sh fls.sh start -p 5701 -t 123456
#   sh fls.sh restart -t 123456 -p 5701
#
# 说明：
# - start / restart 的 -t / -p 是临时生效；
# - 不写入 data/config.json；
# - 菜单中的“修改配置端口 / 修改配置密钥”会写入配置文件；
# - 如果启动时传入 -t / -p，会通过环境变量 FLS_TOKEN / FLS_PORT 覆盖配置。
# ============================================================

set -u

SCRIPT_PATH="$0"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)"
CURRENT_DIR="$(pwd 2>/dev/null || echo .)"

detect_base_dir() {
  # 1. 环境变量优先
  if [ -n "${FLS_BASE_DIR:-}" ]; then
    cd "$FLS_BASE_DIR" 2>/dev/null && pwd
    return 0
  fi

  # 2. fls.sh 所在目录如果有 fls-manager.py，优先使用
  if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/fls-manager.py" ]; then
    cd "$SCRIPT_DIR" 2>/dev/null && pwd
    return 0
  fi

  # 3. 当前执行目录如果有 fls-manager.py，使用当前目录
  if [ -n "$CURRENT_DIR" ] && [ -f "$CURRENT_DIR/fls-manager.py" ]; then
    cd "$CURRENT_DIR" 2>/dev/null && pwd
    return 0
  fi

  # 4. Termux 自动识别：默认 $HOME/fls
  if [ -n "${PREFIX:-}" ] && echo "$PREFIX" | grep -q "com.termux"; then
    echo "$HOME/fls"
    return 0
  fi

  if [ -n "${TERMUX_VERSION:-}" ]; then
    echo "$HOME/fls"
    return 0
  fi

  case "${HOME:-}" in
    /data/data/com.termux*)
      echo "$HOME/fls"
      return 0
      ;;
  esac

  # 5. Linux root / proot 常见路径：优先 /root/fls
  if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
    echo "/root/fls"
    return 0
  fi

  if [ -d "/root/fls" ] && [ -r "/root/fls" ]; then
    echo "/root/fls"
    return 0
  fi

  # 6. 普通 Linux 用户默认 $HOME/fls
  if [ -n "${HOME:-}" ]; then
    echo "$HOME/fls"
    return 0
  fi

  # 7. 最后兜底 fls.sh 所在目录
  echo "$SCRIPT_DIR"
}

BASE_DIR="$(detect_base_dir)"

MANAGER_FILE="$BASE_DIR/fls-manager.py"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/log"
PID_FILE="$DATA_DIR/fls-manager.pid"
DAEMON_LOG="$LOG_DIR/fls-manager-daemon.log"
VENV_DIR="$BASE_DIR/.venv"
MANAGER_DOWNLOAD_URL="https://github.com/liyw0205/fls/raw/refs/heads/main/fls-manager.py"

DEFAULT_PORT="5700"

mkdir -p "$DATA_DIR" "$LOG_DIR"

say() {
  echo "[FLS] $*"
}

err() {
  echo "[FLS][ERROR] $*" >&2
}


download_with_tool() {
  url="$1"
  out="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -L --connect-timeout 15 --max-time 180 --retry 2 -fsSL "$url" -o "$out"
    return $?
  fi

  if command -v wget >/dev/null 2>&1; then
    wget --timeout=180 --tries=2 -O "$out" "$url"
    return $?
  fi

  return 127
}

validate_manager_file() {
  file="$1"

  if [ ! -s "$file" ]; then
    return 1
  fi

  py="$(find_python)"
  "$py" -m py_compile "$file" >/dev/null 2>&1
}

download_manager() {
  mkdir -p "$BASE_DIR" "$DATA_DIR" "$LOG_DIR"

  tmp="$BASE_DIR/.fls-manager.py.download.$$"
  tmp2="$BASE_DIR/.fls-manager.py.download2.$$"
  rm -f "$tmp" "$tmp2"

  say "未找到 fls-manager.py，准备自动下载到工作目录：$BASE_DIR"
  say "下载地址：$MANAGER_DOWNLOAD_URL"

  if ! download_with_tool "$MANAGER_DOWNLOAD_URL" "$tmp"; then
    rm -f "$tmp" "$tmp2"
    err "下载失败：请检查网络，或手动下载 fls-manager.py 到 $BASE_DIR"
    err "手动下载地址：$MANAGER_DOWNLOAD_URL"
    exit 1
  fi

  first_line="$(sed -n '/[^[:space:]]/{p;q}' "$tmp" 2>/dev/null | tr -d '\r')"

  # 情况 1：下载到的内容本身是一条真正脚本下载链接。
  case "$first_line" in
    http://*|https://*)
      say "检测到下载结果是二次下载链接：$first_line"
      if ! download_with_tool "$first_line" "$tmp2"; then
        rm -f "$tmp" "$tmp2"
        err "二次下载失败：$first_line"
        exit 1
      fi
      mv "$tmp2" "$tmp"
      ;;
  esac

  # 情况 2：下载到了 HTML / 跳转页，尝试从里面提取 fls-manager.py 相关链接。
  if ! validate_manager_file "$tmp"; then
    extracted_url="$(grep -Eao "https?://[^\"'<> ]+" "$tmp" 2>/dev/null | grep -m1 "fls-manager.py" || true)"

    if [ -n "$extracted_url" ]; then
      say "检测到页面中包含脚本链接，尝试二次下载：$extracted_url"
      if download_with_tool "$extracted_url" "$tmp2"; then
        mv "$tmp2" "$tmp"
      fi
    fi
  fi

  if ! validate_manager_file "$tmp"; then
    rm -f "$tmp" "$tmp2"
    err "下载到的内容不是有效的 Python 脚本"
    err "可能下载到了网页、错误页或网络被拦截"
    err "请手动访问并下载：$MANAGER_DOWNLOAD_URL"
    exit 1
  fi

  mv "$tmp" "$MANAGER_FILE"
  chmod +x "$MANAGER_FILE" 2>/dev/null || true
  rm -f "$tmp2"

  say "fls-manager.py 下载完成：$MANAGER_FILE"
}

need_manager() {
  if [ ! -f "$MANAGER_FILE" ]; then
    download_manager
  fi

  if [ ! -f "$MANAGER_FILE" ]; then
    err "未找到 $MANAGER_FILE"
    err "请在 FLS 目录执行，或设置 FLS_BASE_DIR"
    exit 1
  fi
}


find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  err "未找到 python3 / python"
  exit 1
}

try_install_git() {
  say "未找到 git，尝试自动安装..."

  if command -v pkg >/dev/null 2>&1; then
    pkg update -y >/dev/null 2>&1 && pkg install -y git >/dev/null 2>&1 && return 0
  fi

  if command -v apt >/dev/null 2>&1; then
    apt update >/dev/null 2>&1 && apt install -y git >/dev/null 2>&1 && return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update >/dev/null 2>&1 && apt-get install -y git >/dev/null 2>&1 && return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    dnf install -y git >/dev/null 2>&1 && return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    yum install -y git >/dev/null 2>&1 && return 0
  fi

  if command -v apk >/dev/null 2>&1; then
    apk add --no-cache git >/dev/null 2>&1 && return 0
  fi

  if command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm git >/dev/null 2>&1 && return 0
  fi

  return 1
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return 0
  fi

  if try_install_git && command -v git >/dev/null 2>&1; then
    say "git 自动安装成功"
    return 0
  fi

  err "未找到 git，且自动安装失败"
  err "请手动安装 git 后再启动 FLS Manager（脚本管理中的拉取仓库功能依赖 git）"
  exit 1
}

ensure_python_env() {
  need_manager

  PY_SYS="$(find_python)"

  if [ -x "$VENV_DIR/bin/python" ]; then
    PY_BIN="$VENV_DIR/bin/python"
    echo "$PY_BIN"
    return 0
  fi

  # 尝试创建虚拟环境。失败时不退出，直接用系统 Python。
  if "$PY_SYS" -m venv "$VENV_DIR" >/dev/null 2>&1; then
    PY_BIN="$VENV_DIR/bin/python"
    "$PY_BIN" -m pip install --upgrade pip >/dev/null 2>&1 || true
    echo "$PY_BIN"
    return 0
  fi

  echo "$PY_SYS"
}

install_basic_deps() {
  PY_BIN="$1"

  "$PY_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
mods = ["flask", "requests", "apscheduler"]
raise SystemExit(0 if all(importlib.util.find_spec(m) for m in mods) else 1)
PY

  if [ "$?" = "0" ]; then
    return 0
  fi

  say "检测到基础依赖可能缺失，尝试安装..."
  "$PY_BIN" -m pip install flask requests apscheduler PySocks tzdata setproctitle >/dev/null 2>&1 || \
  "$PY_BIN" -m pip install --break-system-packages flask requests apscheduler PySocks tzdata setproctitle >/dev/null 2>&1 || \
  "$PY_BIN" -m pip install --user flask requests apscheduler PySocks tzdata setproctitle >/dev/null 2>&1 || true
}

read_config_value() {
  key="$1"
  default_value="$2"

  python3 - "$BASE_DIR" "$key" "$default_value" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
key = sys.argv[2]
default = sys.argv[3]
cfg_file = base / "data" / "config.json"

try:
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    value = data.get(key, default)
    if value is None or value == "":
        value = default
    print(value)
except Exception:
    print(default)
PY
}

write_config_value() {
  key="$1"
  value="$2"

  python3 - "$BASE_DIR" "$key" "$value" <<'PY'
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

data_dir = base / "data"
data_dir.mkdir(parents=True, exist_ok=True)

cfg_file = data_dir / "config.json"

try:
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

if key == "port":
    try:
        port = int(value)
        if port < 1 or port > 65535:
            raise ValueError("端口范围必须是 1-65535")
        data[key] = port
    except Exception as e:
        print("写入失败：{}".format(e))
        raise SystemExit(1)
else:
    data[key] = value

cfg_file.write_text(
    json.dumps(data, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print("已写入 {} = {}".format(key, value))
PY
}

is_running_pid() {
  pid="$1"

  if [ -z "$pid" ]; then
    return 1
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi

  return 1
}

get_pid() {
  if [ -f "$PID_FILE" ]; then
    cat "$PID_FILE" 2>/dev/null | tr -dc '0-9'
    return 0
  fi

  echo ""
}

find_running_pids() {
  # 优先按进程名 / 命令查找 fls-manager.py。
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "fls-manager.py" 2>/dev/null || true
    return 0
  fi

  ps -ef 2>/dev/null | grep "fls-manager.py" | grep -v grep | awk '{print $2}' || true
}

parse_start_opts() {
  TEMP_TOKEN=""
  TEMP_PORT=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      -t|--token)
        shift
        if [ "$#" -eq 0 ]; then
          err "-t / --token 后面需要填写密钥"
          exit 1
        fi
        TEMP_TOKEN="$1"
        ;;
      -p|--port)
        shift
        if [ "$#" -eq 0 ]; then
          err "-p / --port 后面需要填写端口"
          exit 1
        fi
        TEMP_PORT="$1"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "未知参数：$1"
        usage
        exit 1
        ;;
    esac
    shift
  done

  if [ -n "$TEMP_PORT" ]; then
    case "$TEMP_PORT" in
      ''|*[!0-9]*)
        err "端口必须是数字：$TEMP_PORT"
        exit 1
        ;;
    esac

    if [ "$TEMP_PORT" -lt 1 ] || [ "$TEMP_PORT" -gt 65535 ]; then
      err "端口范围必须是 1-65535"
      exit 1
    fi
  fi
}

start_fls() {
  parse_start_opts "$@"
  need_manager
  ensure_git

  old_pid="$(get_pid)"
  if is_running_pid "$old_pid"; then
    say "FLS Manager 已在运行，PID: $old_pid"
    say "如需应用新的临时端口/密钥，请执行：sh fls.sh restart -p 端口 -t 密钥"
    return 0
  fi

  PY_BIN="$(ensure_python_env)"
  install_basic_deps "$PY_BIN"

  cd "$BASE_DIR" || exit 1

  export FLS_BASE_DIR="$BASE_DIR"
  export FLS_PYTHON="$PY_BIN"

  if [ -n "$TEMP_TOKEN" ]; then
    export FLS_TOKEN="$TEMP_TOKEN"
    say "本次启动使用临时密钥：已设置"
  fi

  if [ -n "$TEMP_PORT" ]; then
    export FLS_PORT="$TEMP_PORT"
    say "本次启动使用临时端口：$TEMP_PORT"
  fi

  say "启动 FLS Manager..."
  say "日志文件：$DAEMON_LOG"

  nohup "$PY_BIN" "$MANAGER_FILE" >> "$DAEMON_LOG" 2>&1 &
  pid="$!"

  echo "$pid" > "$PID_FILE"

  sleep 1

  if is_running_pid "$pid"; then
    port_show="${TEMP_PORT:-$(read_config_value port "$DEFAULT_PORT")}"
    say "启动成功，PID: $pid"
    say "访问地址：http://服务器IP:$port_show"
  else
    err "启动失败，请查看日志：$DAEMON_LOG"
    tail -n 80 "$DAEMON_LOG" 2>/dev/null || true
    exit 1
  fi
}

stop_fls() {
  need_manager

  pids=""

  pid="$(get_pid)"
  if is_running_pid "$pid"; then
    pids="$pid"
  fi

  found="$(find_running_pids)"
  if [ -n "$found" ]; then
    pids="$pids $found"
  fi

  pids="$(echo "$pids" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ')"

  if [ -z "$pids" ]; then
    say "FLS Manager 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  say "准备停止 FLS Manager：$pids"

  for p in $pids; do
    kill "$p" >/dev/null 2>&1 || true
  done

  sleep 2

  for p in $pids; do
    if kill -0 "$p" >/dev/null 2>&1; then
      kill -9 "$p" >/dev/null 2>&1 || true
    fi
  done

  rm -f "$PID_FILE"
  say "已停止"
}

restart_fls() {
  parse_start_opts "$@"

  # parse_start_opts 会消费参数，所以这里保存后再传给 start。
  saved_token="$TEMP_TOKEN"
  saved_port="$TEMP_PORT"

  stop_fls

  args=""
  if [ -n "$saved_token" ]; then
    args="$args -t $saved_token"
  fi
  if [ -n "$saved_port" ]; then
    args="$args -p $saved_port"
  fi

  # shellcheck disable=SC2086
  start_fls $args
}

status_fls() {
  pid="$(get_pid)"
  cfg_port="$(read_config_value port "$DEFAULT_PORT")"
  cfg_token="$(read_config_value admin_token "")"

  echo "===================================================="
  echo "FLS Manager 状态"
  echo "工作目录：$BASE_DIR"
  echo "PID 文件：$PID_FILE"
  echo "日志文件：$DAEMON_LOG"
  echo "配置端口：$cfg_port"
  if [ -n "$cfg_token" ]; then
    echo "配置密钥：已设置"
  else
    echo "配置密钥：未设置"
  fi

  if is_running_pid "$pid"; then
    echo "运行状态：运行中"
    echo "PID：$pid"
  else
    found="$(find_running_pids | tr '\n' ' ')"
    if [ -n "$found" ]; then
      echo "运行状态：运行中"
      echo "PID：$found"
    else
      echo "运行状态：未运行"
    fi
  fi
  echo "===================================================="
}

tail_log() {
  touch "$DAEMON_LOG"
  say "日志文件：$DAEMON_LOG"
  tail -n 200 -f "$DAEMON_LOG"
}

show_config() {
  cfg_port="$(read_config_value port "$DEFAULT_PORT")"
  cfg_token="$(read_config_value admin_token "")"

  echo "===================================================="
  echo "当前配置"
  echo "配置文件：$DATA_DIR/config.json"
  echo "端口：$cfg_port"
  if [ -n "$cfg_token" ]; then
    echo "密钥：已设置"
  else
    echo "密钥：未设置"
  fi
  echo "===================================================="
}

set_config_port() {
  printf "请输入新的配置端口，1-65535："
  read -r port

  if [ -z "$port" ]; then
    err "端口不能为空"
    return 1
  fi

  case "$port" in
    *[!0-9]*)
      err "端口必须是数字"
      return 1
      ;;
  esac

  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    err "端口范围必须是 1-65535"
    return 1
  fi

  write_config_value port "$port"
  say "配置端口已修改为 $port"
  say "注意：配置端口需要重启后生效"
}

set_config_token() {
  printf "请输入新的配置密钥，留空表示取消："
  read -r token

  if [ -z "$token" ]; then
    say "已取消"
    return 0
  fi

  write_config_value admin_token "$token"
  say "配置密钥已修改"
  say "注意：配置密钥需要重启后生效"
}

temp_start_menu() {
  printf "临时端口，可空："
  read -r port

  printf "临时密钥，可空："
  read -r token

  args=""
  if [ -n "$port" ]; then
    args="$args -p $port"
  fi
  if [ -n "$token" ]; then
    args="$args -t $token"
  fi

  # shellcheck disable=SC2086
  start_fls $args
}

temp_restart_menu() {
  printf "临时端口，可空："
  read -r port

  printf "临时密钥，可空："
  read -r token

  args=""
  if [ -n "$port" ]; then
    args="$args -p $port"
  fi
  if [ -n "$token" ]; then
    args="$args -t $token"
  fi

  # shellcheck disable=SC2086
  restart_fls $args
}

menu() {
  while true; do
    echo ""
    echo "===================================================="
    echo "FLS Manager 菜单"
    echo "===================================================="
    echo "1. 启动"
    echo "2. 停止"
    echo "3. 重启"
    echo "4. 状态"
    echo "5. 查看实时日志"
    echo "6. 临时端口/密钥启动，不写入配置"
    echo "7. 临时端口/密钥重启，不写入配置"
    echo "8. 查看配置"
    echo "9. 修改配置端口，写入配置"
    echo "10. 修改配置密钥，写入配置"
    echo "0. 退出"
    echo "===================================================="
    printf "请选择："
    read -r choice

    case "$choice" in
      1) start_fls ;;
      2) stop_fls ;;
      3) restart_fls ;;
      4) status_fls ;;
      5) tail_log ;;
      6) temp_start_menu ;;
      7) temp_restart_menu ;;
      8) show_config ;;
      9) set_config_port ;;
      10) set_config_token ;;
      0) exit 0 ;;
      *) echo "无效选择" ;;
    esac
  done
}

usage() {
  cat <<USAGE_EOF
用法：
  sh fls.sh start [-t 密钥] [-p 端口]
  sh fls.sh stop
  sh fls.sh restart [-t 密钥] [-p 端口]
  sh fls.sh status
  sh fls.sh log
  sh fls.sh menu
  sh fls.sh ensure-manager

示例：
  sh fls.sh start -t 123456
  sh fls.sh start -p 5701
  sh fls.sh start -t 123456 -p 5701
  sh fls.sh start -p 5701 -t 123456
  sh fls.sh restart -t 123456 -p 5701

说明：
  start / restart 的 -t / -p 为临时参数，不写入配置。
  菜单中的“修改配置端口 / 修改配置密钥”会写入 data/config.json。
USAGE_EOF
}

cmd="${1:-menu}"
shift || true

case "$cmd" in
  start)
    start_fls "$@"
    ;;
  stop)
    stop_fls
    ;;
  restart)
    restart_fls "$@"
    ;;
  status)
    status_fls
    ;;
  log|logs)
    tail_log
    ;;
  menu)
    menu
    ;;
  ensure-manager|download-manager)
    need_manager
    say "fls-manager.py 已就绪：$MANAGER_FILE"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    err "未知命令：$cmd"
    usage
    exit 1
    ;;
esac
